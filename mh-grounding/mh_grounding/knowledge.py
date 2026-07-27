"""mh_grounding.knowledge — the agent-path RAG core (RFC-MH-005 P3).

READ-ONLY retrieval over OWUI's embedded Chroma store for OUT-OF-OWUI processes (mh-mcp).
Inside OWUI, knowledge_search/research_search deliberately reuse OWUI's own pipeline
(`open_webui.retrieval.utils.query_collection` + `app.state.EMBEDDING_FUNCTION`) — that
can't cross a process boundary, so this module reimplements the READ path faithfully:

  - chromadb PersistentClient on OWUI's `vector_db/` — **pin chromadb to OWUI's exact
    version** (on-disk format compatibility; 1.5.2 at build). ALL WRITES stay in OWUI
    (KB ingest, save_research) — this module never mutates the store.
  - the same embedder (sentence-transformers/all-MiniLM-L6-v2, shared HF cache).
  - OWUI's score normalization (similarity, 1=best — the 0.69 CRAG threshold's scale),
    derived per-collection from its `hnsw:space`. Parity anchors (calibrated 2026-06-13):
    on-domain 0.72–0.85, off-domain 0.60–0.65 — verify on any change here.
  - the CRAG grade + ACTIONABLE-empty strings byte-matched to the OWUI tools.

Governor: same three-layer shape as the OWUI tools, session-scoped for the agent path
(no per-turn signal exists over MCP): per-session dedup + an empty-cap where a HIT resets
the empties counter (the per-turn-reset analog). Separate scopes (knowledge vs research)
so they never cross-dedup — mirroring the OWUI KCHAT/RCHAT split. NOTE: the kgov/rgov
helper copies inside the OWUI tools are a known remaining duplication (they can only
unify when those tools import this lib — a later, optional redeploy).

Concurrency stance: chroma embedded is officially single-process; sqlite WAL makes
concurrent READERS safe in practice, but this is validated at build (and the store is
backed up first). If it ever proves flaky, that is the trigger for the pgvector
consolidation (rag-stack-design.md §5) — do not fight sqlite.
"""

import logging
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mh.knowledge")

DEFAULT_VECTOR_DB = str(Path.home() / "service-data" / "open-webui" / "vector_db")
DEFAULT_WEBUI_DB = str(Path.home() / "service-data" / "open-webui" / "webui.db")
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_client = None
_embedder = None


def _get_client(path=DEFAULT_VECTOR_DB):
    global _client
    if _client is None:
        import chromadb
        _client = chromadb.PersistentClient(path=path)
    return _client


def _get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMBEDDING_MODEL)
        log.info("knowledge: embedder %s loaded", EMBEDDING_MODEL)
    return _embedder


def _score_from_distance(d, space):
    """Normalize a chroma distance to OWUI's similarity scale (score=(1+cosine)/2, 1=best).
    cosine space: d = 1-cos  -> score = 1 - d/2.
    l2 space (normalized embeddings): d = 2-2cos -> score = 1 - d/4."""
    if d is None:
        return None
    if space == "l2":
        return 1.0 - d / 4.0
    return 1.0 - d / 2.0  # cosine (chroma's and OWUI's default for these collections)


def query_collections(collection_ids, query, k, vector_db_path=DEFAULT_VECTOR_DB):
    """Query each collection, merge by normalized score desc, return top-k
    [(score, document, metadata)]. Read-only. Raises on store/embedder errors —
    callers wrap into their model-readable failure strings."""
    client = _get_client(vector_db_path)
    emb = _get_embedder().encode([query])[0].tolist()
    merged = []
    for cid in collection_ids:
        try:
            col = client.get_collection(cid)
        except Exception:
            log.warning("knowledge: collection %r not found", cid)
            continue
        space = (col.metadata or {}).get("hnsw:space", "cosine")
        res = col.query(query_embeddings=[emb], n_results=max(1, k),
                        include=["documents", "metadatas", "distances"])
        for d, m, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            merged.append((_score_from_distance(dist, space), d, m or {}))
    merged.sort(key=lambda x: (x[0] is not None, x[0] if x[0] is not None else 0.0), reverse=True)
    return merged[:k]


def enumerate_collections_by_prefix(prefix, webui_db_path=DEFAULT_WEBUI_DB):
    """Resolve knowledge-collection ids by name prefix (e.g. 'research:') from webui.db,
    read-only. Returns [(id, name)]."""
    import sqlite3
    db = sqlite3.connect(f"file:{webui_db_path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT id, name FROM knowledge WHERE name LIKE ?",
                          (prefix + "%",)).fetchall()
    finally:
        db.close()
    return rows


# ===== session-scoped knowledge governor (agent-path analog of the OWUI per-turn kgov) =====
_KGOV_MAX_KEYS = 400
_KGOV_STOPWORDS = frozenset(
    "the a an of to in for on at and or vs with from by is are be as what which how when "
    "where who whom my our your do i need about tell me explain".split()
)
_KGOV_NUM_RE = re.compile(
    r"[\$£€]?\d[\d,\.]*\s*[kKmM]?(?:\s*(?:\.\.|-|to)\s*[\$£€]?\d[\d,\.]*\s*[kKmM]?)?"
)


def _gov_module():
    m = sys.modules.get("_mh_governor_store")
    if m is None:
        m = types.ModuleType("_mh_governor_store")
        m.CHAT = {}
        m.ORDER = []
        sys.modules["_mh_governor_store"] = m
    if not hasattr(m, "KCHAT"):
        m.KCHAT = {}
        m.KORDER = []
    return m


def kgov_state(key):
    """Per-(session, scope) governor state. `key` should be (session_key, 'knowledge'|'research')
    so knowledge and research searches never cross-dedup (the OWUI KCHAT/RCHAT split)."""
    store = _gov_module()
    st = store.KCHAT.get(key)
    if st is None:
        st = {"norm": [], "raw": [], "empties": 0}
        store.KCHAT[key] = st
        store.KORDER.append(key)
        while len(store.KORDER) > _KGOV_MAX_KEYS:
            store.KCHAT.pop(store.KORDER.pop(0), None)
    return st


def _kgov_normalize(q):
    q = (q or "").lower().replace('"', " ").replace("'", " ")
    q = _KGOV_NUM_RE.sub(" <num> ", q)
    toks = set()
    for raw in q.split():
        t = raw.strip(".,;()[]{}!?")
        if t and t not in _KGOV_STOPWORDS:
            toks.add(t)
    return frozenset(toks)


def kgov_near_dup(st, query, threshold, kb_kind="knowledge"):
    nq = _kgov_normalize(query)
    best, best_raw = 0.0, None
    for pn, pr in zip(st["norm"], st["raw"]):
        j = (len(nq & pn) / len(nq | pn)) if (nq and pn) else 0.0
        if j > best:
            best, best_raw = j, pr
    if best >= threshold and best_raw is not None:
        return (f"[knowledge guard] Skipped — nearly identical to your earlier {kb_kind} search "
                f"«{best_raw}» (similarity {best:.0%}) and the knowledge base already returned nothing "
                f"relevant. Don't re-query it for this topic; answer from your own knowledge or use web search.")
    return None


def kgov_record(st, query, n_hits):
    st["norm"].append(_kgov_normalize(query))
    st["raw"].append(query)
    if n_hits == 0:
        st["empties"] += 1
    else:
        st["empties"] = 0  # a HIT resets the cap — the agent-path analog of the per-turn reset
# ===== end governor =====


@dataclass
class KnowledgeConfig:
    """Mirrors mh-tools/knowledge_search.py Valves — names AND defaults."""
    KB_COLLECTION_ID: str = "0854e216-8644-4d9e-95c3-d5f6727719e7"
    REFERENCE_COLLECTION_IDS: str = "82a7c3ab-4d3e-4008-9123-c11c18bad8e5"
    KB_DESCRIPTION: str = ("the operator's home network & self-hosted services (devices, configs, "
                           "incidents) AND official docs for the software the stack uses (Python "
                           "libraries/frameworks, ops tools)")
    RELEVANCE_THRESHOLD: float = 0.69
    K: int = 4
    MAX_CONTENT_CHARS: int = 1500
    GOVERNOR_ENABLED: bool = True
    DEDUP_JACCARD: float = 0.8
    K_EMPTY: int = 2


@dataclass
class KnowledgeResult:
    text: str
    hits: list = field(default_factory=list)   # (score, content, title, src) kept chunks
    empty: bool = False


def _format_hits(kept, cfg, research=False, prefix=None):
    if research:
        out = [f"Found {len(kept)} relevant page(s) in your saved research "
               f"({prefix}* collections):\n"]
    else:
        out = [f"Found {len(kept)} relevant document(s) in the knowledge base:\n"]
    hits = []
    for i, (score, doc, meta) in enumerate(kept, 1):
        meta = meta or {}
        title = meta.get("name") or meta.get("title") or meta.get("source") or f"chunk {i}"
        src = meta.get("source") or meta.get("file_id") or ""
        saved_at = (meta.get("saved_at") or "")[:10] if research else ""
        content = doc.strip()
        if len(content) > cfg.MAX_CONTENT_CHARS:
            content = content[: cfg.MAX_CONTENT_CHARS].rstrip() + "…"
        head = f"[{i}] {title}" + (f" — {src}" if src else "")
        if saved_at:
            head += f" (saved {saved_at})"
        block = [head]
        if isinstance(score, (int, float)):
            block.append(f"    relevance: {score:.2f}")
        block.append(f"    {content}")
        out.append("\n".join(block))
        hits.append((score, content, title, src))
    return "\n\n".join(out), hits


def knowledge_query(query, cfg: KnowledgeConfig = None, gov=None) -> KnowledgeResult:
    """The knowledge_search read path (KB + reference union), CRAG-graded, governed."""
    cfg = cfg or KnowledgeConfig()
    if gov is not None:
        if gov["empties"] >= max(1, cfg.K_EMPTY):
            return KnowledgeResult(
                f"You have searched the knowledge base {gov['empties']} times with no relevant "
                f"results. It covers {cfg.KB_DESCRIPTION} and does not contain information on "
                f"this topic — stop querying it and answer from your own knowledge or use web search.",
                empty=True)
        dup = kgov_near_dup(gov, query, cfg.DEDUP_JACCARD, "knowledge")
        if dup is not None:
            return KnowledgeResult(dup, empty=True)

    ids = [cfg.KB_COLLECTION_ID] + [
        c.strip() for c in (cfg.REFERENCE_COLLECTION_IDS or "").split(",") if c.strip()]
    try:
        rows = query_collections(ids, query, max(1, int(cfg.K)))
    except Exception as e:
        log.warning("knowledge_query retrieval error: %s", e)
        return KnowledgeResult(
            "The knowledge base could not be searched right now (retrieval error). "
            "Answer from your own knowledge or use web search.", empty=True)

    thr = cfg.RELEVANCE_THRESHOLD
    kept = [(s, d, m) for s, d, m in rows
            if isinstance(d, str) and d.strip() and (s is None or s >= thr)]
    if gov is not None:
        kgov_record(gov, query, len(kept))

    if not kept:
        log.info("knowledge_query: no chunk >= %.2f for q=%r", thr, query[:80])
        return KnowledgeResult(
            f"No relevant documents in the knowledge base (covers: {cfg.KB_DESCRIPTION}) "
            f"for this query — it is out of this knowledge base's domain. Do not re-query the "
            f"knowledge base for this topic; answer from your own knowledge or use web search.",
            empty=True)

    text, hits = _format_hits(kept, cfg)
    return KnowledgeResult(text, hits=hits)


def research_query(query, cfg: KnowledgeConfig = None, gov=None,
                   prefix="research:", webui_db_path=DEFAULT_WEBUI_DB) -> KnowledgeResult:
    """The research_search read path: enumerate research:* collections, query, grade.
    Surfaces each hit's source URL + saved-at (snapshots — the model should flag age)."""
    cfg = cfg or KnowledgeConfig()
    if gov is not None:
        if gov["empties"] >= max(1, cfg.K_EMPTY):
            return KnowledgeResult(
                f"You have searched the saved research {gov['empties']} times with no relevant "
                f"results. Stop querying it and answer from your own knowledge or use web search.",
                empty=True)
        dup = kgov_near_dup(gov, query, cfg.DEDUP_JACCARD, "research")
        if dup is not None:
            return KnowledgeResult(dup, empty=True)

    try:
        rows = enumerate_collections_by_prefix(prefix, webui_db_path)
    except Exception as e:
        log.warning("research_query enumeration error: %s", e)
        rows = []
    if not rows:
        return KnowledgeResult(
            "You haven't saved any research yet (no research:* collections). Use the "
            "Save Sources button in a chat after reading pages to build this corpus.",
            empty=True)

    try:
        merged = query_collections([r[0] for r in rows], query, max(1, int(cfg.K)))
    except Exception as e:
        log.warning("research_query retrieval error: %s", e)
        return KnowledgeResult(
            "The saved research could not be searched right now (retrieval error). "
            "Answer from your own knowledge or use web search.", empty=True)

    thr = cfg.RELEVANCE_THRESHOLD
    kept = [(s, d, m) for s, d, m in merged
            if isinstance(d, str) and d.strip() and (s is None or s >= thr)]
    if gov is not None:
        kgov_record(gov, query, len(kept))

    if not kept:
        return KnowledgeResult(
            f"No relevant pages in your saved research ({prefix}* collections) for this query. "
            f"Do not re-query it for this topic; answer from your own knowledge or use web search.",
            empty=True)

    text, hits = _format_hits(kept, cfg, research=True, prefix=prefix)
    return KnowledgeResult(text, hits=hits)
