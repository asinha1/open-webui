"""mh_grounding.fetch — the read_page v1.4 core (SSRF-guarded fetch + render escalation).

Extracted 1:1 from mh-tools/read_page.py v1.4 (RFC-MH-005 P1). Behavior, model-facing
strings, and log messages are deliberately byte-identical — the OWUI tool and the mh-mcp
server both return `FetchResult.text` verbatim, so the two surfaces can't drift. The
logger stays "mh.read_page" so existing log greps keep working regardless of process.

The SSRF guard is load-bearing: this box is on a tailnet with internal services
(llama-server 127.0.0.1:8081, OWUI :8080, AdGuard/HA, the LAN). The model must never be
able to make a fetch reach any non-public address — see url_refusal(). Client-side
redirect targets (meta-refresh/JS) are SSRF-re-checked at the top of the fetch loop, and
during a headless render EVERY sub-request host is vetted (fail-closed).

Config fields are UPPERCASE on purpose: they mirror the OWUI tool's Valve names 1:1, so
the wrapper is `FetchConfig(**self.valves.model_dump())` — no field-mapping to drift.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from markdownify import MarkdownConverter
except ImportError:
    MarkdownConverter = None

try:
    import certifi
    # TLS context from certifi's CA bundle, not OpenSSL's default search path. On this host the
    # default context can't complete some valid chains (the server sends a leaf but the needed
    # intermediate lives only in the macOS system store, which Python's OpenSSL doesn't read) ->
    # [SSL: CERTIFICATE_VERIFY_FAILED] on sites the OS trusts fine (e.g. cityjobs.nyc.gov).
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = None

try:
    # The JS-render escalation. Optional, heavy dep (a browser engine) — guarded so a box
    # WITHOUT it keeps the full plain-fetch path; escalation just degrades off.
    from playwright.async_api import async_playwright, Error as PlaywrightError
    _PW_OK = True
except Exception as _e:
    async_playwright = None
    PlaywrightError = Exception
    _PW_OK = False
    logging.getLogger("mh.read_page").warning(
        "playwright unavailable, JS-render escalation off: %s", _e)

log = logging.getLogger("mh.read_page")

# Tailscale / CGNAT shared address space (RFC 6598). Python 3.11 already reports
# is_global=False for this, but we block it explicitly so the guard is version-proof.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_MAX_REDIRECTS = 5
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/123.0 Safari/537.36")
_XML_HINTS = ("xml", "rss", "atom")
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_SKIP_TAGS = ["script", "style", "nav", "footer", "header", "aside", "noscript",
              "form", "svg", "button", "iframe"]
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\'][^"\']*?url=([^"\'>]+)', re.I)
_JS_REDIRECT_RE = re.compile(
    r'location\.(?:replace|assign|href)\s*(?:\(\s*|=\s*)["\']([^"\']+)["\']', re.I)
_MD_LINK_RE = re.compile(r'\]\((\S+?)(\s+"[^"]*")?\)')

# block elements that carry section content (collected in document order)
_SECTION_BLOCKS = ("p", "table", "ul", "ol", "dl", "blockquote", "pre", "figure",
                   "h3", "h4", "h5", "h6")


@dataclass
class FetchConfig:
    """Mirrors mh-tools/read_page.py v1.4 Valves — names AND defaults. See that file's
    Valve descriptions for the sizing rationale; do not re-derive defaults here."""
    MAX_CONTENT_CHARS: int = 60000
    MAX_FEED_ITEMS: int = 500
    MAX_FETCH_BYTES: int = 5_000_000
    MIN_READABLE_CHARS: int = 200
    TIMEOUT: int = 30
    USE_TAVILY_EXTRACT: bool = False
    TAVILY_API_KEY: str = ""
    RENDER_ESCALATION: bool = True
    RENDER_TIMEOUT: int = 35
    RENDER_NETWORKIDLE_MS: int = 6000
    RENDER_BLOCK_MEDIA: bool = True


@dataclass
class FetchResult:
    """`text` is the complete model-facing return (identical wording across clients).
    `ok` means the success path was reached — adapters emit citations only then, from
    body/title/final_url."""
    ok: bool
    text: str
    title: str = ""
    final_url: str = ""
    body: str = ""
    rendered: bool = False
    truncated: bool = False


async def _noop_status(desc, done=False):
    return None


def _absolutize_links(md, base):
    """Rewrite relative markdown link targets to absolute (so the model can read_page them).
    Leaves http(s)/mailto/#anchor targets untouched."""
    if not base:
        return md

    def repl(m):
        target, title = m.group(1), m.group(2) or ""
        if target.startswith(("http://", "https://", "mailto:", "#", "tel:", "data:")):
            return m.group(0)
        return f"]({urljoin(base, target)}{title})"

    return _MD_LINK_RE.sub(repl, md)


def _localname(tag):
    """Strip an XML namespace: '{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1].lower()


if MarkdownConverter is not None:
    class _AltOnlyMarkdown(MarkdownConverter):
        """HTML->Markdown that keeps an image's ALT text for context but drops the (noisy) src URL."""
        def convert_img(self, el, text, *args, **kwargs):
            alt = (el.attrs.get("alt") or "").strip()
            return f"![{alt}]" if alt else ""
else:
    _AltOnlyMarkdown = None


# ---- SSRF guard --------------------------------------------------------------

async def url_refusal(url):
    """None if the URL is a public http(s) address; else a model-readable refusal string.
    Rejects non-http schemes, internal/private/loopback/link-local/CGNAT(tailnet) IPs, and
    bare .local/.internal/localhost names — before any connection is made."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"I can only read http/https URLs (got '{parts.scheme or 'no scheme'}')."
    host = parts.hostname
    if not host:
        return "That URL has no host I can read."
    low = host.lower().rstrip(".")
    if low == "localhost" or low.endswith(".local") or low.endswith(".internal"):
        return "I can't fetch internal or private addresses — only public web URLs."
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, proto=socket.IPPROTO_TCP
        )
    except (socket.gaierror, OSError):
        return f"I couldn't resolve that host ({host}); check the URL."
    for info in infos:
        addr = info[4][0].split("%")[0]  # drop any IPv6 scope id
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return "I can't fetch internal or private addresses — only public web URLs."
        if ip in _CGNAT_V4 or not ip.is_global:
            return "I can't fetch internal or private addresses — only public web URLs."
    return None


# ---- fetch + SSRF-safe redirect following -------------------------------------

def _client_redirect_target(head_text, base_url):
    """A meta-refresh or JS location.replace target in the page head, or None."""
    m = _META_REFRESH_RE.search(head_text) or _JS_REDIRECT_RE.search(head_text)
    if not m:
        return None
    return urljoin(base_url, unescape(m.group(1)).strip())


async def fetch_following_redirects(session, url, cfg: FetchConfig):
    """Return (final_url, status, content_type, raw_bytes), or a model-readable refusal string.
    Each hop — initial, every HTTP 3xx, and every client-side (meta-refresh / JS) redirect —
    is SSRF-checked BEFORE we connect to it."""
    for _hop in range(_MAX_REDIRECTS + 1):
        refusal = await url_refusal(url)
        if refusal:
            return refusal
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                if not loc:
                    break  # redirect with no target -> treat as a dead end
                url = urljoin(url, loc)
                continue
            final_url = str(resp.url)
            status = resp.status
            ctype = (resp.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            raw = b""
            async for chunk in resp.content.iter_chunked(65536):
                raw += chunk
                if len(raw) >= cfg.MAX_FETCH_BYTES:
                    raw = raw[: cfg.MAX_FETCH_BYTES]
                    break
        # Client-side redirect (meta-refresh / JS) — the "open in app" / share interstitials
        # (e.g. open.substack.com). Follow it like an HTTP redirect; SSRF re-checked next loop.
        if "html" in ctype:
            tgt = _client_redirect_target(raw[:8192].decode("utf-8", "replace"), final_url)
            if tgt and tgt != url:
                url = tgt
                continue
        return (final_url, status, ctype, raw)
    return "That URL redirected too many times; it may be a redirect loop or a broken link."


# ---- rendering by content type -------------------------------------------------

def render(ctype, raw, fragment="", base_url="", cfg: FetchConfig = None):
    """Return (title, text). HTML -> Markdown (links/tables kept); XML/RSS/Atom -> compact
    item list; JSON -> pretty; text/* -> as-is; anything else -> empty (caller emits the note)."""
    cfg = cfg or FetchConfig()
    if ctype.startswith("image/") or ctype.startswith("audio/") or ctype.startswith("video/") \
            or ctype in ("application/octet-stream", "application/pdf", "application/zip"):
        return "", ""

    text = _decode(raw)

    if "html" in ctype or (not ctype and "<html" in text[:2000].lower()):
        return html_to_markdown(text, fragment, base_url)
    # RSS/Atom feed (the enumeration use case): parse into a compact "N. title — date — link"
    # list so the WHOLE feed fits the char budget — raw XML is ~10x larger per item.
    looks_feed = any(h in ctype for h in _XML_HINTS) or \
        any(tag in text[:1500].lower() for tag in ("<rss", "<feed", "<channel"))
    if looks_feed:
        items = _feed_items(text)
        if items:
            shown = items[: cfg.MAX_FEED_ITEMS]
            lines = []
            for i, (it_title, it_date, it_link) in enumerate(shown, 1):
                parts = [f"{i}. {it_title or '(untitled)'}"]
                if it_date:
                    parts.append(it_date)
                if it_link:
                    parts.append(it_link)
                lines.append(" — ".join(parts))
            body = "\n".join(lines)
            if len(items) > len(shown):
                body += (f"\n…(+{len(items) - len(shown)} more items beyond MAX_FEED_ITEMS)")
            return f"feed: {len(items)} items", body
        return "", _collapse(text)  # not parseable as a feed -> raw passthrough
    # JSON by content-type OR by content-sniff — iTunes serves JSON as text/javascript,
    # and some APIs mislabel; if the body parses as JSON, pretty-print it.
    if "json" in ctype or "javascript" in ctype or text.lstrip()[:1] in ("{", "["):
        try:
            return "", json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass
    if ctype.startswith("text/") or not ctype:
        return "", _collapse(text)
    return "", ""  # unknown non-text type


# ---- HTML -> Markdown -----------------------------------------------------------

def html_to_markdown(html, fragment="", base_url=""):
    """Convert HTML to Markdown (links/tables/headings preserved, images -> alt text only,
    relative links absolutized so they're followable). If a #fragment is given, render just
    that section. Falls back to plain text if the markdownify/bs4 deps are absent."""
    if BeautifulSoup is None or _AltOnlyMarkdown is None:
        return _html_to_text_plain(html)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_SKIP_TAGS):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    conv = _AltOnlyMarkdown(heading_style="ATX")
    md = ""
    if fragment:
        nodes = _focus_fragment(soup, fragment)
        if nodes:
            md = _collapse_md("\n\n".join(conv.convert(str(n)) for n in nodes))
    if not md:  # no fragment, fragment not found, or it rendered empty -> whole main content
        target = soup.find("article") or soup.find("main") or soup.body or soup
        md = _collapse_md(conv.convert(str(target)))
    return title, _absolutize_links(md, base_url)


def _focus_fragment(soup, fragment):
    """Nodes for the section a #fragment points at. If the id is on a content container
    (section/table/list), return just that. If it's on/under a heading, collect the heading
    plus the following content blocks in DOCUMENT order — across DOM nesting, which sibling-
    walking misses (e.g. Wikipedia's table isn't a sibling of its heading) — up to the next
    heading of the same-or-higher level. None if the id isn't found."""
    anchor = soup.find(id=fragment)
    if anchor is None:
        return None
    if anchor.name in ("section", "article", "main", "table", "ul", "ol", "dl", "figure"):
        return [anchor]
    hd = anchor if anchor.name in _HEADINGS else anchor.find_parent(_HEADINGS)
    if hd is None:
        return [anchor]  # id on a non-heading element — use it directly
    level = int(hd.name[1])
    nodes = [hd]
    added = {id(hd)}  # dedupe: skip blocks nested inside an already-collected block
    for el in hd.find_all_next():
        nm = getattr(el, "name", None)
        if nm in _HEADINGS and int(nm[1]) <= level:
            break  # next section of same-or-higher level
        if nm in _SECTION_BLOCKS and not any(id(a) in added for a in el.parents):
            nodes.append(el)
            added.add(id(el))
    return nodes


def _html_to_text_plain(html):
    """Fallback when markdownify/bs4 are unavailable — flat readable text."""
    if BeautifulSoup is None:
        stripped = re.sub(r"(?is)<(script|style|nav|footer|header|aside|noscript)[^>]*>.*?</\1>", " ", html)
        stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
        return "", _collapse(unescape(stripped))
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_SKIP_TAGS):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    return title, _collapse(soup.get_text("\n"))


def _feed_items(text):
    """Parse an RSS/Atom feed into [(title, date, link), ...]; None if not parseable.
    Refuses DOCTYPE/ENTITY first — stdlib ElementTree expands internal entities (billion-laughs)."""
    head = text[:2000].lower()
    if "<!doctype" in head or "<!entity" in head:
        return None
    try:
        root = ET.fromstring(text.encode("utf-8", "replace"))
    except ET.ParseError:
        return None
    items = []
    for el in root.iter():
        if _localname(el.tag) not in ("item", "entry"):
            continue
        title = date = ""
        links = []
        for ch in el:
            ln = _localname(ch.tag)
            if ln == "title" and not title:
                title = "".join(ch.itertext()).strip()
            elif ln == "link":
                href = ch.get("href")
                if href:
                    links.append((ch.get("rel"), href))     # Atom: href attr
                elif (ch.text or "").strip():
                    links.append((None, ch.text.strip()))    # RSS: element text
            elif ln in ("pubdate", "published", "updated", "date") and not date:
                date = (ch.text or "").strip()
        link = next((h for rel, h in links if rel in (None, "alternate")), "")
        if not link and links:
            link = links[0][1]
        items.append((title, date, link))
    return items or None


def _decode(raw):
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _collapse(text):
    """Aggressive collapse for raw/feed/plain text — drop blank lines."""
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _collapse_md(text):
    """Markdown-preserving collapse — keep paragraph breaks, trim trailing space, cap blank runs."""
    text = "\n".join(ln.rstrip() for ln in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---- internal JS-render escalation (the folded-in render_page) -------------------

async def render_with_browser(url, fragment="", cfg: FetchConfig = None):
    """Headless-Chromium escalation for a page the plain fetch got as an app shell. Reuses
    url_refusal (per-request route guard) + html_to_markdown (fragment-aware). Returns
    (title, body); ('', '') on any failure (the caller then emits the access-failure marker).
    SSRF is HARDER here — a browser fires its OWN sub-requests — so every request is
    host-checked via the route guard, fail-closed, with a per-host verdict cache."""
    cfg = cfg or FetchConfig()
    if not _PW_OK:
        return "", ""
    host_verdicts = {}  # host -> None(ok) | str(refuse); one DNS check per distinct host

    async def _guard(route):
        req = route.request
        try:
            if cfg.RENDER_BLOCK_MEDIA and req.resource_type in _BLOCKED_RESOURCE_TYPES:
                return await route.abort()
            host = urlsplit(req.url).hostname
            verdict = host_verdicts.get(host, "‹unset›")
            if verdict == "‹unset›":
                verdict = await url_refusal(req.url)
                host_verdicts[host] = verdict
            if verdict is None:
                await route.continue_()
            else:
                log.warning("read_page render SSRF-blocked sub-request host=%r", host)
                await route.abort()
        except Exception:
            try:
                await route.abort()  # never let the guard raise into the browser; fail closed
            except Exception:
                pass

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=_UA)
                await context.route("**/*", _guard)
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=cfg.RENDER_TIMEOUT * 1000)
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=cfg.RENDER_NETWORKIDLE_MS)
                except PlaywrightError:
                    pass  # networkidle is best-effort; proceed with what rendered
                final_url = page.url
                html = await page.content()
            finally:
                await browser.close()
    except PlaywrightError as e:
        msg = str(e).splitlines()[0] if str(e) else "navigation error"
        log.warning("read_page render failed url=%r: %s", url[:120], msg)
        return "", ""
    return html_to_markdown(html, fragment, final_url)


def render_available():
    """True when the headless-render escalation can actually run (playwright importable)."""
    return _PW_OK


# ---- optional Tavily Extract fallback (config-gated) ------------------------------

async def _tavily_extract(session, url, cfg: FetchConfig):
    try:
        payload = {"api_key": cfg.TAVILY_API_KEY, "urls": [url], "extract_depth": "basic"}
        async with session.post("https://api.tavily.com/extract", json=payload) as resp:
            if resp.status != 200:
                return ""
            data = await resp.json()
        results = data.get("results") or []
        content = results[0].get("raw_content", "") if results else ""
        log.info("read_page tavily_extract url=%r usage=%s chars=%d",
                 url[:120], data.get("usage") or {}, len(content))
        return _collapse(content)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError) as e:
        log.warning("read_page tavily_extract failed url=%r: %s", url[:120], e)
        return ""


# Public aliases for adapters that compose their own flows (deep_research) — the
# helpers themselves stay single-copy here.
UA = _UA
SSL_CONTEXT = _SSL_CONTEXT
decode_bytes = _decode
collapse = _collapse
collapse_md = _collapse_md
absolutize_links = _absolutize_links


# ---- the composed read_page flow ---------------------------------------------------

async def read_url(url: str, max_chars: Optional[int] = None,
                   cfg: FetchConfig = None, on_status=None) -> FetchResult:
    """The full read_page v1.4 flow: SSRF-guarded fetch -> content-type render ->
    JS-render escalation on a near-empty HTML shell -> failure-to-access marker.
    `on_status(desc, done=False)` is an optional async progress hook (OWUI status events);
    `FetchResult.text` is the complete model-facing return, identical across clients."""
    cfg = cfg or FetchConfig()
    emit_status = on_status or _noop_status

    url = (url or "").strip()
    if not url:
        return FetchResult(False, "No URL given to read.")
    fragment = urlsplit(url).fragment  # user intent — focus rendering on this section

    cap = cfg.MAX_CONTENT_CHARS
    if max_chars is not None:
        try:
            cap = max(500, min(int(max_chars), cfg.MAX_CONTENT_CHARS))
        except (TypeError, ValueError):
            cap = cfg.MAX_CONTENT_CHARS

    await emit_status(f"Reading {url}")

    try:
        timeout = aiohttp.ClientTimeout(total=cfg.TIMEOUT)
        # Apply the certifi-backed TLS context (when available) so valid HTTPS chains that
        # the default OpenSSL store can't complete still verify.
        connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": _UA}, connector=connector
        ) as session:
            fetched = await fetch_following_redirects(session, url, cfg)
            if isinstance(fetched, str):  # a model-readable refusal/error
                await emit_status("Could not read the page.", done=True)
                return FetchResult(False, fetched)
            final_url, status, ctype, raw = fetched

            if status != 200:
                await emit_status(f"Page returned HTTP {status}.", done=True)
                return FetchResult(
                    False,
                    f"That page returned HTTP {status}; the URL may be wrong or the page is gone. "
                    f"({final_url})")

            title, body = render(ctype, raw, fragment, final_url, cfg)

            # JS-heavy page yielded almost nothing -> optional Tavily Extract fallback.
            if (("html" in ctype) and len(body.strip()) < cfg.MIN_READABLE_CHARS
                    and cfg.USE_TAVILY_EXTRACT and cfg.TAVILY_API_KEY):
                extracted = await _tavily_extract(session, final_url, cfg)
                if extracted:
                    body = extracted

    except asyncio.TimeoutError:
        await emit_status("Reading the page timed out.", done=True)
        return FetchResult(
            False,
            f"Reading that page timed out after {cfg.TIMEOUT}s; "
            "try again or work from what you have.")
    except aiohttp.ClientError as e:
        log.warning("read_page network error url=%r: %s", url[:120], e)
        await emit_status("Reading the page failed (network).", done=True)
        return FetchResult(
            False,
            "Reading that page failed (network error). Work from what you have, or try a different URL.")

    # Near-empty HTML = the plain fetch got an app shell (JS-gated / walled / interstitial).
    # TIER 2 — escalate to a headless-Chromium render INTERNALLY so the model never has to
    # pick read-vs-render. Fires only on this cold path; the hot path never touches the browser.
    rendered = False
    if ("html" in ctype and len(body.strip()) < cfg.MIN_READABLE_CHARS
            and cfg.RENDER_ESCALATION and _PW_OK):
        await emit_status("Page looks JavaScript-rendered — escalating to a headless browser…")
        r_title, r_body = await render_with_browser(final_url, fragment, cfg)
        if len(r_body.strip()) >= cfg.MIN_READABLE_CHARS:
            body, title, rendered = r_body, (r_title or title), True

    # Still near-empty after the fetch (and the render, if it ran) = retrieval FAILURE, not
    # "the content isn't there". The model must report it couldn't ACCESS the page rather
    # than infer absence (the Harry's "they don't list a menu" failure, 2026-06-06).
    if "html" in ctype and len(body.strip()) < cfg.MIN_READABLE_CHARS:
        await emit_status("Page returned almost no readable text.", done=True)
        tried = ("fetched it and rendered it in a headless browser"
                 if (cfg.RENDER_ESCALATION and _PW_OK) else "fetched it")
        return FetchResult(
            False,
            f"I {tried}, but {final_url} returned almost no readable text "
            f"({len(body.strip())} chars) — it's most likely behind a login/anti-bot wall or an "
            f"'open in app' interstitial. Treat this as a FAILURE TO ACCESS the page, NOT as "
            f"evidence the content doesn't exist. Tell the user you couldn't read it, and try a "
            f"different source or the site's RSS/Atom feed.")

    if not body.strip():
        return FetchResult(
            False,
            "That URL returned no readable text (it may be a media/binary file or a "
            "JavaScript-only app). Try its RSS/Atom feed or a different source.")

    truncated = len(body) > cap
    if truncated:
        body = body[:cap].rstrip() + "…"

    log.info(
        "read_page url=%r status=%s ctype=%s frag=%r bytes=%d chars=%d rendered=%s truncated=%s",
        final_url[:120], status, ctype, fragment, len(raw), len(body), rendered, truncated,
    )

    await emit_status(f"Read {len(body)} chars.", done=True)

    header = f"Content of {final_url}"
    if rendered:
        header += " (read via headless render — the plain fetch returned an app shell)"
    if title:
        header += f" — {title}"
    note = ("\n\n[content truncated to fit; the page/list may be incomplete — re-read with a "
            "higher max_chars or a more specific URL if you need more]") if truncated else ""
    return FetchResult(True, f"{header}\n\n{body}{note}",
                       title=title, final_url=final_url, body=body,
                       rendered=rendered, truncated=truncated)
