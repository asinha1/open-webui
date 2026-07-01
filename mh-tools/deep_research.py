"""
title: Deep Research
author: mh-tools
version: 1.2.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — version-controlled in the open-webui fork under mh-tools/.
# Reads SEVERAL sources at once and returns one consolidated digest, so the model can research a
# topic across many pages in a single turn instead of reading them one-by-one. Closes the gap the
# 2026-06-06 readout surfaced (the 4-article ingestion chat read pages sequentially).
#
# THE PARALLELISM IS TOOL-I/O, NOT INFERENCE. The conversation runs on one llama-server slot
# (--parallel 1, deliberate). You cannot run multiple Gemma generations at once — but you CAN fetch
# N pages concurrently (I/O-bound) and hand the model ONE digest to synthesize in a single pass.
# That also sidesteps Gemma's native-FC parallel-call unreliability: the model makes ONE tool call;
# the fan-out is deterministic code here. And it's CACHE-FRIENDLIER than N sequential read turns —
# one prefill of the digest at the end of context, not N re-prefills (the two-pass-FC cost).
#
# Two modes: pass `urls` (read exactly those, in parallel) or `query` (search the web, then read the
# top results in parallel). Reuses read_page's SSRF guard + certifi TLS + markdownify render; query
# mode reuses the tavily_search REST call (key in this tool's Valves). Deployed via Workspace ->
# Tools, then RESTART OWUI. Design + acceptance: mh-tools/deep_research.md.
#
# v1.1.0 (2026-06-08): when sources can't be read, the digest ROUTES the model to the right next
# action instead of inviting a reworded re-search — JS-gated -> "open it with the JavaScript-capable
# reader"; access-blocked (401/403/429) -> "read a different authoritative source", not "search
# again". Part of the v10 over-search/under-read fix (system-prompt-v10-research.md). Signature same.
#
# v1.2.0 (2026-06-12): joins the CROSS-TOOL over-search governor (Thread #2). `query` mode is a web
#   SEARCH, so it shares tavily_search's per-chat dedup + budget (a query that repeats a prior
#   tavily/deep_research search is skipped; the escalating read-nudge counts deep_research queries
#   toward the combined budget). `urls` mode is a READ — never deduped; its URLs are noted as
#   "reading happened". Closes the v1.1 escape hatch where the model fled the tavily governor into
#   the ungoverned deep_research (probe 10c). The shared block below is MIRRORED byte-for-byte in
#   tavily_search.py — keep the two copies in sync.

import asyncio
import ipaddress
import logging
import re
import socket
import ssl
import sys
import types
from html import unescape
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
from pydantic import BaseModel, Field

from open_webui.utils.telemetry.mh_tools import instrument, governor_event  # [mh] tool-usage metrics

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
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = None

log = logging.getLogger("mh.deep_research")

_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_MAX_REDIRECTS = 5
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/123.0 Safari/537.36")
_SKIP_TAGS = ["script", "style", "nav", "footer", "header", "aside", "noscript",
              "form", "svg", "button", "iframe"]
_MD_LINK_RE = re.compile(r'\]\((\S+?)(\s+"[^"]*")?\)')
_TAVILY_URL = "https://api.tavily.com/search"


def _absolutize_links(md, base):
    if not base:
        return md
    def repl(m):
        target, title = m.group(1), m.group(2) or ""
        if target.startswith(("http://", "https://", "mailto:", "#", "tel:", "data:")):
            return m.group(0)
        return f"]({urljoin(base, target)}{title})"
    return _MD_LINK_RE.sub(repl, md)


if MarkdownConverter is not None:
    class _AltOnlyMarkdown(MarkdownConverter):
        def convert_img(self, el, text, *args, **kwargs):
            alt = (el.attrs.get("alt") or "").strip()
            return f"![{alt}]" if alt else ""
else:
    _AltOnlyMarkdown = None


# ===== over-search governor — SHARED in-process store (Thread #2, v1.2 cross-tool) ===========
# MIRRORED byte-for-byte in tavily_search.py. OWUI DB-tools can't import a sibling module, so the
# block is duplicated; the SHARED STATE is a sys.modules sentinel both tools (and the eval harness)
# reach — one per-chat budget + dedup set within the single-worker uvicorn process. Pure helpers
# below are stateless (operate on a passed-in state dict); only CHAT/ORDER on the sentinel are
# singleton. Ephemeral (lost on restart — fine, over-search is within-conversation).
_GOV_MAX_CHATS = 200
_GOV_STOPWORDS = frozenset(
    "the a an of to in for on at and or vs with from by is are be as what which how when "
    "where who whom current latest list find show me get all any near".split()
)
# Collapse number / salary-syntax variants to one <num> token so cosmetic facet-repeats dedup:
#   "100,000" == "$100k" == "100,000..200,000" == "$100,000 - $150,000".
_GOV_NUM_RE = re.compile(
    r"[\$£€]?\d[\d,\.]*\s*[kKmM]?(?:\s*(?:\.\.|-|to)\s*[\$£€]?\d[\d,\.]*\s*[kKmM]?)?"
)


def _gov_store():
    """Process-global shared store (sys.modules sentinel) — the SAME dict for tavily_search,
    deep_research, and the eval harness, within one uvicorn process."""
    m = sys.modules.get("_mh_governor_store")
    if m is None:
        m = types.ModuleType("_mh_governor_store")
        m.CHAT = {}    # chat_id -> {"norm":[frozenset], "raw":[str], "urls":set(), "searches":int}
        m.ORDER = []   # LRU order of chat_ids
        sys.modules["_mh_governor_store"] = m
    return m


def _gov_normalize(q):
    """Query -> token SET for Jaccard near-dup detection (number/salary variants -> <num>,
    site:<domain> kept, stopwords dropped)."""
    q = (q or "").lower().replace('"', " ").replace("'", " ")
    q = _GOV_NUM_RE.sub(" <num> ", q)
    toks = set()
    for raw in q.split():
        t = raw.strip(".,;()[]{}!?")  # trim edge punctuation; keeps site:foo.bar and <num> intact
        if t and t not in _GOV_STOPWORDS:
            toks.add(t)
    return frozenset(toks)


def _gov_jaccard(a, b):
    return (len(a & b) / len(a | b)) if (a and b) else 0.0


def _gov_state(chat_id):
    """Get-or-create per-chat state on the shared store (LRU-bounded)."""
    store = _gov_store()
    st = store.CHAT.get(chat_id)
    if st is None:
        st = {"norm": [], "raw": [], "urls": set(), "searches": 0}
        store.CHAT[chat_id] = st
        store.ORDER.append(chat_id)
        while len(store.ORDER) > _GOV_MAX_CHATS:
            store.CHAT.pop(store.ORDER.pop(0), None)
    return st


def _gov_near_dup(st, query, threshold):
    """If `query` is a near-duplicate of a prior search THIS CHAT (any tool), return the skip note;
    else None. Conservative: catches cosmetic repeats, leaves genuinely different facets."""
    nq = _gov_normalize(query)
    best, best_raw = 0.0, None
    for pn, pr in zip(st["norm"], st["raw"]):
        j = _gov_jaccard(nq, pn)
        if j > best:
            best, best_raw = j, pr
    if best >= threshold and best_raw is not None:
        hint = (" Open a page you already found with read_page to get a specific value, or search a "
                "genuinely different facet." if st["urls"] else
                " Refine to a genuinely different facet, or read a result with read_page.")
        return (f"[over-search guard] Skipped: nearly identical to your earlier search «{best_raw}» "
                f"(similarity {best:.0%}); re-running won't surface new results.{hint}")
    return None


def _gov_record_search(st, query, urls):
    """Record a real web search (tavily OR deep_research query-mode) into the shared per-chat state."""
    st["norm"].append(_gov_normalize(query))
    st["raw"].append(query)
    st["searches"] += 1
    for u in urls:
        if isinstance(u, str) and u.startswith("http"):
            st["urls"].add(u)


def _gov_note_urls(st, urls):
    """Note URLs a READ surfaced (deep_research urls-mode) without counting a search."""
    for u in urls:
        if isinstance(u, str) and u.startswith("http"):
            st["urls"].add(u)


def _gov_nudge(st, soft_k):
    """Read-nudge after K combined searches with URLs in hand; escalates to a firm stop at 2K."""
    n = st["searches"]
    if n >= max(1, soft_k) and st["urls"]:
        if n >= 2 * max(1, soft_k):
            return (f"\n[over-search guard] You've run {n} web searches this conversation. Stop "
                    f"searching — you have enough sources; synthesize an answer from what you've "
                    f"found, or open a specific listing with read_page. More broad searches won't help.")
        return (f"\n[over-search guard] You've run {n} searches this conversation and already have "
                f"specific page URLs. Open the most relevant result with read_page to verify exact "
                f"values rather than searching again.")
    return None
# ===== end shared governor block =============================================================


class Tools:
    class Valves(BaseModel):
        MAX_SOURCES: int = Field(
            8, description="Hard cap on how many sources are fetched in one call (guards a runaway)."
        )
        CONCURRENCY: int = Field(
            5, description="Max concurrent fetches (I/O parallelism; the inference stays single-slot)."
        )
        MAX_CHARS_PER_SOURCE: int = Field(
            10000, description="Truncate each source's extracted content to this many chars."
        )
        MAX_TOTAL_CHARS: int = Field(
            60000, description="Ceiling on the whole digest (~22K tok) handed back to the model."
        )
        TIMEOUT: int = Field(30, description="Per-source HTTP timeout (s).")
        TAVILY_API_KEY: str = Field(
            "", description="Only used in `query` mode (search-then-read). Mirror the tavily_search "
                            "key. Never committed."
        )
        TAVILY_SEARCH_DEPTH: str = Field(
            "basic", description="Discovery search depth for `query` mode: 'basic' (1 credit) or "
                                 "'advanced' (2). Reading is done by this tool, so 'basic' usually suffices."
        )
        # ---- over-search governor (Thread #2; shared cross-tool with tavily_search; query mode only) ----
        GOVERNOR_ENABLED: bool = Field(
            True, description="Over-search governor: cross-tool near-dup dedup + escalating read-nudge (query mode only; needs the injected chat_id)."
        )
        DEDUP_JACCARD: float = Field(
            0.8, description="Near-duplicate threshold (Jaccard token-set similarity vs prior searches this chat, across tavily+deep_research). Higher = more conservative."
        )
        READ_NUDGE_AFTER_K: int = Field(
            4, description="Soft read-nudge after this many combined searches (with URLs in hand); a firm 'stop searching' fires at 2x."
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = True

    @instrument("deep_research", "web")
    async def deep_research(
        self,
        query: Optional[str] = None,
        urls: Optional[list] = None,
        max_sources: int = 5,
        __event_emitter__=None,
        __chat_id__: str = "",
        __metadata__=None,
    ) -> str:
        """
        Research a topic across SEVERAL web pages in one step, reading them in parallel and returning
        a single consolidated digest to work from. Use this instead of reading pages one-by-one when
        you have (or can find) multiple sources for one question — e.g. several articles on a topic,
        a few candidate pages from a search, or links the user gave you. Provide EITHER `urls` (read
        exactly those) OR `query` (search the web, then read the top results). After it returns,
        synthesize the digest in your answer and cite the sources. For a single known page use
        read_page; for plain discovery use tavily_search.

        :param query: a research question/topic to search the web for, then read the top results (omit if you pass urls).
        :param urls: a list of absolute http(s) URLs to read in parallel (omit if you pass query).
        :param max_sources: how many sources to read (clamped to the tool's ceiling).
        """

        async def emit_status(desc, done=False):
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": desc, "done": done}})

        try:
            n = max(1, min(int(max_sources), self.valves.MAX_SOURCES))
        except (TypeError, ValueError):
            n = min(5, self.valves.MAX_SOURCES)

        urls = [u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()]
        query = (query or "").strip()
        if not urls and not query:
            return "Give me either a list of URLs to read or a research query to search for."

        # ---- over-search governor (shared cross-tool state; degrades off without an injected chat_id) ----
        # query mode = a governed web SEARCH (dedup + budget); urls mode = a READ (noted, never deduped).
        chat_id = __chat_id__ or (__metadata__ or {}).get("chat_id") or ""
        gov = _gov_state(chat_id) if (chat_id and self.valves.GOVERNOR_ENABLED) else None
        is_search = bool(query) and not urls
        if gov is not None:
            if is_search:
                dup_note = _gov_near_dup(gov, query, self.valves.DEDUP_JACCARD)
                if dup_note is not None:
                    log.info("deep_research governor: dedup chat=%s q=%r", chat_id, query[:80])
                    governor_event("dedup", "deep_research")
                    await emit_status("Near-duplicate research query — skipped (over-search guard).", done=True)
                    return dup_note
            elif urls:
                _gov_note_urls(gov, urls)  # urls-mode = reading; record that reading happened

        timeout = aiohttp.ClientTimeout(total=self.valves.TIMEOUT)
        connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": _UA}, connector=connector
        ) as session:
            # Discovery (query mode): one search to get the URLs, then read them.
            if not urls:
                await emit_status(f"Searching: {query}")
                found, err = await self._discover(session, query, n)
                if err:
                    return err
                if not found:
                    return (f"I searched for \"{query}\" but found no usable sources to read. "
                            "Try rephrasing, or give me specific URLs.")
                urls = found
                if gov is not None:
                    _gov_record_search(gov, query, found)  # count this discovery search + its URLs

            urls = urls[:n]
            await emit_status(f"Reading {len(urls)} sources in parallel…")

            sem = asyncio.Semaphore(max(1, self.valves.CONCURRENCY))

            async def one(u):
                async with sem:
                    return await self._fetch_render(session, u)

            results = await asyncio.gather(*[one(u) for u in urls], return_exceptions=True)

        # Assemble the digest; record failures explicitly (inability != absence).
        sections, failures, total = [], [], 0
        ok = 0
        for u, res in zip(urls, results):
            if isinstance(res, Exception):
                failures.append((u, "error while reading"))
                continue
            title, content, final_url, problem = res
            if problem:
                failures.append((final_url or u, problem))
                continue
            content = content.strip()
            if len(content) > self.valves.MAX_CHARS_PER_SOURCE:
                content = content[: self.valves.MAX_CHARS_PER_SOURCE].rstrip() + "…"
            if total + len(content) > self.valves.MAX_TOTAL_CHARS:
                content = content[: max(0, self.valves.MAX_TOTAL_CHARS - total)].rstrip() + "…"
            total += len(content)
            ok += 1
            head = f"## [{ok}] {title or final_url}\n{final_url}"
            sections.append(f"{head}\n\n{content}")
            if __event_emitter__:
                await __event_emitter__({
                    "type": "citation",
                    "data": {"document": [content],
                             "metadata": [{"source": final_url}],
                             "source": {"name": title or final_url, "url": final_url}},
                })
            if total >= self.valves.MAX_TOTAL_CHARS:
                break

        await emit_status(f"Read {ok} of {len(urls)} sources.", done=True)

        if not sections:
            why = "; ".join(f"{u} ({r})" for u, r in failures[:6]) or "no readable content"
            return (f"I couldn't read any of the sources — {why}. Treat this as a failure to access "
                    f"them, not as evidence the information doesn't exist.{self._escalation_hint(failures)}")

        header = (f"Research digest — read {ok} source(s)"
                  + (f" for \"{query}\"" if query else "") + ".")
        if failures:
            fl = "; ".join(f"{u} ({r})" for u, r in failures[:6])
            header += f" Could not read {len(failures)}: {fl}.{self._escalation_hint(failures)}"
        header += ("\n\nSynthesize across these sources and cite them; if they disagree, say so. "
                   "Each source's content is truncated — note where a source may be incomplete.")
        digest = header + "\n\n" + "\n\n---\n\n".join(sections)
        # Governor: escalating read-nudge after K combined searches (query mode = a search).
        if gov is not None and is_search:
            nudge = _gov_nudge(gov, self.valves.READ_NUDGE_AFTER_K)
            if nudge:
                digest += "\n" + nudge
                governor_event("read_nudge", "deep_research")
        return digest

    @staticmethod
    def _escalation_hint(failures):
        """Turn read-failure reasons into the correct next action — NOT 'search again' (which just
        triggers a reworded re-search loop). JS-gated -> the JS reader; access-blocked -> a different
        authoritative source."""
        reasons = [r for _, r in failures]
        if any("JavaScript" in r for r in reasons):
            return (" Some of these need JavaScript to show their content — open that specific page "
                    "with the JavaScript-capable reader rather than searching again.")
        if any(r.startswith(("HTTP 401", "HTTP 403", "HTTP 429")) or "blocked" in r for r in reasons):
            return (" Some of these are blocking automated access; re-running the search won't get "
                    "past that — read a different authoritative source for the value you need.")
        return ""

    # ---- discovery (query mode) ----------------------------------------------

    async def _discover(self, session, query, n):
        """Return (urls, None) or (None, model-readable error). Reuses the Tavily search REST call."""
        if not self.valves.TAVILY_API_KEY:
            return None, ("Query mode needs the Tavily key set in this tool's Valves. Either set it, "
                          "or pass me specific URLs to read instead.")
        depth = "advanced" if self.valves.TAVILY_SEARCH_DEPTH == "advanced" else "basic"
        payload = {"api_key": self.valves.TAVILY_API_KEY, "query": query,
                   "search_depth": depth, "max_results": max(1, min(n, 10))}
        try:
            async with session.post(_TAVILY_URL, json=payload) as resp:
                if resp.status == 401:
                    return None, "Web search auth failed (check the Tavily key in this tool's Valves)."
                if resp.status == 429:
                    return None, "Web search hit its rate/quota limit; try again later."
                if resp.status != 200:
                    return None, f"Web search failed (HTTP {resp.status})."
                data = await resp.json()
        except asyncio.TimeoutError:
            return None, f"Web search timed out after {self.valves.TIMEOUT}s."
        except (aiohttp.ClientError, ValueError) as e:
            log.warning("deep_research discover failed: %s", e)
            return None, "Web search failed (network error)."
        urls, seen = [], set()
        for r in (data.get("results") or []):
            u = (r.get("url") or "").strip()
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
        return urls[:n], None

    # ---- fetch + render one source (SSRF-guarded; mirrors read_page) ----------

    async def _fetch_render(self, session, url):
        """Return (title, content_markdown, final_url, problem|None). Never raises for expected
        failures — returns a `problem` string so the digest can report inability explicitly."""
        refusal = await self._url_refusal(url)
        if refusal:
            return "", "", url, "internal/blocked address"
        final_url = url
        try:
            for _hop in range(_MAX_REDIRECTS + 1):
                refusal = await self._url_refusal(url)
                if refusal:
                    return "", "", url, "redirected to a blocked address"
                async with session.get(url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            return "", "", url, "broken redirect"
                        url = urljoin(url, loc)
                        continue
                    final_url = str(resp.url)
                    status = resp.status
                    ctype = (resp.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
                    if status != 200:
                        return "", "", final_url, f"HTTP {status}"
                    if "html" not in ctype and not ctype.startswith("text/"):
                        return "", "", final_url, f"unreadable type ({ctype or 'unknown'})"
                    raw = b""
                    async for chunk in resp.content.iter_chunked(65536):
                        raw += chunk
                        if len(raw) >= 5_000_000:
                            break
                    break
            else:
                return "", "", url, "too many redirects"
        except asyncio.TimeoutError:
            return "", "", final_url, "timed out"
        except aiohttp.ClientError:
            return "", "", final_url, "network error"

        title, body = self._html_to_markdown(self._decode(raw), final_url)
        if len(body.strip()) < 200 and "html" in ctype:
            return title, "", final_url, "almost no readable text (likely JavaScript-rendered)"
        return title, body, final_url, None

    async def _url_refusal(self, url):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return "non-http(s)"
        host = parts.hostname
        if not host:
            return "no host"
        low = host.lower().rstrip(".")
        if low == "localhost" or low.endswith(".local") or low.endswith(".internal"):
            return "internal name"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, OSError):
            return "unresolvable host"
        for info in infos:
            addr = info[4][0].split("%")[0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                return "bad address"
            if ip in _CGNAT_V4 or not ip.is_global:
                return "non-public address"
        return None

    def _html_to_markdown(self, html, base_url=""):
        if BeautifulSoup is None or _AltOnlyMarkdown is None:
            stripped = re.sub(r"(?is)<(script|style|nav|footer|header|aside|noscript)[^>]*>.*?</\1>", " ", html or "")
            stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
            return "", self._collapse(unescape(stripped))
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(_SKIP_TAGS):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""
        conv = _AltOnlyMarkdown(heading_style="ATX")
        target = soup.find("article") or soup.find("main") or soup.body or soup
        md = self._collapse_md(conv.convert(str(target)))
        return title, _absolutize_links(md, base_url)

    @staticmethod
    def _decode(raw):
        for enc in ("utf-8", "latin-1"):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _collapse(text):
        lines = [ln.strip() for ln in text.splitlines()]
        text = "\n".join(ln for ln in lines if ln)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    @staticmethod
    def _collapse_md(text):
        text = "\n".join(ln.rstrip() for ln in text.splitlines())
        return re.sub(r"\n{3,}", "\n\n", text).strip()
