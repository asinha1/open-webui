"""mh_grounding.tavily — the tavily_search v1.2 core (search + governor interplay).

Extracted 1:1 from mh-tools/tavily_search.py v1.2 (RFC-MH-005 P1). Model-facing strings
and log messages byte-identical; logger stays "mh.tavily_search" for grep continuity
(quota telemetry: grep 'tavily_search depth=' for usage/burn).

Governor interplay lives HERE (single copy, identical behavior for every client). The
caller resolves the session key (OWUI: __chat_id__; mh-mcp: the MCP session id) and
passes the governor state in; `on_gov_event(kind)` is the per-process metrics hook
(OWUI: the fork's OTel governor_event; mh-mcp: mh_grounding.metrics).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .governor import gov_near_dup, gov_nudge, gov_record_search

log = logging.getLogger("mh.tavily_search")

TAVILY_URL = "https://api.tavily.com/search"
_DEPTH = {"quick": "basic", "deep": "advanced"}  # friendly names -> Tavily API values


@dataclass
class TavilyConfig:
    """Mirrors mh-tools/tavily_search.py Valves — names AND defaults. The sizing history
    (5/1500 -> 3/800 -> 4/1500) is documented on the Valves; don't re-derive here."""
    TAVILY_API_KEY: str = ""
    MAX_RESULTS: int = 4
    INCLUDE_ANSWER: bool = True
    MAX_CONTENT_CHARS: int = 1500
    TIMEOUT: int = 30
    GOVERNOR_ENABLED: bool = True
    DEDUP_JACCARD: float = 0.8
    READ_NUDGE_AFTER_K: int = 4


@dataclass
class TavilyResult:
    """`text` is the complete model-facing return. `results` carries the raw result dicts
    (url/title/content/published_date) so the OWUI adapter can emit citation events;
    `skipped` marks a governor dedup (no search was run)."""
    text: str
    results: list = field(default_factory=list)
    skipped: bool = False


async def _noop_status(desc, done=False):
    return None


def _noop_gov_event(kind):
    return None


async def search(query: str,
                 depth: str = "deep",
                 topic: str = "general",
                 recency: Optional[str] = None,
                 cfg: TavilyConfig = None,
                 gov=None,
                 on_status=None,
                 on_gov_event=None) -> TavilyResult:
    """One Tavily search, governor-checked. `gov` is the per-session governor state dict
    (from mh_grounding.governor.gov_state) or None to run ungoverned (the caller applies
    the GOVERNOR_ENABLED valve + session-key resolution)."""
    cfg = cfg or TavilyConfig()
    emit_status = on_status or _noop_status
    gov_event = on_gov_event or _noop_gov_event

    if not cfg.TAVILY_API_KEY:
        return TavilyResult(
            "Web search is not configured (no Tavily API key set in the tool's Valves).")

    if gov is not None:
        dup_note = gov_near_dup(gov, query, cfg.DEDUP_JACCARD)
        if dup_note is not None:
            log.info("tavily_search governor: dedup q=%r", query[:80])
            gov_event("dedup")
            await emit_status("Near-duplicate search — skipped (over-search guard).", done=True)
            return TavilyResult(dup_note, skipped=True)

    payload = {
        "api_key": cfg.TAVILY_API_KEY,
        "query": query,
        "search_depth": _DEPTH.get(depth, "advanced"),
        "topic": topic,
        "include_answer": cfg.INCLUDE_ANSWER,
        "max_results": max(1, min(cfg.MAX_RESULTS, 10)),
        "include_usage": True,
    }
    if depth == "deep":
        payload["chunks_per_source"] = 3  # advanced-only: more semantic snippets per source
    if recency:
        payload["time_range"] = recency

    await emit_status(f"Searching the web ({depth}): {query}")
    try:
        timeout = aiohttp.ClientTimeout(total=cfg.TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(TAVILY_URL, json=payload) as resp:
                if resp.status == 401:
                    await emit_status("Web search unavailable (auth).", done=True)
                    return TavilyResult(
                        "Web search is unavailable (authentication failed). Answer from your "
                        "own knowledge if you can, and tell the user the web lookup failed.")
                if resp.status == 429:
                    await emit_status("Web search quota exhausted.", done=True)
                    return TavilyResult(
                        "Web search quota is exhausted for now. Answer from your own knowledge "
                        "if you can, and tell the user the web lookup was unavailable.")
                resp.raise_for_status()
                data = await resp.json()
    except asyncio.TimeoutError:
        await emit_status("Web search timed out.", done=True)
        return TavilyResult(
            f"Web search timed out after {cfg.TIMEOUT}s. Try a narrower query or "
            "answer from your own knowledge.")
    except aiohttp.ClientError as e:
        log.warning("tavily_search network error: %s", e)
        await emit_status("Web search failed (network).", done=True)
        return TavilyResult(
            "Web search failed (network error). Answer from your own knowledge if you can.")

    results = data.get("results", []) or []

    # Governor: record this real search (query + result URLs) into the shared per-session state.
    if gov is not None:
        gov_record_search(gov, query, [r.get("url", "") for r in results])

    # Quota/cost telemetry -> the service log. Tally burn with: grep 'tavily_search depth='.
    log.info(
        "tavily_search depth=%s topic=%s recency=%s results=%d usage=%s q=%r",
        depth, topic, recency, len(results), data.get("usage") or {}, query[:80],
    )

    out = []
    if data.get("answer"):
        out.append(f"Web answer: {data['answer']}\n")
    if not results:
        out.append("No web results.")
    for i, r in enumerate(results, 1):
        content = (r.get("content") or "").strip()
        if len(content) > cfg.MAX_CONTENT_CHARS:
            content = content[: cfg.MAX_CONTENT_CHARS].rstrip() + "…"
        block = [f"[{i}] {r.get('title', '(untitled)')} — {r.get('url', '')}"]
        if r.get("published_date"):
            block.append(f"    published: {r['published_date']}")
        block.append(f"    {content}")
        out.append("\n".join(block))

    # Governor: escalating read-nudge after K combined searches with URLs (appended, not a block).
    if gov is not None:
        nudge = gov_nudge(gov, cfg.READ_NUDGE_AFTER_K)
        if nudge:
            out.append(nudge)
            gov_event("read_nudge")

    await emit_status(f"Found {len(results)} result(s).", done=True)
    return TavilyResult("\n\n".join(out), results=results)
