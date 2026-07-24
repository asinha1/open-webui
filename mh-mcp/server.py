"""mh-mcp — the agent-facing MCP server over the mh-grounding core (RFC-MH-005 P1).

One lean streamable-HTTP MCP server on loopback :8090 serving the grounding tools to
the desktop agent (Goose; any future MCP client). The logic is mh_grounding — the SAME
library the OWUI mh-tools wrap — so the two surfaces cannot drift. Tool DESCRIPTIONS
here are the agent-facing surface (worded for a coding/assistant agent); the OWUI
docstrings are tuned separately for the chat model. Keep this server small (≤ ~6 tools,
tight descriptions): MCP schemas load upfront into every agent turn, and forge-26B pays
that context tax.

P1 scope: read_page only (+ /health + /metrics). P2 adds tavily_search + deep_research
with the per-MCP-session governor. RAG tools are P3.

Run (see README.md / the launchd plist):
    .venv/bin/python server.py
Config: ~/service-data/mh-mcp/env (KEY=value lines, mode 600) — loaded at startup;
nothing secret in the plist or this file.
"""

import logging
import os
import sys
from pathlib import Path

# ---- env file (secrets + overrides; launchd can't source files) -------------------
ENV_FILE = Path.home() / "service-data" / "mh-mcp" / "env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

HOST = os.environ.get("MH_MCP_HOST", "127.0.0.1")   # loopback until the Tailscale Serve phase
PORT = int(os.environ.get("MH_MCP_PORT", "8090"))    # fleet ports.yaml: hephaestus 8090

logging.basicConfig(
    level=os.environ.get("MH_MCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mh.mcp")

from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse, Response  # noqa: E402

from mh_grounding import __version__ as GROUNDING_VERSION  # noqa: E402
from mh_grounding import fetch, metrics  # noqa: E402

# Instruments only (no separate exposition port) — /metrics is served on THIS port
# below, matching the port registry's `metrics: self` idiom.
metrics.init(port=None, client="mh-mcp")

mcp = FastMCP("mh-grounding", host=HOST, port=PORT)


@mcp.tool()
@metrics.instrument("read_page", "web")
async def read_page(url: str, max_chars: int | None = None) -> str:
    """Read a web page, article, doc page, or RSS/Atom feed and return it as Markdown
    (links preserved, so you can follow one by calling read_page again). USE THIS TO
    VERIFY before you state: a URL you are about to cite, a version number, an API's
    actual signature, a changelog claim — never present a guessed URL or version as
    fact when you could read the source. Handles JavaScript-rendered pages
    automatically (internal headless-browser escalation). Only public http(s) URLs —
    internal/private/tailnet addresses are refused.

    Args:
        url: absolute http(s) URL. A #fragment focuses on that section of the page.
        max_chars: optional cap on returned characters (raise for a long enumeration
            like a full feed/episode list).
    """
    result = await fetch.read_url(url, max_chars=max_chars, cfg=fetch.FetchConfig())
    return result.text


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return PlainTextResponse(
        f"ok mh-mcp grounding={GROUNDING_VERSION} render={fetch.render_available()}")


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics_route(request: Request) -> Response:
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
    except ImportError:
        return PlainTextResponse("prometheus-client not installed", status_code=501)


if __name__ == "__main__":
    log.info("mh-mcp starting on %s:%s (grounding %s, render=%s)",
             HOST, PORT, GROUNDING_VERSION, fetch.render_available())
    if HOST not in ("127.0.0.1", "localhost"):
        log.warning("non-loopback bind %s — only expected in the Tailscale Serve phase "
                    "(and mind the macOS app-firewall gotcha for tailnet binds)", HOST)
    sys.exit(mcp.run(transport="streamable-http"))
