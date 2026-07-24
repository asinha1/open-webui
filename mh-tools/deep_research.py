"""
title: Deep Research
author: mh-tools
version: 1.3.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — version-controlled in the open-webui fork under mh-tools/.
# Reads SEVERAL sources at once and returns one consolidated digest, so the model can research a
# topic across many pages in a single turn instead of reading them one-by-one.
#
# THE PARALLELISM IS TOOL-I/O, NOT INFERENCE. The conversation runs on one llama-server slot
# (--parallel 1, deliberate). You cannot run multiple Gemma generations at once — but you CAN fetch
# N pages concurrently (I/O-bound) and hand the model ONE digest to synthesize in a single pass.
#
# Two modes: pass `urls` (read exactly those, in parallel) or `query` (search the web, then read the
# top results in parallel). Deployed via Workspace -> Tools, then RESTART OWUI. Design + acceptance:
# mh-tools/deep_research.md.
#
# v1.3.0 (2026-07-23, RFC-MH-005 P1): LIB-BACKED. The mirrored governor block is GONE — the single
#   copy lives in mh_grounding.governor (same sys.modules store, so cross-tool state + the eval
#   harness's introspection point are unchanged). The markdown/decode/UA/TLS plumbing now imports
#   from mh_grounding.fetch. This tool KEEPS its own _fetch_render/_url_refusal: they return short
#   problem REASONS for the digest's failure list (a deliberately different surface from read_page's
#   model-facing refusal strings). Behavior + strings byte-identical to v1.2. NOTE: no longer
#   self-contained — needs `mh-grounding` installed in the OWUI venv; lib changes take effect on
#   OWUI restart, surface changes here still need the tools-update-API re-parse.
# v1.2.0: joined the CROSS-TOOL over-search governor (query mode = a governed search; urls mode =
#   a read, noted never deduped). v1.1.0: failure routing (JS-gated -> the JS reader; blocked ->
#   different source), part of the v10 over-search/under-read fix.

import asyncio
import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
from pydantic import BaseModel, Field

from open_webui.utils.telemetry.mh_tools import instrument, governor_event  # [mh] tool-usage metrics

from mh_grounding.fetch import (
    SSL_CONTEXT as _SSL_CONTEXT,
    UA as _UA,
    decode_bytes as _decode,
    html_to_markdown as _html_to_markdown,
)
from mh_grounding.governor import (
    gov_near_dup as _gov_near_dup,
    gov_note_urls as _gov_note_urls,
    gov_nudge as _gov_nudge,
    gov_record_search as _gov_record_search,
    gov_state as _gov_state,
)

log = logging.getLogger("mh.deep_research")

_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_MAX_REDIRECTS = 5
_TAVILY_URL = "https://api.tavily.com/search"


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
        # ---- over-search governor (shared cross-tool with tavily_search; query mode only) ----
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

    # ---- fetch + render one source (SSRF-guarded; digest-surface semantics) ----
    # KEPT LOCAL on purpose: returns short problem REASONS for the failure list (e.g.
    # "internal/blocked address"), not read_page's model-facing refusal strings. The
    # markdown/decode plumbing is the lib's; only the flow + reason strings live here.

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

        title, body = _html_to_markdown(_decode(raw), "", final_url)
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
