"""mh_grounding.research — the deep_research v1.2/1.3 core (parallel multi-source digest).

Extracted 1:1 from mh-tools/deep_research.py (RFC-MH-005 P2). Model-facing digest strings
byte-identical; logger stays "mh.deep_research". Two modes: `urls` (read exactly those, in
parallel) or `query` (one Tavily discovery search, then read the top results in parallel).
The parallelism is tool-I/O, not inference — one digest, one synthesis pass.

This module deliberately owns its OWN fetch/SSRF surface (`_fetch_render`/`_url_refusal`):
they return short problem REASONS for the digest's failure list ("internal/blocked address",
"HTTP 403", …), a different surface from fetch.py's model-facing read_page refusal strings.
The markdown/decode/UA/TLS plumbing is imported from fetch.py — single copy.

Governor interplay mirrors tavily.py: the caller resolves the session key and passes the
state in; query-mode is a governed SEARCH (dedup + budget), urls-mode is a READ (noted,
never deduped). `on_gov_event(kind)` is the per-process metrics hook; `on_source(title,
content, final_url)` fires per assembled source (the OWUI adapter emits citations from it).
"""

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp

from .fetch import (
    SSL_CONTEXT as _SSL_CONTEXT,
    UA as _UA,
    decode_bytes as _decode,
    html_to_markdown as _html_to_markdown,
)
from .governor import gov_near_dup, gov_note_urls, gov_nudge, gov_record_search

log = logging.getLogger("mh.deep_research")

_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_MAX_REDIRECTS = 5
_TAVILY_URL = "https://api.tavily.com/search"


@dataclass
class ResearchConfig:
    """Mirrors mh-tools/deep_research.py Valves — names AND defaults."""
    MAX_SOURCES: int = 8
    CONCURRENCY: int = 5
    MAX_CHARS_PER_SOURCE: int = 10000
    MAX_TOTAL_CHARS: int = 60000
    TIMEOUT: int = 30
    TAVILY_API_KEY: str = ""
    TAVILY_SEARCH_DEPTH: str = "basic"
    GOVERNOR_ENABLED: bool = True
    DEDUP_JACCARD: float = 0.8
    READ_NUDGE_AFTER_K: int = 4


@dataclass
class ResearchResult:
    """`text` is the complete model-facing digest (or refusal/dedup note). `sources` carries
    the assembled (title, content, final_url) tuples in section order for citation emit;
    `skipped` marks a governor dedup (nothing was fetched)."""
    text: str
    sources: list = field(default_factory=list)
    skipped: bool = False


async def _noop_status(desc, done=False):
    return None


def _noop_gov_event(kind):
    return None


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


async def _url_refusal(url):
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


async def _discover(session, query, n, cfg: ResearchConfig):
    """Return (urls, None) or (None, model-readable error). Reuses the Tavily search REST call."""
    if not cfg.TAVILY_API_KEY:
        return None, ("Query mode needs the Tavily key set in this tool's Valves. Either set it, "
                      "or pass me specific URLs to read instead.")
    depth = "advanced" if cfg.TAVILY_SEARCH_DEPTH == "advanced" else "basic"
    payload = {"api_key": cfg.TAVILY_API_KEY, "query": query,
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
        return None, f"Web search timed out after {cfg.TIMEOUT}s."
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


async def _fetch_render(session, url):
    """Return (title, content_markdown, final_url, problem|None). Never raises for expected
    failures — returns a `problem` string so the digest can report inability explicitly."""
    refusal = await _url_refusal(url)
    if refusal:
        return "", "", url, "internal/blocked address"
    final_url = url
    try:
        for _hop in range(_MAX_REDIRECTS + 1):
            refusal = await _url_refusal(url)
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

    title, body = _html_to_markdown(_decode(raw), "", final_url)
    if len(body.strip()) < 200 and "html" in ctype:
        return title, "", final_url, "almost no readable text (likely JavaScript-rendered)"
    return title, body, final_url, None


async def research(query: Optional[str] = None,
                   urls: Optional[list] = None,
                   max_sources: int = 5,
                   cfg: ResearchConfig = None,
                   gov=None,
                   on_status=None,
                   on_gov_event=None,
                   on_source=None) -> ResearchResult:
    """The full deep_research flow. `gov` is the per-session governor state dict (or None to
    run ungoverned — the caller applies GOVERNOR_ENABLED + session-key resolution)."""
    cfg = cfg or ResearchConfig()
    emit_status = on_status or _noop_status
    gov_event = on_gov_event or _noop_gov_event

    try:
        n = max(1, min(int(max_sources), cfg.MAX_SOURCES))
    except (TypeError, ValueError):
        n = min(5, cfg.MAX_SOURCES)

    urls = [u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()]
    query = (query or "").strip()
    if not urls and not query:
        return ResearchResult("Give me either a list of URLs to read or a research query to search for.")

    # query mode = a governed web SEARCH (dedup + budget); urls mode = a READ (noted, never deduped).
    is_search = bool(query) and not urls
    if gov is not None:
        if is_search:
            dup_note = gov_near_dup(gov, query, cfg.DEDUP_JACCARD)
            if dup_note is not None:
                log.info("deep_research governor: dedup q=%r", query[:80])
                gov_event("dedup")
                await emit_status("Near-duplicate research query — skipped (over-search guard).", done=True)
                return ResearchResult(dup_note, skipped=True)
        elif urls:
            gov_note_urls(gov, urls)  # urls-mode = reading; record that reading happened

    timeout = aiohttp.ClientTimeout(total=cfg.TIMEOUT)
    connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": _UA}, connector=connector
    ) as session:
        # Discovery (query mode): one search to get the URLs, then read them.
        if not urls:
            await emit_status(f"Searching: {query}")
            found, err = await _discover(session, query, n, cfg)
            if err:
                return ResearchResult(err)
            if not found:
                return ResearchResult(
                    f"I searched for \"{query}\" but found no usable sources to read. "
                    "Try rephrasing, or give me specific URLs.")
            urls = found
            if gov is not None:
                gov_record_search(gov, query, found)  # count this discovery search + its URLs

        urls = urls[:n]
        await emit_status(f"Reading {len(urls)} sources in parallel…")

        sem = asyncio.Semaphore(max(1, cfg.CONCURRENCY))

        async def one(u):
            async with sem:
                return await _fetch_render(session, u)

        results = await asyncio.gather(*[one(u) for u in urls], return_exceptions=True)

    # Assemble the digest; record failures explicitly (inability != absence).
    sections, failures, total = [], [], 0
    sources = []
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
        if len(content) > cfg.MAX_CHARS_PER_SOURCE:
            content = content[: cfg.MAX_CHARS_PER_SOURCE].rstrip() + "…"
        if total + len(content) > cfg.MAX_TOTAL_CHARS:
            content = content[: max(0, cfg.MAX_TOTAL_CHARS - total)].rstrip() + "…"
        total += len(content)
        ok += 1
        head = f"## [{ok}] {title or final_url}\n{final_url}"
        sections.append(f"{head}\n\n{content}")
        sources.append((title, content, final_url))
        if on_source:
            await on_source(title, content, final_url)
        if total >= cfg.MAX_TOTAL_CHARS:
            break

    await emit_status(f"Read {ok} of {len(urls)} sources.", done=True)

    if not sections:
        why = "; ".join(f"{u} ({r})" for u, r in failures[:6]) or "no readable content"
        return ResearchResult(
            f"I couldn't read any of the sources — {why}. Treat this as a failure to access "
            f"them, not as evidence the information doesn't exist.{_escalation_hint(failures)}")

    header = (f"Research digest — read {ok} source(s)"
              + (f" for \"{query}\"" if query else "") + ".")
    if failures:
        fl = "; ".join(f"{u} ({r})" for u, r in failures[:6])
        header += f" Could not read {len(failures)}: {fl}.{_escalation_hint(failures)}"
    header += ("\n\nSynthesize across these sources and cite them; if they disagree, say so. "
               "Each source's content is truncated — note where a source may be incomplete.")
    digest = header + "\n\n" + "\n\n---\n\n".join(sections)
    # Governor: escalating read-nudge after K combined searches (query mode = a search).
    if gov is not None and is_search:
        nudge = gov_nudge(gov, cfg.READ_NUDGE_AFTER_K)
        if nudge:
            digest += "\n" + nudge
            gov_event("read_nudge")
    return ResearchResult(digest, sources=sources)
