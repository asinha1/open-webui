# research_search — design & rationale

Companion to `research_search.py`. The model-facing **READ side** of the self-generating RAG.
**Authoritative design: RFC-MH-002 / `provisioning/hephaestus/tooling-research/self-generating-rag-design.md` (§3b).**

## What it is

A single model-facing tool, `research_search(query) -> str`, that searches the user's OWN saved research —
the `research:<domain>/<subtopic>` Knowledge collections written by the "Save Sources" Action
(`save_research.py`). It mirrors `knowledge_search` exactly but over a different corpus, and surfaces each
hit's **source URL + saved-date** so the model cites and can flag age (saved pages are snapshots).

**Deliberately SEPARATE from `knowledge_search`** (which owns the home-network KB): extending that tool would
force broadening its narrow L1 docstring and rewrite its K2 routing criterion, putting K1–K4 at risk. Keeping
them split protects that validation. **Long-term unification into one "search my knowledge" tool is OPEN**
(operator 2026-06-15).

## Layers (mirrors knowledge_search)

1. **Scoped docstring** — "search the research pages YOU'VE saved"; NOT the home network (knowledge_search),
   NOT fresh facts (web). Routes cleanly.
2. **CRAG relevance grade** — reuses `query_collection` over ALL the user's `research:*` collections
   (enumerated via `get_knowledge_bases_by_user_id`, filtered on the `research:` name prefix); keeps chunks
   ≥ `RELEVANCE_THRESHOLD` (0.69); on empty returns an actionable "nothing saved on this", never a bare `[]`.
3. **Per-turn governor** — dedup + empty-cap, on its OWN `RCHAT` scope of the shared `sys.modules` sentinel
   (never cross-deduped with knowledge or web).

## Model-facing surface

| Param | Type | Purpose |
|---|---|---|
| `query` | str (required) | a natural-language question about the user's saved research |

Injected (dunder, stripped from the schema): `__user__` (→ the user's id, to find their `research:*`
collections), `__request__` (→ `app.state.EMBEDDING_FUNCTION`), `__chat_id__` + `__metadata__` (per-turn governor).

## Valves

| Valve | Default | Purpose |
|---|---|---|
| `COLLECTION_PREFIX` | `research:` | name prefix of the saved-research collections to enumerate |
| `RELEVANCE_THRESHOLD` | 0.69 | CRAG grade (same calibration as knowledge_search) |
| `K` | 6 | top-K across the research collections before grading |
| `MAX_CONTENT_CHARS` | 1500 | per-chunk truncation |
| `GOVERNOR_ENABLED` / `DEDUP_JACCARD` / `K_EMPTY` | on / 0.8 / 2 | per-turn governor knobs |

## Output — source + age (the freshness contract)

Each hit renders `[i] title — source-URL (saved YYYY-MM-DD)` + the graded relevance + content; the result
header reminds that these are saved snapshots ("for current values prefer a web search"). This is what lets
the model cite the URL and flag/avoid stale data — the §6 temporal-bypass behavior.

## Deploy

Standard mh-tool: Workspace → Tools → paste → enable on Gemma (`meta.toolIds += research_search`) → restart.
No KB-detach needed (it owns no built-in). **Upgrade-check:** `query_collection` +
`get_knowledge_bases_by_user_id` are OWUI internals — re-verify on bumps (`reference/upgrades.md`).

## Acceptance (IN-OWUI — needs `app.state`; cf. `probes.yaml` Block S / S3)

- [x] On-domain saved query returns the saved page(s) with source URL + saved-date (validated 2026-06-15, S3).
- [ ] No saved research → actionable "nothing saved", not `[]`; model falls back.
- [ ] Snapshot-age surfaced → model flags age / prefers web for a "right now" follow-up (soft S4).

## Deferred (v1)

- The long-term `research_search` ↔ `knowledge_search` **unification** (one tool over home corpus + saved research).
- Domain-scoping the query to `research:<domain>/*` if query-all ever slows — the registry bounds it for now.
