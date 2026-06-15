# deep_research — read several sources in parallel, return one digest

**Status: v1.1.0 DEPLOYED 2026-06-08.** v1.0 built+validated 2026-06-07; **v1.1.0 (2026-06-08)** is the
tool-side half of the v10 over-search/under-read fix — when a source can't be read, the digest now
ROUTES the model to the right next action instead of "try different sources" (the reworded-re-search
trigger; see `provisioning/hephaestus/system-prompt-v10-research.md`). Loaded via Workspace→Tools
(DB-stored), enabled on Gemma alongside `tavily_search`, `read_page`, `render_page`, `export_document`;
`TAVILY_API_KEY` Valve set (query mode — mirror `tavily_search`'s key). **No new dep** (`aiohttp`,
`bs4`, `markdownify`, `certifi` already in the venv).

## Intent

Let the model **research a topic across many pages in one turn** — read N sources *in parallel* and
get back a single consolidated digest to synthesize — instead of reading pages one-by-one.

## Why (the trigger) + the feasibility insight

The 2026-06-06 readout's 4-article ingestion chat read pages **sequentially**; reading them
concurrently would have been faster. The operator asked for a "deep-research" capability and flagged
"maybe this isn't feasible." The resolution:

- **Parallel *inference* is NOT feasible** — the conversation runs on one llama-server slot
  (`--parallel 1`, deliberate). You can't run multiple Gemma generations at once, and we *tell* the
  model "one tool call at a time" (Gemma native-FC parallel-call unreliability + two-pass FC).
- **Parallel *tool-I/O* IS feasible** — fetching N pages is I/O-bound (`asyncio.gather`). One model
  tool call → the tool fans out concurrent fetches → returns ONE digest → the model synthesizes in a
  single pass. This sidesteps the single-slot limit *and* the parallel-call unreliability (the fan-out
  is deterministic code, not model-orchestrated).
- **It's also CACHE-FRIENDLIER** than N sequential read turns: one prefill of the digest at the end of
  context, not N re-prefills (the two-pass-FC cost we measured in the soak).

So this is the "deep-research" capability scoped to what's actually feasible + cache-conscious. The
heavier autonomous-agent flavor (the tool calling an LLM to decompose/summarize per source) was
deliberately **not** built — it would re-introduce single-slot serialization and Gemma agentic-
reliability risk. Decomposition stays with the model (it picks the query/urls in one call); the tool
does deterministic fan-out only.

## Model-facing surface (thin)

```
deep_research(query: Optional[str] = None, urls: Optional[list] = None, max_sources: int = 5) -> str
```
- **`query`** — a research question/topic to search, then read the top results (omit if you pass urls).
- **`urls`** — a list of absolute http(s) URLs to read in parallel (omit if you pass query).
- **`max_sources`** — how many to read (clamped to `MAX_SOURCES`).

Docstring routes it: use instead of reading pages one-by-one when there are multiple sources; for a
single known page use `read_page`; for plain discovery use `tavily_search`.

## Behavior

1. **Mode:** `urls` → read exactly those; `query` (no urls) → one `tavily_search` REST call →
   top-`max_sources` result URLs → read them.
2. **Parallel fetch** (`asyncio.gather`, capped by `CONCURRENCY`): each source fetched with the
   read_page pipeline — **certifi TLS**, **SSRF-guarded** manual redirect-following (every hop
   re-checked), HTML→Markdown via markdownify (main-content targeting, links absolutized, images→alt).
3. **Per-source truncation** (`MAX_CHARS_PER_SOURCE`) + **overall budget** (`MAX_TOTAL_CHARS`).
4. **Consolidate** into one digest: a header (how many read, plus an explicit list of any that
   **couldn't** be read — inability, not absence) + a synthesize-and-cite instruction + `## [i] title
   / url / content` sections. **One Source chip per source** (citation events). **v1.1.0:** when sources
   are unread, the header appends the correct next action — JS-gated → "open that page with the
   JavaScript-capable reader"; access-blocked (401/403/429) → "read a different authoritative source" —
   rather than "search again" (which triggered the reworded-re-search loop; `_escalation_hint`).
5. Status events show progress (searching → reading N → read K of N).

## Safety / trust posture

- **SSRF guard on every fetch** (entry URL + each redirect hop): rejects loopback/private/link-local/
  **CGNAT-tailnet `100.64/10`**/`*.local`/`*.internal`; a blocked source becomes a reported failure,
  not a crash. **Validated:** an internal URL in the list (`127.0.0.1:8081`) was reported as
  "internal/blocked address", the rest still read.
- **Bounded fan-out:** `MAX_SOURCES` hard cap + `CONCURRENCY` cap — no unbounded request storm.
- **Read-only** (unlike `export_document`); query mode costs Tavily credits (one discovery search).
- Bot-walled/unreadable sources are reported honestly in the digest header (HTTP 403/400 etc.), so the
  model says it couldn't read them rather than inventing their contents.

## Valves (DB, never committed)

| Valve | Default | Purpose |
|---|---|---|
| `MAX_SOURCES` | 8 | hard cap on sources per call. |
| `CONCURRENCY` | 5 | max concurrent fetches (I/O parallelism; inference stays single-slot). |
| `MAX_CHARS_PER_SOURCE` | 10000 | per-source content truncation. |
| `MAX_TOTAL_CHARS` | 60000 | ceiling on the whole digest (~22K tok). |
| `TIMEOUT` | 30 | per-source HTTP timeout (s). |
| `TAVILY_API_KEY` | "" | query mode only (mirror `tavily_search`'s key). |
| `TAVILY_SEARCH_DEPTH` | basic | discovery depth (`basic`=1 credit / `advanced`=2); reading is done here, so basic usually suffices. |
| `GOVERNOR_ENABLED` | on | over-search governor — cross-tool dedup + read-nudge (query mode only). |
| `DEDUP_JACCARD` | 0.8 | near-duplicate threshold vs prior searches this chat (across tavily+deep_research). |
| `READ_NUDGE_AFTER_K` | 4 | soft read-nudge after K combined searches; firm 'stop searching' at 2×. |

## Over-search governor — cross-tool participation (v1.2.0, 2026-06-12)

`deep_research` joins the shared over-search governor (designed + owned in `tavily_search.md` → "Over-search
governor"). The split is by **mode**:

- **`query` mode = a web SEARCH → governed.** It shares one per-chat budget + dedup set with `tavily_search`
  (a process-global `sys.modules` sentinel `_mh_governor_store`, byte-for-byte mirrored block). A `query`
  that's ≥ `DEDUP_JACCARD` similar to a prior `tavily_search` **or** `deep_research` search this chat is
  **skipped** (returns the `[over-search guard] Skipped…` note, no discovery search, no reads), and the call
  counts toward the combined read-nudge threshold.
- **`urls` mode = a READ → NOT governed.** Reading specific pages is the desired action; its URLs are noted as
  "reading happened" (so the nudge knows the model is reading), but it's never deduped or counted as a search.

**Why:** v1.1 governed only `tavily_search`, and the model **escaped the storm into the ungoverned
`deep_research`** (probe 10c: 6 tavily + 6 deep_research = 12 combined, round cap). Cross-tool state closes that
hatch. The injected `__chat_id__`/`__metadata__` are dunder params → stripped from the model-facing signature,
so the tool `specs` are **unchanged**. No chat_id → degrades off. Full mechanism + shared-state rationale (why
`sys.modules`, not `app.state`/Redis): `tavily_search.md` + provisioning `composition-design.md` §3/§8.

## Acceptance criteria

1. **Parallel multi-source read.** **VALIDATED 2026-06-07:** 3 Wikipedia pages read in **0.3 s**
   (concurrent) → one 30 K digest with 3 source sections.
2. **Query mode** (search→read). **VALIDATED:** "rsync delta-transfer algorithm" → searched, read the
   top result; the two bot-walled results (Medium 403, Facebook 400) reported as failures in the
   header, not faked.
3. **SSRF holds in the list.** **VALIDATED:** `127.0.0.1:8081` among the URLs → reported failure, others read.
4. **Routing:** the model reaches for `deep_research` for multi-source research, `read_page` for one
   page, `tavily_search` for plain discovery — not `deep_research` for a single page.

## Deferred / out of scope

- **Escalating a failed/JS-gated source to `render_page` *inside* the fan-out** (the tool itself doing
  the headless render) — still deferred: OWUI tools can't import each other, so it means embedding
  Playwright/Chromium in this tool (its own validation surface). **v1.1.0 ships the lightweight version**:
  the digest ROUTES the model to `render_page` on the specific JS-gated URL (`_escalation_hint`); the
  model performs the render. The heavy in-tool render is the future upgrade if routing proves insufficient.
- **LLM-in-the-tool** (per-source summarize / autonomous decompose) — deliberately not built
  (single-slot serialization + Gemma agentic-reliability risk; see "Why" above).
- Writing read pages into the self-generating `web-knowledge` RAG cache — natural future pairing
  (deep_research is a strong writer), but gated with that separate idea.
