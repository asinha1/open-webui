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
tavily_search.py   — web search (Tavily REST via aiohttp; no SDK dep)
tavily_search.md   — its design rationale + acceptance criteria
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
