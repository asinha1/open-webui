"""
title: Tavily Web Search
author: mh-tools
version: 1.3.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — version-controlled in the open-webui fork under mh-tools/.
# Deployed into Open WebUI via Workspace -> Tools (stored in webui.db). Edit here, re-deploy via
# the tools-update API, then RESTART OWUI. Design rationale + acceptance: mh-tools/tavily_search.md.
#
# v1.3.0 (2026-07-23, RFC-MH-005 P1): LIB-BACKED. The search call + result shaping + the
#   over-search governor moved to the shared `mh_grounding` package — ONE copy at last (the
#   v1.2 block was mirrored byte-for-byte here and in deep_research.py; that mirror is gone).
#   The sys.modules shared store lives in mh_grounding.governor, so the cross-tool per-chat
#   budget + the eval harness's introspection point are unchanged. This file is now only the
#   OWUI surface: docstring, Valves, status/citation events, chat-id resolution, and the OTel
#   governor-event hook. Behavior + model-facing strings byte-identical to v1.2. NOTE: no
#   longer self-contained — needs `mh-grounding` installed in the OWUI venv; lib changes take
#   effect on OWUI restart, surface changes here still need the tools-update-API re-parse.
# v1.2.0: governor made CROSS-TOOL (sys.modules store shared with deep_research).
# v1.1.0: over-search governor (near-dup dedup + read-nudge).

from typing import Optional, Literal

from pydantic import BaseModel, Field

from open_webui.utils.telemetry.mh_tools import instrument, governor_event  # [mh] tool-usage metrics

from mh_grounding.governor import gov_state
from mh_grounding.tavily import TavilyConfig, search as tavily_search_core


class Tools:
    class Valves(BaseModel):
        # Field names mirror mh_grounding.tavily.TavilyConfig 1:1 — the body passes
        # TavilyConfig(**self.valves.model_dump()); keep them in lockstep.
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
        # ---- over-search governor (cross-tool with deep_research; needs the injected chat_id) ----
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

    @instrument("tavily_search", "web")
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

        # Over-search governor: shared per-chat state (degrades off without an injected chat_id).
        chat_id = __chat_id__ or (__metadata__ or {}).get("chat_id") or ""
        gov = gov_state(chat_id) if (chat_id and self.valves.GOVERNOR_ENABLED) else None

        result = await tavily_search_core(
            query,
            depth=depth,
            topic=topic,
            recency=recency,
            cfg=TavilyConfig(**self.valves.model_dump()),
            gov=gov,
            on_status=emit_status,
            on_gov_event=lambda kind: governor_event(kind, "tavily_search"),
        )

        # Emit citations (Source chips), indexed to match the [n] markers in the text.
        if __event_emitter__ and result.results:
            for i, r in enumerate(result.results, 1):
                await __event_emitter__({
                    "type": "citation",
                    "data": {
                        "document": [(r.get("content") or "")[: self.valves.MAX_CONTENT_CHARS]],
                        "metadata": [{"source": r.get("url", ""),
                                      "date_accessed": r.get("published_date", "")}],
                        "source": {"name": f"[{i}] {r.get('title', '')}", "url": r.get("url", "")},
                    },
                })

        return result.text
