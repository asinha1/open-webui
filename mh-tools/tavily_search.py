"""
title: Tavily Web Search
author: mh-tools
version: 1.0.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — version-controlled in the open-webui fork under mh-tools/.
# Deployed into Open WebUI by creating it in Workspace -> Tools (stored in webui.db;
# this fork has no filesystem tools dir). Edit here, re-paste to update, then RESTART OWUI
# (model tool-binding is not reliably re-read live — restart OWUI after a re-paste).
# Design rationale + acceptance criteria: mh-tools/tavily_search.md.

import asyncio
import logging
from typing import Optional, Literal

import aiohttp
from pydantic import BaseModel, Field

log = logging.getLogger("mh.tavily_search")

TAVILY_URL = "https://api.tavily.com/search"
_DEPTH = {"quick": "basic", "deep": "advanced"}  # friendly names -> Tavily API values


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

        await emit_status(f"Found {len(results)} result(s).", done=True)
        return "\n\n".join(out)
