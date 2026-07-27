"""Shared fixtures for the mh-mcp test suite.

Tiers (a test declares its needs via marks / fixtures):
  - PURE   : imports server, tests pure logic. No network, no data. Always runnable.
  - DATA   : uses mh_grounding.knowledge against the live Chroma store (needs OWUI's
             vector_db + the MiniLM embedder). Verifies corpus CONTENT isolation.
  - LIVE   : opens a real MCP session to the running mh-mcp (loopback :8090). Needs the
             server up. Exercises the protocol surface end-to-end (operator path).
"""
import sys
from pathlib import Path

import pytest

MH_MCP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MH_MCP))

MCP_URL = "http://127.0.0.1:8090/mcp"


@pytest.fixture(scope="session")
def srv():
    """The imported server module (pure-function + routing-constant access)."""
    import server
    return server


from contextlib import asynccontextmanager


@asynccontextmanager
async def open_mcp():
    """Open a live MCP ClientSession to loopback mh-mcp (= the OPERATOR identity path).
    Used as `async with open_mcp() as s:` INSIDE a test so the client's nested async
    context managers enter AND exit in the same task (anyio cancel-scope requirement —
    an async-generator *fixture* enters/exits in different tasks under pytest-asyncio and
    trips 'exit cancel scope in a different task')."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


async def server_up():
    """True if mh-mcp answers on loopback — for a real (not bug-masking) skip guard."""
    import urllib.request
    try:
        urllib.request.urlopen(MCP_URL.replace("/mcp", "/health"), timeout=3).read()
        return True
    except Exception:
        return False


async def call_text(session, tool, args):
    """Call an MCP tool, return its text result."""
    res = await session.call_tool(tool, args)
    return res.content[0].text if res.content else ""
