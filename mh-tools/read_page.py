"""
title: Read Page
author: mh-tools
version: 1.5.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — version-controlled in the open-webui fork under mh-tools/.
# Companion to tavily_search: search FINDS pages (snippets); read_page OPENS one (full body) —
# escalating to a headless-Chromium render INTERNALLY for JS-gated pages (the v1.4 fold).
# Deployed into Open WebUI via Workspace -> Tools (stored in webui.db). Edit here, re-deploy via
# the tools-update API, then RESTART OWUI. Design rationale + acceptance: mh-tools/read_page.md.
#
# v1.5.0 (2026-07-23, RFC-MH-005 P1): LIB-BACKED. The entire fetch/render/SSRF/escalation core
#   moved to the shared `mh_grounding` package (installed editable in the OWUI venv) — the SAME
#   code the mh-mcp agent server runs, so the two surfaces cannot drift. This file is now only
#   the OWUI surface: the model-facing docstring, Valves, status events, and the citation emit.
#   Behavior and model-facing strings are byte-identical to v1.4 (FetchConfig mirrors the Valves
#   1:1; the return text IS FetchResult.text). NOTE: no longer self-contained — a fresh provision
#   must `uv pip install -e mh-grounding` into the OWUI venv BEFORE this tool works.
#   Lib-logic changes now take effect on OWUI RESTART (no DB re-deploy needed); only changes to
#   THIS file (docstring/Valves/params) still need the tools-update-API re-parse.
# v1.4: render_page fold-in (JS-render escalation). v1.3: certifi TLS + retrieval-failure marker.
# v1.2: markdownify render, meta-refresh/JS redirects, #fragment focus, RSS/Atom enumeration.

from typing import Optional

from pydantic import BaseModel, Field

from open_webui.utils.telemetry.mh_tools import instrument  # [mh] tool-usage metrics

from mh_grounding.fetch import FetchConfig, read_url


class Tools:
    class Valves(BaseModel):
        # Field names mirror mh_grounding.fetch.FetchConfig 1:1 — the body passes
        # FetchConfig(**self.valves.model_dump()); keep them in lockstep.
        MAX_CONTENT_CHARS: int = Field(
            60000, description="Ceiling on returned chars (~22K tok) — sized to hold a full long "
                               "article. The max_chars arg clamps to this. HTML is rendered to "
                               "Markdown; feeds are parsed to a compact item list."
        )
        MAX_FEED_ITEMS: int = Field(
            500, description="Max items enumerated from an RSS/Atom feed (covers a 140-episode podcast; "
                             "guards against a pathologically huge feed)."
        )
        MAX_FETCH_BYTES: int = Field(
            5_000_000, description="Stop reading the response body past this many bytes (don't OOM on a huge file)."
        )
        MIN_READABLE_CHARS: int = Field(
            200, description="If an HTML page renders fewer than this many chars of text, treat it as a "
                             "retrieval FAILURE (JS-gated / login-walled / interstitial) and tell the model "
                             "it could not ACCESS the page — never let it infer the content doesn't exist."
        )
        TIMEOUT: int = Field(30, description="HTTP timeout in seconds.")
        USE_TAVILY_EXTRACT: bool = Field(
            False, description="If a JS-heavy page returns near-empty text, fall back to Tavily Extract "
                              "(costs credits, reuses TAVILY_API_KEY). Off by default — direct fetch handles RSS/JSON."
        )
        TAVILY_API_KEY: str = Field(
            "", description="Only read when USE_TAVILY_EXTRACT is on; mirror the tavily_search key. Never committed."
        )
        RENDER_ESCALATION: bool = Field(
            True, description="When a fetched HTML page is near-empty (JS-gated), escalate to a "
                              "headless-Chromium render internally. Off = v1.3 behavior (emit the "
                              "couldn't-access marker). Auto-off if playwright is absent."
        )
        RENDER_TIMEOUT: int = Field(
            35, description="Headless-render navigation timeout (s). The render is slow; keep generous. "
                            "Separate from the plain-fetch TIMEOUT above."
        )
        RENDER_NETWORKIDLE_MS: int = Field(
            6000, description="After DOM load, wait up to this long for the network to go idle (lets "
                              "client-side content inject) before reading the rendered page."
        )
        RENDER_BLOCK_MEDIA: bool = Field(
            True, description="During a render, abort image/media/font requests (faster render, smaller "
                              "SSRF surface)."
        )

    def __init__(self):
        self.valves = self.Valves()
        # We emit our own citation event (Source chip), so tell OWUI not to auto-wrap.
        self.citation = True

    @instrument("read_page", "web")
    async def read_page(
        self,
        url: str,
        max_chars: Optional[int] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Read a specific web page or feed when you already have its URL — e.g. a page a search
        surfaced, a link the user gave you, or a podcast/blog RSS feed (which lists ALL items,
        unlike a search snippet). Returns the page as Markdown with links preserved, so you can
        follow a link by calling read_page again on it. Do NOT use this to search the web for a
        topic — use tavily_search for discovery. For an exhaustive list (every podcast episode,
        every post), fetch the site's RSS/Atom feed; if you only have an Apple Podcasts id, first
        read "https://itunes.apple.com/lookup?id=<id>" to get its feedUrl, then read that feed.

        :param url: the absolute http(s) URL to fetch. A #fragment focuses on that section of the page.
        :param max_chars: optional cap on returned characters for this call (raise it for a long enumeration like a full episode list). Clamped to the tool's ceiling.
        """

        async def emit_status(desc, done=False):
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": desc, "done": done}}
                )

        result = await read_url(
            url,
            max_chars=max_chars,
            cfg=FetchConfig(**self.valves.model_dump()),
            on_status=emit_status,
        )

        if result.ok and __event_emitter__:
            await __event_emitter__({
                "type": "citation",
                "data": {
                    "document": [result.body],
                    "metadata": [{"source": result.final_url}],
                    "source": {"name": result.title or result.final_url, "url": result.final_url},
                },
            })

        return result.text
