# save_research — design & rationale ("Save Sources" Action)

Companion to `save_research.py`. The **WRITE side** of the self-generating RAG: a deterministic OWUI
**Action** (button), NOT a model-routed tool. **Authoritative design: RFC-MH-002 /
`provisioning/hephaestus/tooling-research/self-generating-rag-design.md`.**

## What it is

An OWUI **Function of `type=action`** (id `save_sources`, global) → renders a **📌 "Save Sources" button** on
the assistant's response messages. On click it saves the web pages READ this chat into the user's
`research:<domain>/<subtopic>` Knowledge collections, retrievable later by `research_search`.

**Why an Action, not a tool:** a save mutates durable storage that future retrievals depend on — it must fire
exactly on intent, never on a model whim. The deterministic button is the strongest form of "no model
judgment on the write path" (the honesty thesis, §2). The model only ever *proposes*; the click *writes* —
and the model must never *claim* it saved (it can't; only the button does).

## The flow (on click)

1. **Find read pages** — load the chat (`Chats.get_chat_by_id`), collect the http `source.url`s from
   read_page citations in the assistant messages (KB chunks / non-http excluded), with the triggering query.
2. **Dialog 1 — topic** (`__event_call__` `input`): user types `domain/subtopic`; validated against the
   registry (`research-topics.json`); re-prompts once on a bad/unknown domain.
3. **Dialog 2 — pages** (`input`): the read pages are listed numbered; user types `all` (default) or `1,3`.
   *(OWUI `__event_call__` has only input/confirmation/execute — no rich form; two text steps is the native
   shape. A model-proposed pre-fill is a deferred enhancement.)*
4. **Re-fetch + save** — re-fetch each chosen page FULL via OWUI's `get_content_from_url` (uncapped, behind
   `byte_ceiling`, decoupled from read_page's ~60K model-return cap); build provenance; dedup by url-hash
   (SKIP unchanged / REINDEX on content change / ADD); create the `research:<domain>/<subtopic>` Knowledge
   if absent (`Knowledges.insert_new_knowledge`; its id = the vector collection_name); write via
   `save_docs_to_vector_db(add=True)`. Confirm-back via a status emit.

## Provenance (per chunk)

`source` (url) · `saved_at` (ISO) · `title` · `hash` = sha256(url) · `content_hash` = sha256(text) ·
`content_class` (reference|volatile — the v2 TTL hook). Flat (Chroma-legal). `research-topics.json` is the
curated domain **breadth-cap** (seed finance/health/cooking; new domains = an operator edit).

## Surfaces

Pure helpers (stdlib-only, offline-tested by `eval/save_research_test.py`): `_slug`, `_resolve_topic`,
`_classify`, `_meta`, `_dedup_decision`, `_cap_bytes`. Valves: `byte_ceiling` (per-page save cap),
`new_topic_description`. Icon: Heroicons `archive-box-arrow-down` as an SVG **data-URI** — OWUI renders
`module.icon` as an `<img src>` (an emoji → broken image), special-cases `data:image/svg` for dark-mode invert.

## Deploy (an ACTION, not a Tool)

**Admin Panel → Functions** (not Workspace → Tools) → paste `save_research.py` → Save → enable + **Global**
(so the button renders on every model's messages). **Restart OWUI** to rebuild `model.actions` (it reads
`getattr(module, 'icon')` at model-build time, `utils/models.py`).

## Acceptance (IN-OWUI; cf. `probes.yaml` Block S / S3 — validated 2026-06-15)

- [x] Save → `research:<domain>/<subtopic>` created, pages re-fetched, chunks written with full provenance
      (S3: Ally page → 47 chunks, every provenance field present).
- [x] Confirm-back shows what saved + where; collection visible in Workspace → Knowledge (curation gate #2).
- [x] New chat → `research_search` retrieves the saved page with source + date.

## Deferred (v1)

- **Model-proposal** pre-filling the dialog topic; **content-class TTL** enforcement + auto-refresh;
  `deep_research` as a second writer; the `content_class` heuristic calibration (it classified the Ally
  savings page `reference`; a rates/APY page is arguably `volatile` — widen the keyword set).
