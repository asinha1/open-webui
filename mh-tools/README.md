# mh-tools — custom Open WebUI tools

Our bespoke tools for this OWUI deployment (Gemma 4), version-controlled **inside this fork** so they travel with the deployment artifact. This directory is ours — upstream OWUI has no `mh-tools/`, so it won't conflict on rebase/merge.

## Why here (and not a separate repo)

OWUI loads tools from `webui.db`, not the filesystem — so the repo choice is purely *where the source-of-truth lives*. Since we **dockerize from this fork**, keeping tools here means the image build context already contains them (seed them into the DB at build time, no second repo to pull). OWUI Tools aren't portable across frontends anyway — the portability play is **MCP** (a separate artifact).

## The mh-tools ⇄ MCP boundary

- **MCP server** — for anything **reusable across clients** or with a good prebuilt server (Google Workspace, GitHub, …). Speaks a standard protocol, survives a frontend swap. Prefer this for portable capability.
- **mh-tools (here)** — for **OWUI-specific, bespoke glue** for this setup. Keep it thin. If a tool turns out reusable, promote it to an MCP server rather than growing this dir.

Tool param surfaces here are designed to **map 1:1 to a future MCP tool**, so promotion is mechanical.

## Contents

```
tavily_search.py     — web search (Tavily REST via aiohttp; no SDK dep)
tavily_search.md     — its design rationale + acceptance criteria
read_page.py         — fetch one URL/feed → full readable body; escalates JS-gated pages to a headless-Chromium render INTERNALLY (v1.4 fold of the former render_page; SSRF-guarded; dep: playwright + chromium)
read_page.md         — its design rationale + acceptance criteria
export_document.py   — render the model's content to a downloadable .md/.pdf file + return a download link (markdown + fpdf2; no new dep)
export_document.md   — its design rationale + acceptance criteria
deep_research.py     — read several sources in parallel (urls, or query→search→read) and return one consolidated digest (parallel tool-I/O, single-pass synthesis; SSRF-guarded; no new dep)
deep_research.md     — its design rationale + acceptance criteria
knowledge_search.py  — owned, governed RAG over the home-network KB (replaces OWUI's built-in; CRAG relevance grade + a per-turn empty-loop guard; RFC-MH-001)
knowledge_search.md  — its design rationale + acceptance criteria
save_research.py     — the "Save Sources" ACTION (OWUI function, type=action, id save_sources): a click saves the pages read this chat into a research:<domain>/<subtopic> Knowledge collection (two-step input dialog → re-fetch via get_content_from_url → save_docs_to_vector_db; provenance + url-hash dedup). NOT a model-routed tool — the button writes, never the model. Design: tooling-research/self-generating-rag-design.md (RFC-MH-002).
save_research.md     — its design rationale + acceptance criteria
research_search.py   — the model-facing READ tool for the saved research:* collections (mirrors knowledge_search: query_collection + CRAG 0.69 + per-turn governor; surfaces each hit's source URL + saved_at). Separate from knowledge_search; unification long-term-open. Design: same RFC-MH-002 doc.
research_search.md   — its design rationale + acceptance criteria
research-topics.json — the curated topic registry / breadth-cap (seed domains: finance, health, cooking; new domains = an operator edit)
redeploy.py          — [mh] push these .py into a running OWUI via the tools-update API (re-parse, NOT a raw webui.db swap); needs OWUI_API_KEY, then restart OWUI. Automates the manual re-paste in "How a tool gets into Open WebUI" below.
```

Each tool = one `.py` (the OWUI Tool) + a `.md` spec.

## How a tool gets into Open WebUI

1. **Deploy:** **Workspace → Tools** → ＋ → paste the `.py` → Save. (It's a *Tool* — a `class Tools:` file. Not a *Function* (those are Pipes/Filters/Actions), and not a *Tool Server* (that's the external OpenAPI/MCP integration).)
2. **Configure Valves:** the tool's gear/Valves → set `TAVILY_API_KEY` — keys live in Valves (DB), never committed here.
3. **Enable:** in the Gemma model editor (Workspace → Models → Gemma — the same place you set Function Calling: Native) → Tools → toggle this tool on → Save.
4. **Restart OWUI** — model tool/RAG binding is not reliably re-read from a live DB write, so a re-paste needs a restart to take effect (substitute your own launchd label):
   ```bash
   U=$(id -u)
   launchctl bootout gui/$U/com.example.open-webui 2>/dev/null
   launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.example.open-webui.plist
   ```
5. **Update:** edit here → re-paste into the existing OWUI tool → Save → restart.

When OWUI is dockerized (post-soak), the image build should **seed these tools into the DB deterministically** from this directory — that replaces the manual paste.

## Conventions

- Secrets live in OWUI **Valves**, per-deployment, never committed (`.gitignore` in the fork guards strays).
- **No new pip deps** when avoidable — prefer OWUI's venv (`aiohttp`, `requests`, `pydantic`).
- Errors **degrade gracefully** into a model-readable string (auth/quota/timeout), never a stack trace.
- Curated model-facing param surface (≤4–5 typed/enum args for 31B reliability); everything else is a Valve.
- **Usage metrics:** wrap a tool's entry method with `@instrument("<tool>", "<web|rag|local>")` (from `open_webui.utils.telemetry.mh_tools`) → Prometheus `:9094` (`mh_tool_calls_total` / `mh_tool_duration_seconds`). Signature-transparent (`functools.wraps`), so the OWUI tool spec is unchanged. Visualized in the `mh-ai-stack` Grafana dashboard.
