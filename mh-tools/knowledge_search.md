# knowledge_search — design & rationale

Companion to `knowledge_search.py`. The owned, governed replacement for OWUI's built-in knowledge/RAG
search tool. **Authoritative design: RFC-MH-001 / `provisioning/hephaestus/tooling-research/knowledge-tool-design.md`.**

## What it is

A single model-facing tool, `knowledge_search(query) -> str`, that searches the household knowledge base
(the `home-networking-repo` Chroma collection) — but, unlike the built-in `search_knowledge_files`, it
**grades retrieval relevance and refuses to loop on empty results**. It exists because the built-in looped
**512×** on an off-domain finance query (the KB returns `[]` for finance; the model treated `[]` as
"rephrase + retry" forever — `incidents/owui-runaway-generation-slot-wedge.md`).

## The three-layer defense

1. **Domain-scoped docstring (relevance gate).** The model-facing docstring scopes the tool to the home
   network and explicitly says *don't use it for general/finance/news.* The built-in's generic description
   is a root cause; this cuts off-domain calls before retrieval runs.
2. **CRAG relevance grade.** Reuses OWUI's `query_collection` (same MiniLM embedder + Chroma client) then
   keeps only chunks with normalized similarity `>= RELEVANCE_THRESHOLD`. OWUI's score is `(1+cosine)/2`
   (1 = best); our path is non-hybrid so OWUI applies **no** threshold itself — the grade lives here. On
   empty/below-threshold it returns an **actionable** "not in this KB, stop, fall back" — **never a bare `[]`**.
3. **Per-turn governor (mechanical floor).** Cross-call dedup + a hard **empty-search cap**: after `K_EMPTY`
   (2) empty searches **in a turn** it refuses further knowledge calls and tells the model to stop. Shares
   the web governor's `sys.modules` sentinel but a **separate scope** (`KCHAT`, keyed by
   `(chat_id, message_id)`) so knowledge + web are never cross-deduped and the cap resets each user turn.

## Model-facing surface

| Param | Type | Purpose |
|---|---|---|
| `query` | str (required) | a natural-language question about the home network / self-hosted services |

Injected (dunder, stripped from the schema): `__request__` (→ `app.state.EMBEDDING_FUNCTION`, the retrieval
pipeline), `__chat_id__` + `__metadata__` (chat_id + **message_id** for the per-turn governor).

## Valves

| Valve | Default | Purpose |
|---|---|---|
| `KB_COLLECTION_ID` | `0854e216-…` (home-networking-repo) | Chroma collection id (== KB id) to search |
| `KB_DESCRIPTION` | "the operator's private home network…" | domain string named in the actionable message |
| `RELEVANCE_THRESHOLD` | **0.69** | CRAG grade: keep chunks with similarity ≥ this. **Calibrated 2026-06-13** on home-networking-repo (585 chunks): on-domain top-scores 0.72–0.85, off-domain 0.60–0.65 → 0.69 separates cleanly. |
| `K` | 4 | top-K retrieved before grading |
| `MAX_CONTENT_CHARS` | 1500 | per-chunk truncation |
| `GOVERNOR_ENABLED` / `DEDUP_JACCARD` / `K_EMPTY` | on / 0.8 / 2 | the per-turn governor knobs |

## Reuse (not reimplementation)

`from open_webui.retrieval.utils import query_collection` — called with `__request__`, `[KB_COLLECTION_ID]`,
`[query]`, `app.state.EMBEDDING_FUNCTION`, `K`. Same embedder/Chroma/normalization as the built-in; zero new
deps; no external calls. **Upgrade-check:** `query_collection`'s signature is an OWUI internal — re-verify on
OWUI version bumps (`reference/upgrades.md`).

## Deploy (the verified bleed-fix — BOTH flags)

DB surgery on the model row + restart OWUI:
1. `meta.knowledge = []` **AND** `meta.builtinTools.knowledge = false` — detaching the KB alone is **not**
   enough; `utils/tools.py:500-511` still injects the built-in knowledge tools in browse-all mode.
2. Add `knowledge_search` to `meta.toolIds`.
3. Restart. The built-in knowledge tools then never enter the tool chain; this tool owns the path.

## Acceptance (verify IN-OWUI — needs `app.state`, NOT the disk eval harness)

- [ ] **Calibration:** on-domain queries ("janus reverse proxy nginx", "AdGuard DNS rewrites") return real
      chunks ≥ threshold; off-domain (the Roth queries) return the actionable empty, **not `[]`**. Tune
      `RELEVANCE_THRESHOLD` so on-domain clears and finance fails.
- [ ] **Loop-killer (the headline):** the exact Roth prompt issues ≤ `K_EMPTY` knowledge searches (was **512**),
      an honest "not in the knowledge base", **no loop**, a sensible web/parametric answer.
- [ ] **Per-turn reset:** a later on-domain turn in the same chat still uses the KB (cap reset). *(Governor
      unit-tested 2026-06-13: Roth storm bounded at 2; new turn resets to 0.)*
- [ ] **Zero bleed:** after deploy, the model's tool list shows `knowledge_search` and **no** `search_knowledge_files`/`query_knowledge_files`/`grep_knowledge_files`.

## Deferred / out of scope (v1)

- Disabling the `knowledge` category also drops the built-in `grep_knowledge_files` / `list_knowledge` /
  `view_*`. v1 ships only `knowledge_search`; re-add the others as mh-tools if needed.
- **Multi-KB routing** (per-domain KBs + relevance routing) — the scalable future; the actionable message
  already names the KB domain (the routing seed).
