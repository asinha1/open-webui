# mh-mcp — agent-facing MCP server (RFC-MH-005)

The grounding tools as a **streamable-HTTP MCP server** on `127.0.0.1:8090`, for the
desktop agent (Goose) and any future MCP client. Serves (P2, 2026-07-24): `read_page`,
`tavily_search`, `deep_research` — with the **per-MCP-session over-search governor**
(one agent session = one cross-tool dedup set + search budget, keyed on the MCP session). Logic lives in `../mh-grounding/` —
the same library the OWUI `mh-tools/` wrap; this file is only the agent-facing surface.
Plan + phasing: `home_networking/provisioning/hephaestus/tooling-research/mcp-promotion-plan.md`.

## Provision (uv-only, own venv — nothing lands in OWUI's venv)

```sh
cd ~/repos/open-webui/mh-mcp
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e "../mh-grounding[render,metrics]" "mcp>=1.9" 
# headless-render browser (shared per-user cache with the OWUI install; ~no-op if present):
.venv/bin/python -m playwright install chromium
```

Config/secrets: `~/service-data/mh-mcp/env` (KEY=value, `chmod 600`), loaded by
`server.py` at startup — never in the plist. Keys used: `TAVILY_API_KEY` (P2),
`MH_MCP_HOST` / `MH_MCP_PORT` / `MH_MCP_LOG_LEVEL` (optional overrides).

## Run

- Service: LaunchAgent `com.maisonhanover.mh-mcp`
  (artifact: `home_networking/provisioning/hephaestus/artifacts/mh-mcp.plist`).
- Manual: `.venv/bin/python server.py`
- Health: `curl 127.0.0.1:8090/health` · Metrics: `curl 127.0.0.1:8090/metrics`
  (`metrics: self` — same port, per the fleet port registry).
- MCP endpoint (for Goose / OWUI `type:'mcp'` tool-server config): `http://127.0.0.1:8090/mcp`

## Rules of the house

- **Loopback only** until the Tailscale Serve phase (P5) — never a LAN bind.
- **Keep it lean** (≤ ~6 tools, tight descriptions): schemas load upfront into every
  agent turn; forge-26B pays the context tax.
- Port 8090 is registered in `provisioning/fleet/ports.yaml` — opening any new port
  means adding its row there in the same change.
- Tool logic changes go in `mh-grounding` (shared with OWUI); only agent-facing
  descriptions/params belong here. Param surfaces stay 1:1 with the OWUI tools.
