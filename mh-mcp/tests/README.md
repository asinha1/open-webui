# mh-mcp test suite

**Server-level regression tests for mh-mcp** — distinct from the usage-focused evals that
stay in their own repos (OWUI `provisioning/hephaestus/eval/`, Goose coding regression
`provisioning/hephaestus/eval/agent-coding/`, and the Goose lived-use analysis rounds
`provisioning/hephaestus/goose-analysis/`). Those test the *model/agent in the loop*; THIS
suite tests the **mh-mcp server's own behavior** — deterministic, model-free, fast, CI-able
— now that it carries an exposed endpoint + real authorization/routing logic.

Run:  `cd mh-mcp && .venv/bin/python -m pytest -q`   (needs the server up for LIVE tests)

## Test tiers
- **PURE** — imports `server`, tests pure logic (`is_operator_identity`, routing constants).
  No network/data. The identity/auth core.
- **DATA** — `mh_grounding.knowledge` against the live Chroma store; verifies corpus CONTENT
  isolation (needs OWUI's `vector_db` + the embedder).
- **LIVE** — a real MCP session to loopback `:8090` (= the OPERATOR path); exercises the
  protocol surface end-to-end. Skips if the server isn't up.

## Area 1 — Home-networking RAG routing (`test_routing.py`) ✅ THE BIG GAP, DONE
Locks down the security property "household members can't retrieve infra secrets via
knowledge_search" at three levels: (1) `is_operator_identity` decides operator-vs-other
(10 branch cases — loopback, tailnet+operator, tailnet+member, no-header, spoof, fail-closed);
(2) that decision selects the right KB collection; (3) the PUBLIC corpus contains no infra
CONTENT and its not-found message names no infra COVERAGE (a real metadata leak this suite
caught on first run — the public path now gets its own `KB_DESCRIPTION`), while the operator's
full corpus + live path DO return the detail. **Note:** the non-operator LIVE path can't be
tested from the operator's own box (loopback = operator; Serve injects the real identity) —
it's a documented **manual check from a household device**: on Karina's laptop, ask Goose an
infra question and confirm it does NOT get infra content. Run that after any routing change.

## Expansion areas (proposed — build as the server grows)
- **Area 2 — Bridge identity gate (`test_bridge.py`) ✅ DONE:** `_bridge_refusal` allows
  operator / refuses everyone else / fails closed (7 branch cases); a refused call is proven
  to NEVER reach the DB or write with the operator's key (monkeypatched short-circuit —
  incl. the critical `save_owui_note`-never-writes case); operator live-path lists chats.
- **Area 3 — Governor:** per-session dedup fires; cross-tool (tavily↔deep_research) shares
  a budget; sessions are isolated; read-nudge escalates. Mostly LIVE + governor-unit.
- **Area 4 — CRAG behavior:** on-domain hits ≥ 0.69; off-domain → actionable-empty (not
  `[]`); empty-cap after K; reference corpus reachable. DATA-tier.
- **Area 5 — Fetch/SSRF:** read_page refuses loopback/private/CGNAT + non-http; JS-render
  escalation fires; feed/JSON handling. (Overlaps the OWUI eval but here at the MCP surface.)
- **Area 6 — Transport/security:** DNS-rebind Host allowlist (bad Host → 421); `/whoami`
  identity echo; annotations present on all tools; the 7-tool roster is stable.
- **Area 7 — Metrics/audit:** a call increments `mh_tool_calls_total` with the right
  labels + writes an audit line with the resolved user.

Keep it server-behavior only. Anything needing the model or real agent flow belongs in the
usage-focused evals, not here.
