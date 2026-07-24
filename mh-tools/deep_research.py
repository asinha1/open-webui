"""
title: Deep Research
author: mh-tools
version: 1.4.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — version-controlled in the open-webui fork under mh-tools/.
# Reads SEVERAL sources at once and returns one consolidated digest (parallel tool-I/O,
# single-pass synthesis). Deployed via the tools-update API (`mh-tools/redeploy.py`), then
# RESTART OWUI. Design + acceptance: mh-tools/deep_research.md.
#
# v1.4.0 (2026-07-24, RFC-MH-005 P2): FULLY LIB-BACKED. The digest orchestration (discovery,
#   parallel fetch_render with the digest-surface short-reason SSRF semantics, assembly,
#   escalation hints, governor interplay) moved to mh_grounding.research — the SAME core the
#   mh-mcp agent server now serves. This file is only the OWUI surface: docstring, Valves,
#   status/citation events, chat-id resolution, OTel governor-event hook. Behavior + digest
#   strings byte-identical to v1.3.
# v1.3.0: governor + markdown plumbing from mh_grounding. v1.2.0: cross-tool governor.
# v1.1.0: failure routing (JS-gated -> the JS reader; blocked -> different source).

from typing import Optional

from pydantic import BaseModel, Field

from open_webui.utils.telemetry.mh_tools import instrument, governor_event  # [mh] tool-usage metrics

from mh_grounding.governor import gov_state
from mh_grounding.research import ResearchConfig, research


class Tools:
    class Valves(BaseModel):
        # Field names mirror mh_grounding.research.ResearchConfig 1:1 — the body passes
        # ResearchConfig(**self.valves.model_dump()); keep them in lockstep.
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

        async def emit_citation(title, content, final_url):
            if __event_emitter__:
                await __event_emitter__({
                    "type": "citation",
                    "data": {"document": [content],
                             "metadata": [{"source": final_url}],
                             "source": {"name": title or final_url, "url": final_url}},
                })

        # Over-search governor: shared per-chat state (degrades off without an injected chat_id).
        chat_id = __chat_id__ or (__metadata__ or {}).get("chat_id") or ""
        gov = gov_state(chat_id) if (chat_id and self.valves.GOVERNOR_ENABLED) else None

        result = await research(
            query=query,
            urls=urls,
            max_sources=max_sources,
            cfg=ResearchConfig(**self.valves.model_dump()),
            gov=gov,
            on_status=emit_status,
            on_gov_event=lambda kind: governor_event(kind, "deep_research"),
            on_source=emit_citation,
        )
        return result.text
