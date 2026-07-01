"""
title: Research Search
author: mh-tools
version: 1.0.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — the READ side of the self-generating RAG (RFC-MH-002,
# provisioning/hephaestus/tooling-research/self-generating-rag-design.md §3b). Searches the user's
# OWN saved research: the `research:<domain>/<subtopic>` Knowledge collections written by the
# "Save sources" Action (save_research.py). DELIBERATELY SEPARATE from knowledge_search (the
# home-network KB) so that tool's narrow L1 docstring + K1–K4 validation stay intact. ⚠️ Long-term
# OPEN (operator 2026-06-15): possibly unify back into knowledge_search post-validation.
#
# Mirrors knowledge_search's proven shape: reuses OWUI's exact retrieval
# (open_webui.retrieval.utils.query_collection — same MiniLM + Chroma), a CRAG relevance grade
# (>= RELEVANCE_THRESHOLD → actionable-empty, never a bare []), and a per-TURN governor (dedup +
# empty-cap) on the shared sys.modules sentinel under its OWN scope (RCHAT) so research / knowledge /
# web searches never cross-dedup. Surfaces each hit's SOURCE URL + SAVED_AT so the model cites and
# can flag age (saved pages are snapshots — the temporal-bypass / freshness contract, §6).
# Validation is IN-OWUI only (needs app.state). UPGRADE-CHECK: query_collection +
# get_knowledge_bases_by_user_id are OWUI internals — re-verify on bumps (reference/upgrades.md).

import logging
import re
import sys
import types

from pydantic import BaseModel, Field

from open_webui.utils.telemetry.mh_tools import instrument  # [mh] tool-usage metrics

log = logging.getLogger("mh.research_search")

# ===== research over-search governor — SEPARATE scope (RCHAT) on the shared sentinel ============
_RGOV_MAX_TURNS = 400
_RGOV_STOPWORDS = frozenset(
    "the a an of to in for on at and or vs with from by is are be as what which how when "
    "where who whom my our your do i need about tell me explain saved save research".split()
)
_RGOV_NUM_RE = re.compile(
    r"[\$£€]?\d[\d,\.]*\s*[kKmM]?(?:\s*(?:\.\.|-|to)\s*[\$£€]?\d[\d,\.]*\s*[kKmM]?)?"
)


def _rgov_store():
    m = sys.modules.get("_mh_governor_store")
    if m is None:
        m = types.ModuleType("_mh_governor_store")
        m.CHAT = {}
        m.ORDER = []
        sys.modules["_mh_governor_store"] = m
    if not hasattr(m, "RCHAT"):
        m.RCHAT = {}    # (chat_id, message_id) -> {"norm":[frozenset], "raw":[str], "empties":int}
        m.RORDER = []
    return m


def _rgov_normalize(q):
    q = (q or "").lower().replace('"', " ").replace("'", " ")
    q = _RGOV_NUM_RE.sub(" <num> ", q)
    toks = set()
    for raw in q.split():
        t = raw.strip(".,;()[]{}!?")
        if t and t not in _RGOV_STOPWORDS:
            toks.add(t)
    return frozenset(toks)


def _rgov_jaccard(a, b):
    return (len(a & b) / len(a | b)) if (a and b) else 0.0


def _rgov_state(turn_key):
    store = _rgov_store()
    st = store.RCHAT.get(turn_key)
    if st is None:
        st = {"norm": [], "raw": [], "empties": 0}
        store.RCHAT[turn_key] = st
        store.RORDER.append(turn_key)
        while len(store.RORDER) > _RGOV_MAX_TURNS:
            store.RCHAT.pop(store.RORDER.pop(0), None)
    return st


def _rgov_near_dup(st, query, threshold):
    nq = _rgov_normalize(query)
    best, best_raw = 0.0, None
    for pn, pr in zip(st["norm"], st["raw"]):
        j = _rgov_jaccard(nq, pn)
        if j > best:
            best, best_raw = j, pr
    if best >= threshold and best_raw is not None:
        return (f"[research guard] Skipped — nearly identical to your earlier research search "
                f"«{best_raw}» (similarity {best:.0%}) which found nothing. Don't re-query; answer from "
                f"what you have or use web search.")
    return None


def _rgov_record(st, query, n_hits):
    st["norm"].append(_rgov_normalize(query))
    st["raw"].append(query)
    if n_hits == 0:
        st["empties"] += 1
# ===== end research governor block =============================================================


class Tools:
    class Valves(BaseModel):
        COLLECTION_PREFIX: str = Field(
            "research:",
            description="Name prefix of the user's saved-research Knowledge collections (research:<domain>/<subtopic>).",
        )
        RELEVANCE_THRESHOLD: float = Field(
            0.69,
            description="CRAG grade: keep only chunks with normalized similarity >= this (score=(1+cosine)/2, 1=best). Same calibration as knowledge_search.",
        )
        K: int = Field(6, description="Top-K chunks to retrieve across the research collections before grading.")
        MAX_CONTENT_CHARS: int = Field(
            1500, description="Truncate each returned chunk's content to this many chars (token-budget guard)."
        )
        GOVERNOR_ENABLED: bool = Field(
            True, description="Per-turn research governor: cross-call dedup + empty-search cap (needs the injected chat_id)."
        )
        DEDUP_JACCARD: float = Field(
            0.8, description="Near-duplicate threshold for repeated research queries this turn."
        )
        K_EMPTY: int = Field(
            2, description="After this many EMPTY research searches in a turn, refuse further research calls for the turn."
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = True

    @instrument("research_search", "rag")
    async def research_search(
        self,
        query: str,
        __user__=None,
        __event_emitter__=None,
        __request__=None,
        __chat_id__: str = "",
        __metadata__=None,
    ) -> str:
        """
        Search the research pages the user has SAVED — web pages they explicitly kept via the
        "Save sources" button, organized by topic (e.g. finance, health, cooking). Use this when the
        user asks about something they previously saved or researched ("what did I save about X",
        "pull up my research on Y", "the savings-rates page I kept"). Each result is a real saved page
        with its source URL and the date it was saved. Do NOT use this for the operator's home-network
        setup (use knowledge_search) or for fresh/current facts (use web search) — saved pages are
        snapshots and may be out of date.

        :param query: a natural-language question about the user's saved research.
        """

        async def emit_status(desc, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": desc, "done": done}})

        if __request__ is None:
            return ("Research search is unavailable in this context (no request handle). "
                    "Answer from your own knowledge or use web search.")

        uid = (__user__ or {}).get("id")
        if not uid:
            return ("Research search needs a user context to find your saved collections. "
                    "Answer from your own knowledge or use web search.")

        # ---- per-turn governor (degrades off without an injected chat_id) ----
        chat_id = __chat_id__ or (__metadata__ or {}).get("chat_id") or ""
        message_id = (__metadata__ or {}).get("message_id") or ""
        gov = _rgov_state((chat_id, message_id)) if (chat_id and self.valves.GOVERNOR_ENABLED) else None
        if gov is not None:
            if gov["empties"] >= max(1, self.valves.K_EMPTY):
                await emit_status("No saved research on this topic — stopping.", done=True)
                return (f"You've searched your saved research {gov['empties']} times this turn with no "
                        f"relevant results — stop querying it and answer from your own knowledge or use web search.")
            dup = _rgov_near_dup(gov, query, self.valves.DEDUP_JACCARD)
            if dup is not None:
                await emit_status("Near-duplicate research search — skipped.", done=True)
                return dup

        # ---- enumerate the user's research:* collections ----
        try:
            from open_webui.models.knowledge import Knowledges
            kbs = (await Knowledges.get_knowledge_bases_by_user_id(uid)) or []
        except Exception as e:
            log.warning("research_search: could not list collections: %s", e)
            kbs = []
        ids = [kb.id for kb in kbs if (getattr(kb, "name", "") or "").startswith(self.valves.COLLECTION_PREFIX)]
        if not ids:
            await emit_status("No saved research collections yet.", done=True)
            return ("You haven't saved any research yet (no research:* collections). Use the "
                    "'Save sources' button after reading pages to build one; meanwhile answer from your "
                    "own knowledge or use web search.")

        # ---- retrieval (REUSE OWUI's exact pipeline over all research collections) ----
        await emit_status(f"Searching your saved research: {query}")
        try:
            from open_webui.retrieval.utils import query_collection  # OWUI internal — upgrade-check
            ef = getattr(__request__.app.state, "EMBEDDING_FUNCTION", None)
            if ef is None:
                return ("Research search is not configured (no embedding function). "
                        "Answer from your own knowledge or use web search.")
            res = await query_collection(__request__, ids, [query], ef, max(1, int(self.valves.K)))
        except Exception as e:
            log.warning("research_search retrieval error: %s", e)
            await emit_status("Research search failed.", done=True)
            return ("Your saved research could not be searched right now (retrieval error). "
                    "Answer from your own knowledge or use web search.")

        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        scores = (res.get("distances") or [[]])[0]  # OWUI normalizes to similarity, 1=best

        thr = self.valves.RELEVANCE_THRESHOLD
        kept = [
            (s, d, m)
            for s, d, m in zip(scores, docs, metas)
            if isinstance(d, str) and d.strip() and (s is None or s >= thr)
        ]

        if gov is not None:
            _rgov_record(gov, query, len(kept))

        if not kept:
            await emit_status("Nothing relevant in your saved research.", done=True)
            return ("Nothing in your saved research matches this query. Do not re-query it for this "
                    "topic; answer from your own knowledge or use web search.")

        # ---- format hits: SOURCE url + SAVED_AT (cite + flag age) ----
        out = [f"Found {len(kept)} relevant page(s) in your saved research "
               f"(these are saved snapshots — for current values prefer a web search):\n"]
        for i, (score, doc, meta) in enumerate(kept, 1):
            meta = meta or {}
            title = meta.get("title") or meta.get("name") or meta.get("source") or f"page {i}"
            src = meta.get("source") or ""
            saved_at = (meta.get("saved_at") or "")[:10]
            content = doc.strip()
            if len(content) > self.valves.MAX_CONTENT_CHARS:
                content = content[: self.valves.MAX_CONTENT_CHARS].rstrip() + "…"
            head = f"[{i}] {title}"
            if src:
                head += f" — {src}"
            if saved_at:
                head += f" (saved {saved_at})"
            block = [head]
            if isinstance(score, (int, float)):
                block.append(f"    relevance: {score:.2f}")
            block.append(f"    {content}")
            out.append("\n".join(block))
            if __event_emitter__:
                await __event_emitter__({
                    "type": "citation",
                    "data": {
                        "document": [content],
                        "metadata": [{"source": src or title}],
                        "source": {"name": f"[{i}] {title}" + (f" (saved {saved_at})" if saved_at else ""),
                                   "url": src},
                    },
                })

        await emit_status(f"Found {len(kept)} relevant saved page(s).", done=True)
        return "\n\n".join(out)
