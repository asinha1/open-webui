# tavily_search — design & rationale

Companion to `tavily_search.py`. Why it's shaped the way it is, what's deliberately deferred, and how to know it works.

## What it is

A web-search tool for Open WebUI / Gemma 4, backed by the Tavily `/search` REST API. Built from analysis of a community tool (`victor1203/web_search_with_tavily`) used as a probe. It calls the documented REST endpoint over `aiohttp` (in OWUI's venv), so there is **no `tavily-python` dependency** and no SDK version-fragility.

## The model-facing surface (the heart of it)

Tool-calling reliability on a 31B drops with every extra parameter, so the model sees a curated **4-param** surface; everything else is an operator Valve.

| Model param | Type | Maps to | Purpose |
|---|---|---|---|
| `query` | str (required) | `query` | the search text |
| `depth` | `"quick"`/`"deep"` (default `deep`) | `search_depth` basic/advanced | quality vs cost/latency |
| `topic` | `"general"`/`"news"` (default `general`) | `topic` | news adds publish dates, recency bias |
| `recency` | `day`/`week`/`month`/`year` (opt) | `time_range` | "this week"-style windows |

Friendly names (`quick`/`deep`) call more reliably than API jargon (`basic`/`advanced`). `Literal` types become JSON-schema enums, so the model can only pick valid values. The `:param:` docstring lines become the per-argument descriptions the model reads — that's the "when to use deep vs quick / news" guidance, and the **prompt-layer trigger** lives here, not in the system prompt.

**Valves (operator-only, model can't see):** `TAVILY_API_KEY`, `MAX_RESULTS` (clamped 1–10, default **4**), `INCLUDE_ANSWER`, `MAX_CONTENT_CHARS` (per-result truncation, default **1500**), `TIMEOUT`. (Sizing went 5/1500 → 3/800 → 4/1500 on 2026-05-30 — see "Trim rationale" below.)

### Locked design decisions (2026-05-30)

1. **Raw-page reading is a separate `read_page` tool**, not a flag here — keeps search single-purpose and cheap; ~20k-char page reads are opt-in. (`read_page` = the deferred `fetch_url`, built next.)
2. **`include_domains` omitted from the model surface in v1** — keeps the surface at 4 params for 31B reliability. It's per-query (not a sensible global Valve), so it's simply not exposed yet; add it as a 5th param if source-restricted search proves needed in real use.
3. **Default depth = `deep`** (advanced, 2 credits, ~10× content + usable relevance scores); the model picks `quick` for fast facts. `include_usage=true` logs credit burn so we can set a smarter default from real data.
4. **Lives in the fork's `mh-tools/` dir as file #1** (version-controlled with the OWUI deployment), deployed into OWUI by UI import.

### Trim rationale (2026-05-30)

Defaults dropped from **5 results / 1500 chars → 3 / 800** after a real failure. A follow-up question that fired **4 searches in one turn** injected **~10K tokens** of results; the synthesis request then hit **18,443 tokens against a 16,384 slot** (`--ctx-size 32768 --parallel 2`), so llama-server returned `send_error: request exceeds context` and decoded **zero tokens** — empty answer, sources pill already shown.

Two structural facts drove the new defaults:
1. The truncation guard is **per-result**, so it can't bound the **aggregate** across sibling calls in one turn — the only lever the tool has is a smaller per-call payload.
2. Analysis of that exact turn: the trophy counts lived in each source's **first ~300–800 chars**; the truncated tail was narrative, and results ranked **[3]–[5] were fixtures / social posts / nav chrome** carrying no data. `answer` + 3 results reproduced the table losslessly.

This pairs with the **2026-05-30 window bump to `--ctx-size 131072` (64K/slot)**: the window holds many *conversation* rounds, the trim keeps each round's *noise* low.

**Relaxed 3/800 → 4/1500 (same day), after lived use.** The first comparison query post-bump came back mostly right but hedged one field as **"4+"** where the real count was **9** — the `N+` signature of a *truncated enumeration* (the full trophy list didn't fit 800 chars / a dropped result). Two facts made relaxing the clear call: (1) the budget reason for trimming was gone at 64K — even a 10-search turn at 4×1500 is ~17K tokens; (2) result count doesn't affect Tavily billing, so the trim never saved money. So content went back to 1500 (enumerations survive) and results to 4 (still drops the worst noise-ranked tail). The result *cap* stays because 64K alone can still be swamped by a pathological 15-search turn — defense-in-depth, not budget.

## Behavior worth knowing

- **Response shaping:** leads with Tavily's `answer`, then numbered `[1] [2] …` results (title — url, optional `published:` date, truncated `content`). The `[n]` indices match the emitted **citation events** (Source chips) and align with the RAG `[id]` convention so citations are uniform across web + KB.
- **`content`, not `snippet`:** the community tool read a nonexistent `snippet` field and silently dropped every result's body. Tavily returns `content`; we read `content`. (Empirically verified against the live API.)
- **Cost gating:** `deep` adds `chunks_per_source=3`; per-result content truncated to `MAX_CONTENT_CHARS`; `raw_content` never requested (that's `read_page`'s job). At the 4×1500 defaults a deep call is ~2k tokens. NB result count / content length do **not** affect billing — Tavily charges per search *depth* (`deep`=2 credits, `quick`=1), so trimming saves context + latency, not money.
- **Error taxonomy the model can act on:** 401 → "unavailable (auth)"; 429 → "quota exhausted, answer from your knowledge"; timeout / network → graceful degrade. Never raises into the chat.
- **Telemetry:** every call logs `depth=… topic=… recency=… results=… usage=… q=…` to **`open-webui.out.log`** (OWUI renames the logger to `tool_<id>` — here `tool_tavily_web_search` — and INFO goes to stdout). Tally monthly burn with `grep 'tavily_search' ~/Library/Logs/open-webui.out.log`. **Measured: a `deep` search = 2 credits** (≈500/mo on the free 1K tier).

## "Advanced" — what the probe measured

`advanced` is a depth/quality knob, not a query type. Same query, basic vs advanced: content per result ~150 chars → ~1,800 chars (multiple semantic snippets/source); scores go from flat `1.0` to graded/usable; latency ~1.1s → ~3.4s; cost 1 → 2 credits. "Complex queries" come from the *param surface* (topic/recency/domains/exact_match), not from depth.

## Acceptance (probe-style, verify in UI + logs)

- [ ] A "what happened this week" question → model calls with `topic=news` + `recency`, results carry publish dates.
- [ ] A research question → model uses `depth=deep`; a quick fact → `depth=quick`.
- [ ] Each result shows `content` (not empty); Source chips render and match the `[n]` markers.
- [ ] `raw_content` never appears in model context.
- [ ] A forced 401/429 returns a graceful message, not a stack trace.
- [ ] Built-in `web_search` feature is OFF on Gemma (this tool owns the web path); restart-after-enable done.

## Portability (bottleneck-watch hedge)

The 4-param surface maps 1:1 to a future MCP `tavily_search` tool, so porting to a swap-proof MCP server (if we ever move off OWUI) is mechanical, not a rewrite.
