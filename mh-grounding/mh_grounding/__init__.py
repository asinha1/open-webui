"""mh-grounding — the shared grounding core (RFC-MH-005 P1).

One copy of the logic behind the mh-tools (OWUI, in-process) and mh-mcp (agent-facing
MCP server) surfaces. Pure logic only — no OWUI imports, no MCP imports, no client glue.
Client-facing surfaces (OWUI docstrings/Valves/events, MCP tool descriptions) live in the
adapters; this package owns fetch/render, Tavily, the over-search governor, and metrics
primitives. Model-facing return STRINGS live here deliberately — their wording is tuned
behavior shared by every client.
"""

__version__ = "0.1.0"
