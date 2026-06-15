"""
title: Tavily Web Search
author: mh-tools
version: 1.2.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — version-controlled in the open-webui fork under mh-tools/.
# Deployed into Open WebUI by creating it in Workspace -> Tools (stored in webui.db;
# this fork has no filesystem tools dir). Edit here, re-paste to update, then RESTART OWUI
# (model tool-binding is not reliably re-read live — restart OWUI after a re-paste).
# Design rationale + acceptance criteria: mh-tools/tavily_search.md.
#
# v1.1.0 (2026-06-12): over-search governor (Thread #2) — cross-call near-dup dedup + read-nudge.
# v1.2.0 (2026-06-12): governor made CROSS-TOOL. State moved to a process-global sys.modules
#   sentinel shared with deep_research, so ONE per-chat web-search budget + dedup set spans BOTH
#   tools (the v1.1 fix governed only tavily_search, and the model escaped the storm into the
#   ungoverned deep_research — probe 10c). The read-nudge now ESCALATES (soft at K, firm "stop
#   searching, synthesize/read" at 2K). The shared block below is MIRRORED byte-for-byte in
#   deep_research.py — keep the two copies in sync. Swap point for a future multi-worker/instance
#   deploy: back _gov_store() with OWUI's RedisDict (app.state.redis) — see composition-design.md.

import asyncio
import logging
import re
import sys
import types
from typing import Optional, Literal

import aiohttp
from pydantic import BaseModel, Field

log = logging.getLogger("mh.tavily_search")

TAVILY_URL = "https://api.tavily.com/search"
_DEPTH = {"quick": "basic", "deep": "advanced"}  # friendly names -> Tavily API values

# ===== over-search governor — SHARED in-process store (Thread #2, v1.2 cross-tool) ===========
# MIRRORED byte-for-byte in deep_research.py. OWUI DB-tools can't import a sibling module, so the
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
        TAVILY_API_KEY: str = Field(
            "", description="Tavily API key (tvly-...). Set in this tool's Valves; never committed."
        )
        # Sizing history (all 2026-05-30):
        #   5/1500 (orig) -> 3/800: a 4-search turn injected ~10K tokens and
        #     overflowed the then-16K slot. The guard is PER-RESULT and can't
        #     bound the aggregate across sibling calls, so we shrank per-call.
        #   3/800 -> 4/1500 (current): after --ctx-size went to 131072 (64K/slot)
        #     the budget reason for trimming was gone, and lived use showed 800
        #     chars truncated enumerations (model hedged "4+" for a real count
        #     of 9). Content restored to 1500 so lists survive; results kept at
        #     4 (not 5) to still drop the noise-ranked tail (fixtures/social).
        #     NB: Tavily bills per search DEPTH, not result count -- trimming
        #     saves no credits (deep = 2 either way), only context + latency.
        # See tavily_search.md "Trim rationale (2026-05-30)".
        MAX_RESULTS: int = Field(4, description="Results per search (clamped 1-10).")
        INCLUDE_ANSWER: bool = Field(
            True, description="Ask Tavily for a synthesized answer alongside the results."
        )
        MAX_CONTENT_CHARS: int = Field(
            1500, description="Truncate each result's content to this many chars (token-budget guard)."
        )
        TIMEOUT: int = Field(30, description="HTTP timeout in seconds.")
        # ---- over-search governor (Thread #2; cross-tool with deep_research; needs the injected chat_id) ----
        GOVERNOR_ENABLED: bool = Field(
            True, description="Over-search governor: cross-tool near-dup dedup + escalating read-nudge."
        )
        DEDUP_JACCARD: float = Field(
            0.8, description="Near-duplicate threshold (Jaccard token-set similarity vs prior searches this chat, across tavily+deep_research). Higher = more conservative."
        )
        READ_NUDGE_AFTER_K: int = Field(
            4, description="Soft read-nudge after this many combined searches (with URLs in hand); a firm 'stop searching' fires at 2x."
        )

    def __init__(self):
        self.valves = self.Valves()
        # We emit our own citation events (Source chips), so tell OWUI not to auto-wrap.
        self.citation = True

    async def tavily_search(
        self,
        query: str,
        depth: Literal["quick", "deep"] = "deep",
        topic: Literal["general", "news"] = "general",
        recency: Optional[Literal["day", "week", "month", "year"]] = None,
        __event_emitter__=None,
        __chat_id__: str = "",
        __metadata__=None,
    ) -> str:
        """
        Search the live web for current or post-training information — news, recent events,
        software/product versions, statistics, anything you may not know reliably from
        training. Do NOT use this for questions about the operator's own private or
        internal documents — use the knowledge base for those.

        :param query: the natural-language search query.
        :param depth: "deep" for research-grade synthesis (more context per source, slower, costs more); "quick" for a fast single-fact lookup.
        :param topic: "news" for current events (adds publish dates, biases toward recent reputable sources); "general" for everything else.
        :param recency: optional time window — "day", "week", "month", or "year". Use with topic="news" for "what happened this week"-style questions.
        """

        async def emit_status(desc, done=False):
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": desc, "done": done}}
                )

        if not self.valves.TAVILY_API_KEY:
            return "Web search is not configured (no Tavily API key set in the tool's Valves)."

        # ---- over-search governor: shared per-chat state (degrades off without an injected chat_id) ----
        chat_id = __chat_id__ or (__metadata__ or {}).get("chat_id") or ""
        gov = _gov_state(chat_id) if (chat_id and self.valves.GOVERNOR_ENABLED) else None
        if gov is not None:
            dup_note = _gov_near_dup(gov, query, self.valves.DEDUP_JACCARD)
            if dup_note is not None:
                log.info("tavily_search governor: dedup chat=%s q=%r", chat_id, query[:80])
                await emit_status("Near-duplicate search — skipped (over-search guard).", done=True)
                return dup_note

        payload = {
            "api_key": self.valves.TAVILY_API_KEY,
            "query": query,
            "search_depth": _DEPTH.get(depth, "advanced"),
            "topic": topic,
            "include_answer": self.valves.INCLUDE_ANSWER,
            "max_results": max(1, min(self.valves.MAX_RESULTS, 10)),
            "include_usage": True,
        }
        if depth == "deep":
            payload["chunks_per_source"] = 3  # advanced-only: more semantic snippets per source
        if recency:
            payload["time_range"] = recency

        await emit_status(f"Searching the web ({depth}): {query}")
        try:
            timeout = aiohttp.ClientTimeout(total=self.valves.TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(TAVILY_URL, json=payload) as resp:
                    if resp.status == 401:
                        await emit_status("Web search unavailable (auth).", done=True)
                        return ("Web search is unavailable (authentication failed). Answer from your "
                                "own knowledge if you can, and tell the user the web lookup failed.")
                    if resp.status == 429:
                        await emit_status("Web search quota exhausted.", done=True)
                        return ("Web search quota is exhausted for now. Answer from your own knowledge "
                                "if you can, and tell the user the web lookup was unavailable.")
                    resp.raise_for_status()
                    data = await resp.json()
        except asyncio.TimeoutError:
            await emit_status("Web search timed out.", done=True)
            return (f"Web search timed out after {self.valves.TIMEOUT}s. Try a narrower query or "
                    "answer from your own knowledge.")
        except aiohttp.ClientError as e:
            log.warning("tavily_search network error: %s", e)
            await emit_status("Web search failed (network).", done=True)
            return f"Web search failed (network error). Answer from your own knowledge if you can."

        results = data.get("results", []) or []

        # Governor: record this real search (query + result URLs) into the shared per-chat state.
        if gov is not None:
            _gov_record_search(gov, query, [r.get("url", "") for r in results])

        # Quota/cost telemetry -> open-webui.err.log. Tally burn with: grep 'omni.tavily_search'.
        log.info(
            "tavily_search depth=%s topic=%s recency=%s results=%d usage=%s q=%r",
            depth, topic, recency, len(results), data.get("usage") or {}, query[:80],
        )

        # Emit citations (Source chips), indexed to match the [n] markers in the text below.
        if __event_emitter__:
            for i, r in enumerate(results, 1):
                await __event_emitter__({
                    "type": "citation",
                    "data": {
                        "document": [(r.get("content") or "")[: self.valves.MAX_CONTENT_CHARS]],
                        "metadata": [{"source": r.get("url", ""),
                                      "date_accessed": r.get("published_date", "")}],
                        "source": {"name": f"[{i}] {r.get('title', '')}", "url": r.get("url", "")},
                    },
                })

        out = []
        if data.get("answer"):
            out.append(f"Web answer: {data['answer']}\n")
        if not results:
            out.append("No web results.")
        for i, r in enumerate(results, 1):
            content = (r.get("content") or "").strip()
            if len(content) > self.valves.MAX_CONTENT_CHARS:
                content = content[: self.valves.MAX_CONTENT_CHARS].rstrip() + "…"
            block = [f"[{i}] {r.get('title', '(untitled)')} — {r.get('url', '')}"]
            if r.get("published_date"):
                block.append(f"    published: {r['published_date']}")
            block.append(f"    {content}")
            out.append("\n".join(block))

        # Governor: escalating read-nudge after K combined searches with URLs (appended, not a block).
        if gov is not None:
            nudge = _gov_nudge(gov, self.valves.READ_NUDGE_AFTER_K)
            if nudge:
                out.append(nudge)

        await emit_status(f"Found {len(results)} result(s).", done=True)
        return "\n\n".join(out)
