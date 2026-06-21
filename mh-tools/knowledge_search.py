"""
title: Knowledge Search
author: mh-tools
version: 1.0.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — version-controlled in the open-webui fork under mh-tools/.
# REPLACES OWUI's built-in knowledge/RAG search (search_knowledge_files/query_knowledge_files)
# for the household chat model. Design + rationale: provisioning/hephaestus/tooling-research/
# knowledge-tool-design.md (RFC-MH-001). Deploy = DB surgery on the model row + RESTART OWUI:
#   meta.knowledge = []  AND  meta.builtinTools.knowledge = false  (BOTH — the verified bleed-fix)
#   + add 'knowledge_search' to meta.toolIds.
#
# WHY: the built-in looped 512x on an off-domain finance query (the home-networking KB returns []
# for finance; the model treated [] as "rephrase + retry" forever — incidents/owui-runaway-
# generation-slot-wedge.md). This tool keeps RAG callable but makes the loop structurally
# impossible via three layers:
#   L1 — domain-scoped docstring (the model shouldn't reach for a home-network KB on finance).
#   L2 — CRAG relevance grade: keep only chunks with similarity >= RELEVANCE_THRESHOLD; on
#        empty/irrelevant return an ACTIONABLE "not in this KB, stop, fall back" — never a bare [].
#   L3 — per-TURN governor: cross-call dedup + an empty-search cap (refuse after K_EMPTY empties
#        in a turn). Shares the web governor's sys.modules sentinel but a SEPARATE scope (KCHAT)
#        so knowledge + web searches are never cross-deduped (they're complementary).
#
# Reuses OWUI's exact retrieval (open_webui.retrieval.utils.query_collection) — same MiniLM
# embedder + Chroma client, no reimplementation. The injected `__request__` carries app.state.
# UPGRADE-CHECK: query_collection's signature is an OWUI internal — re-verify on OWUI bumps
# (reference/upgrades.md). Validation is IN-OWUI only (needs app.state) — not the disk eval harness.

import logging
import re
import sys
import types
from typing import Optional

from pydantic import BaseModel, Field

log = logging.getLogger("mh.knowledge_search")

# ===== knowledge over-search governor — SEPARATE scope on the shared sys.modules sentinel ======
# Shares the module `_mh_governor_store` with the web governor (tavily/deep_research) but uses its
# OWN dict (KCHAT), keyed PER-TURN by (chat_id, message_id) so: (a) knowledge dedup never collides
# with web dedup, (b) the empty-cap resets each user turn (a later on-domain turn can still use the
# KB). Ephemeral; module-level so it survives the per-call Tools() re-instantiation (single worker).
_KGOV_MAX_TURNS = 400
_KGOV_STOPWORDS = frozenset(
    "the a an of to in for on at and or vs with from by is are be as what which how when "
    "where who whom my our your do i need about tell me explain".split()
)
_KGOV_NUM_RE = re.compile(
    r"[\$£€]?\d[\d,\.]*\s*[kKmM]?(?:\s*(?:\.\.|-|to)\s*[\$£€]?\d[\d,\.]*\s*[kKmM]?)?"
)


def _kgov_store():
    m = sys.modules.get("_mh_governor_store")
    if m is None:
        m = types.ModuleType("_mh_governor_store")
        m.CHAT = {}
        m.ORDER = []
        sys.modules["_mh_governor_store"] = m
    if not hasattr(m, "KCHAT"):
        m.KCHAT = {}    # (chat_id, message_id) -> {"norm":[frozenset], "raw":[str], "empties":int}
        m.KORDER = []   # LRU of turn keys
    return m


def _kgov_normalize(q):
    q = (q or "").lower().replace('"', " ").replace("'", " ")
    q = _KGOV_NUM_RE.sub(" <num> ", q)
    toks = set()
    for raw in q.split():
        t = raw.strip(".,;()[]{}!?")
        if t and t not in _KGOV_STOPWORDS:
            toks.add(t)
    return frozenset(toks)


def _kgov_jaccard(a, b):
    return (len(a & b) / len(a | b)) if (a and b) else 0.0


def _kgov_state(turn_key):
    store = _kgov_store()
    st = store.KCHAT.get(turn_key)
    if st is None:
        st = {"norm": [], "raw": [], "empties": 0}
        store.KCHAT[turn_key] = st
        store.KORDER.append(turn_key)
        while len(store.KORDER) > _KGOV_MAX_TURNS:
            store.KCHAT.pop(store.KORDER.pop(0), None)
    return st


def _kgov_near_dup(st, query, threshold):
    nq = _kgov_normalize(query)
    best, best_raw = 0.0, None
    for pn, pr in zip(st["norm"], st["raw"]):
        j = _kgov_jaccard(nq, pn)
        if j > best:
            best, best_raw = j, pr
    if best >= threshold and best_raw is not None:
        return (f"[knowledge guard] Skipped — nearly identical to your earlier knowledge search "
                f"«{best_raw}» (similarity {best:.0%}) and the knowledge base already returned nothing "
                f"relevant. Don't re-query it for this topic; answer from your own knowledge or use web search.")
    return None


def _kgov_record(st, query, n_hits):
    st["norm"].append(_kgov_normalize(query))
    st["raw"].append(query)
    if n_hits == 0:
        st["empties"] += 1
# ===== end knowledge governor block ===========================================================


class Tools:
    class Valves(BaseModel):
        KB_COLLECTION_ID: str = Field(
            "0854e216-8644-4d9e-95c3-d5f6727719e7",
            description="Chroma collection id of the knowledge base (== its KB id). Default = home-networking-repo.",
        )
        REFERENCE_COLLECTION_IDS: str = Field(
            "82a7c3ab-4d3e-4008-9123-c11c18bad8e5",
            description="Comma-separated Chroma collection ids of reference:* KBs (e.g. reference:python), "
                        "UNIONED into retrieval alongside KB_COLLECTION_ID. Shared + read-by-id (the home-repo "
                        "pattern, NOT per-user research_search enumeration) so every user gets them. Blank = off.",
        )
        KB_DESCRIPTION: str = Field(
            "the operator's home network & self-hosted services (devices, configs, incidents) AND official "
            "docs for the software the stack uses (Python libraries/frameworks, ops tools)",
            description="Short domain description, named in the actionable 'not in this KB' message.",
        )
        RELEVANCE_THRESHOLD: float = Field(
            0.69,
            description="CRAG grade: keep only chunks with normalized similarity >= this (score=(1+cosine)/2, 1=best). Calibrated 2026-06-13 on home-networking-repo: on-domain 0.72-0.85, off-domain 0.60-0.65 -> 0.69 separates cleanly.",
        )
        K: int = Field(4, description="Top-K chunks to retrieve before grading.")
        MAX_CONTENT_CHARS: int = Field(
            1500, description="Truncate each returned chunk's content to this many chars (token-budget guard)."
        )
        GOVERNOR_ENABLED: bool = Field(
            True, description="Per-turn knowledge governor: cross-call dedup + empty-search cap (needs the injected chat_id)."
        )
        DEDUP_JACCARD: float = Field(
            0.8, description="Near-duplicate threshold for repeated knowledge queries this turn."
        )
        K_EMPTY: int = Field(
            2, description="After this many EMPTY knowledge searches in a turn, refuse further knowledge calls for the turn."
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = True

    async def knowledge_search(
        self,
        query: str,
        __event_emitter__=None,
        __request__=None,
        __chat_id__: str = "",
        __metadata__=None,
    ) -> str:
        """
        Search the operator's loaded-in reference knowledge: (1) the private home-network / self-hosted
        setup — devices (janus, metis), services, network/DNS/proxy configs, incident write-ups; AND
        (2) official documentation for the software this stack is built on — Python libraries & frameworks
        (e.g. pydantic, numpy, aiohttp, starlette, chromadb, pdfplumber, pillow) and ops tools. Use this
        for questions about this home setup OR about how to use one of those specific libraries/APIs. Do
        NOT use it for general knowledge, finance, news, current events, or libraries not in that set —
        it has nothing on those and they return no relevant results (use web search for those instead).

        :param query: a natural-language question about the home setup or a library/API the stack uses.
        """

        async def emit_status(desc, done=False):
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": desc, "done": done}}
                )

        if __request__ is None:
            return ("Knowledge search is unavailable in this context (no request handle). "
                    "Answer from your own knowledge or use web search.")

        # ---- per-turn governor (degrades off without an injected chat_id) ----
        chat_id = __chat_id__ or (__metadata__ or {}).get("chat_id") or ""
        message_id = (__metadata__ or {}).get("message_id") or ""
        gov = _kgov_state((chat_id, message_id)) if (chat_id and self.valves.GOVERNOR_ENABLED) else None
        if gov is not None:
            if gov["empties"] >= max(1, self.valves.K_EMPTY):
                log.info("knowledge_search governor: empty-cap hit chat=%s", chat_id)
                await emit_status("Knowledge base has nothing on this topic — stopping.", done=True)
                return (f"You have searched the knowledge base {gov['empties']} times with no relevant "
                        f"results this turn. It covers {self.valves.KB_DESCRIPTION} and does not contain "
                        f"information on this topic — stop querying it and answer from your own knowledge "
                        f"or use web search.")
            dup = _kgov_near_dup(gov, query, self.valves.DEDUP_JACCARD)
            if dup is not None:
                await emit_status("Near-duplicate knowledge search — skipped.", done=True)
                return dup

        # ---- retrieval (REUSE OWUI's exact pipeline; no reimplementation) ----
        await emit_status(f"Searching the knowledge base: {query}")
        try:
            from open_webui.retrieval.utils import query_collection  # OWUI internal — upgrade-check
            ef = getattr(__request__.app.state, "EMBEDDING_FUNCTION", None)
            if ef is None:
                return ("The knowledge base is not configured (no embedding function). "
                        "Answer from your own knowledge or use web search.")
            _ids = [self.valves.KB_COLLECTION_ID] + [
                c.strip() for c in (self.valves.REFERENCE_COLLECTION_IDS or "").split(",") if c.strip()
            ]
            res = await query_collection(
                __request__,
                _ids,
                [query],
                ef,
                max(1, int(self.valves.K)),
            )
        except Exception as e:
            log.warning("knowledge_search retrieval error: %s", e)
            await emit_status("Knowledge search failed.", done=True)
            return ("The knowledge base could not be searched right now (retrieval error). "
                    "Answer from your own knowledge or use web search.")

        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        scores = (res.get("distances") or [[]])[0]  # NB: OWUI normalizes to similarity, 1=best

        # ---- CRAG grade: keep only chunks at/above the relevance threshold ----
        thr = self.valves.RELEVANCE_THRESHOLD
        kept = [
            (s, d, m)
            for s, d, m in zip(scores, docs, metas)
            if isinstance(d, str) and d.strip() and (s is None or s >= thr)
        ]

        if gov is not None:
            _kgov_record(gov, query, len(kept))

        # ---- actionable empty (the loop-killer) — NEVER a bare [] ----
        if not kept:
            log.info("knowledge_search: no chunk >= %.2f for q=%r (empties this turn=%s)",
                     thr, query[:80], gov["empties"] if gov else "n/a")
            await emit_status("No relevant documents in the knowledge base.", done=True)
            return (f"No relevant documents in the knowledge base (covers: {self.valves.KB_DESCRIPTION}) "
                    f"for this query — it is out of this knowledge base's domain. Do not re-query the "
                    f"knowledge base for this topic; answer from your own knowledge or use web search.")

        # ---- format hits (title/source + content), emit citations ----
        out = [f"Found {len(kept)} relevant document(s) in the knowledge base:\n"]
        for i, (score, doc, meta) in enumerate(kept, 1):
            meta = meta or {}
            title = meta.get("name") or meta.get("title") or meta.get("source") or f"chunk {i}"
            src = meta.get("source") or meta.get("file_id") or ""
            content = doc.strip()
            if len(content) > self.valves.MAX_CONTENT_CHARS:
                content = content[: self.valves.MAX_CONTENT_CHARS].rstrip() + "…"
            block = [f"[{i}] {title}" + (f" — {src}" if src else "")]
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
                        "source": {"name": f"[{i}] {title}", "url": src},
                    },
                })

        await emit_status(f"Found {len(kept)} relevant document(s).", done=True)
        return "\n\n".join(out)
