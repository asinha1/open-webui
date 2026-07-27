"""mh-mcp — the agent-facing MCP server over the mh-grounding core (RFC-MH-005 P1).

One lean streamable-HTTP MCP server on loopback :8090 serving the grounding tools to
the desktop agent (Goose; any future MCP client). The logic is mh_grounding — the SAME
library the OWUI mh-tools wrap — so the two surfaces cannot drift. Tool DESCRIPTIONS
here are the agent-facing surface (worded for a coding/assistant agent); the OWUI
docstrings are tuned separately for the chat model. Keep this server small (≤ ~6 tools,
tight descriptions): MCP schemas load upfront into every agent turn, and forge-26B pays
that context tax.

P1: read_page (+ /health + /metrics). P2 (2026-07-24): tavily_search + deep_research with
the per-MCP-session over-search governor — one session = one persistent MCP connection
(a Goose session), keyed on id(ctx.session); dedup + read-nudge span both tools within a
session, mirroring the OWUI per-chat behavior. RAG tools are P3.

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

from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse, Response  # noqa: E402

from mh_grounding import __version__ as GROUNDING_VERSION  # noqa: E402
from mh_grounding import fetch, knowledge as knowledge_mod, metrics, research as research_mod, tavily as tavily_mod  # noqa: E402
from mh_grounding.governor import gov_state  # noqa: E402

# Instruments only (no separate exposition port) — /metrics is served on THIS port
# below, matching the port registry's `metrics: self` idiom.
metrics.init(port=None, client="mh-mcp")
metrics.init_audit(os.environ.get("MH_MCP_AUDIT_DIR", "~/service-data/mh-mcp/audit"))

TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")
OWUI_API_BASE = os.environ.get("OWUI_API_BASE", "http://100.100.81.77:8080")
OWUI_API_KEY = os.environ.get("OWUI_API_KEY", "")
# Identity gate for the BRIDGE tools (plan §5b — they act with the operator's OWUI key,
# so they are identity-bearing). Loopback callers = the operator's own machine account;
# tailnet callers must present a matching Tailscale-User-Login header (injected by
# Tailscale Serve). Anything else — including an undeterminable caller — is REFUSED.
OPERATOR_LOGIN = os.environ.get("MH_OPERATOR_LOGIN", "aashish.sinha94@gmail.com")

# knowledge_search corpus routing by caller identity (defense-in-depth: the full corpus
# holds infra mechanics / incident writeups that are attacker-useful; household members get
# a whitelist-authored PUBLIC corpus instead). IDs overridable via env.
KB_FULL = os.environ.get("MH_KB_FULL", "0854e216-8644-4d9e-95c3-d5f6727719e7")       # home-networking-repo
KB_PUBLIC = os.environ.get("MH_KB_PUBLIC", "887c307e-ace7-4889-a546-0ce058c67a63")   # home-networking-public
KB_REFERENCE = os.environ.get("MH_KB_REFERENCE", "82a7c3ab-4d3e-4008-9123-c11c18bad8e5")  # reference:python


def _is_operator(ctx: Context):
    """True only for the operator (loopback machine session, or the operator's Tailscale
    identity over Serve). Delegates to the pure is_operator_identity (testable core)."""
    host, login = _ctx_identity(ctx)
    return is_operator_identity(host, login)

# DNS-rebinding protection (the spec's Origin/Host validation — the SDK enforces it with
# 421s). Loopback identities plus the Tailscale Serve hostname; extend via MH_MCP_ALLOWED_HOSTS
# (comma-separated host:port) if the serve name ever changes.
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
_SERVE_HOST = os.environ.get("MH_MCP_SERVE_HOST", "hephaestus.tail31d045.ts.net:8443")
_ALLOWED_HOSTS = [f"127.0.0.1:{PORT}", f"localhost:{PORT}", _SERVE_HOST] + [
    h.strip() for h in os.environ.get("MH_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_ALLOWED_HOSTS,
    allowed_origins=[f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}",
                     f"https://{_SERVE_HOST}"],
)

mcp = FastMCP("mh-grounding", host=HOST, port=PORT, transport_security=_security)

# Tool annotations (MCP spec): client-UI hints — NOT model-visible, zero context
# cost; correct-by-construction for approval gating in clients that honor them.
_RO_OPEN = ToolAnnotations(readOnlyHint=True, openWorldHint=True)     # web readers
_RO_CLOSED = ToolAnnotations(readOnlyHint=True, openWorldHint=False)  # local RAG/bridge reads
_WRITE_LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                               idempotentHint=False, openWorldHint=False)  # note create


# --- identity resolution (PURE + testable; the routing/auth core) ------------------
LOOPBACK_HOSTS = ("127.0.0.1", "::1")


def is_operator_identity(host, login, operator_login=None):
    """Pure identity decision — the single source of truth for operator-vs-other, used by
    BOTH the bridge gate and the knowledge_search corpus routing. Testable without a live
    request.

    HEADER-AUTHORITATIVE (defense-in-depth, 2026-07-26 review): a Tailscale-Serve request
    ALWAYS carries the tailscaled-injected Tailscale-User-Login header, so if a login is
    present it is the SOLE authority — it must match the operator, regardless of the
    apparent client host. Only when NO identity header is present do we trust loopback as
    the operator's own local process. This removes any dependence on Serve preserving the
    caller's tailnet IP (a generic reverse-proxy-to-loopback would show client.host=127.0.0.1;
    Serve/tsnet does not, but we no longer rely on that). Fail-closed everywhere else."""
    op = (operator_login or OPERATOR_LOGIN).lower()
    if login:  # a present identity header decides, period (Serve-proxied tailnet caller)
        return login.strip().lower() == op
    return host in LOOPBACK_HOSTS  # no header → genuine local process = operator


def _ctx_identity(ctx: Context):
    """Extract (host, login) from a request context; ('' , '') if unavailable (→ non-operator)."""
    try:
        req = ctx.request_context.request
        host = req.client.host if req.client else None
        login = req.headers.get("Tailscale-User-Login", "")
        return host, login
    except Exception:
        return None, ""


def _bridge_refusal(ctx: Context):
    """None if the caller may use the identity-bearing bridge tools; else a refusal string.
    Fail-closed: no request handle / unknown host / wrong tailnet identity all refuse."""
    host, login = _ctx_identity(ctx)
    if is_operator_identity(host, login):
        return None
    return ("This bridge tool acts with the operator's OWUI credentials and is only "
            "available to the operator's own sessions (caller identity: "
            f"{login or host or 'unknown'}).")


# login -> short display name for metric labels (operator preference: names, not emails).
# Trust stays with the HEADER (tailscaled-derived, unspoofable); this only prettifies it.
# Env: MH_USER_NAMES="login1=name1,login2=name2"; unmapped logins fall back to the
# local-part of the login. Loopback has no header -> "local" (the operator's own machine).
_USER_NAMES = {}
for _pair in os.environ.get("MH_USER_NAMES", "aashish.sinha94@gmail.com=aashish").split(","):
    if "=" in _pair:
        _lg, _, _nm = _pair.partition("=")
        _USER_NAMES[_lg.strip().lower()] = _nm.strip()


def _caller_user(ctx: Context):
    """User label for metrics attribution — HEADER-AUTHORITATIVE, mirroring
    is_operator_identity: a present Tailscale-User-Login maps to its short name (Serve-proxied
    tailnet caller); otherwise a loopback caller is 'local' (operator's own process); else
    'unknown'. Household-scale label cardinality by construction."""
    try:
        req = ctx.request_context.request
        login = (req.headers.get("Tailscale-User-Login", "") or "").strip().lower()
        if login:
            return _USER_NAMES.get(login) or login.partition("@")[0]
        host = req.client.host if req.client else None
        return "local" if host in ("127.0.0.1", "::1") else "unknown"
    except Exception:
        return "unknown"


metrics.set_user_resolver(_caller_user)


def _session_gov(ctx: Context):
    """Per-MCP-session governor state — the agent-path analog of OWUI's per-chat key.
    One Goose session holds one persistent connection, so id(ctx.session) is stable for
    its lifetime and the dedup set + search budget span tavily_search AND deep_research
    within it. Ephemeral (server restart clears — same as the OWUI store)."""
    try:
        key = f"mcp-{id(ctx.session)}"
        metrics.session_seen(key)
        return gov_state(key)
    except Exception:
        return None


@mcp.tool(annotations=_RO_OPEN)
@metrics.instrument("read_page", "web")
async def read_page(url: str, ctx: Context = None, max_chars: int | None = None) -> str:
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


@mcp.tool(annotations=_RO_OPEN)
@metrics.instrument("tavily_search", "web")
async def tavily_search(query: str, ctx: Context, depth: str = "deep",
                        topic: str = "general", recency: str | None = None) -> str:
    """Search the live web — news, current versions, changelogs, statistics, anything past
    your training data or that you can't state with certainty. Prefer this over guessing;
    prefer read_page over re-searching once you have a promising URL. depth: "deep" for
    research-grade context (costs more), "quick" for a single-fact lookup. topic: "news"
    for current events (biases recent reputable sources), else "general". recency:
    optional window "day"|"week"|"month"|"year" (pair with topic="news").
    """
    result = await tavily_mod.search(
        query, depth=depth, topic=topic, recency=recency,
        cfg=tavily_mod.TavilyConfig(TAVILY_API_KEY=TAVILY_KEY),
        gov=_session_gov(ctx),
        on_gov_event=lambda kind: metrics.governor_event(kind, "tavily_search", user=_caller_user(ctx)),
    )
    return result.text


@mcp.tool(annotations=_RO_OPEN)
@metrics.instrument("deep_research", "web")
async def deep_research(ctx: Context, query: str | None = None,
                        urls: list[str] | None = None, max_sources: int = 5) -> str:
    """Research a topic across SEVERAL web pages in one step — they are read in parallel and
    returned as one consolidated digest to synthesize from. Use when multiple sources serve
    one question; use read_page for a single known page, tavily_search for plain discovery.
    Provide EITHER urls (read exactly those) OR query (search, then read the top results).
    Cite the digest's sources; unread sources are reported as failures, never invented.
    """
    result = await research_mod.research(
        query=query, urls=urls, max_sources=max_sources,
        cfg=research_mod.ResearchConfig(TAVILY_API_KEY=TAVILY_KEY),
        gov=_session_gov(ctx),
        on_gov_event=lambda kind: metrics.governor_event(kind, "deep_research", user=_caller_user(ctx)),
    )
    return result.text


@mcp.tool(annotations=_RO_CLOSED)
@metrics.instrument("knowledge_search", "rag")
async def knowledge_search(query: str, ctx: Context) -> str:
    """Search the operator's loaded-in reference knowledge: (1) the private home-network /
    self-hosted setup — devices (janus, metis, hephaestus), services, network/DNS/proxy
    configs, incident write-ups; AND (2) official documentation for the software this stack
    uses — Python libraries & frameworks (e.g. pydantic, numpy, aiohttp, starlette, chromadb,
    pdfplumber, pillow) and ops tools. Use it for questions about this home setup OR how to
    use one of those specific libraries/APIs — it beats the open web for both. Do NOT use it
    for general knowledge, news, or libraries outside that set (it returns nothing relevant;
    use tavily_search instead).
    """
    metrics.session_seen(f"mcp-{id(ctx.session)}")
    gov = knowledge_mod.kgov_state((f"mcp-{id(ctx.session)}", "knowledge"))
    # Operator -> full home-networking corpus; everyone else -> the PUBLIC household guide.
    # Reference-docs corpus is shared by all. The full corpus never reaches a non-operator —
    # and the non-operator's "not found" message must not even NAME the full corpus's
    # coverage (incidents/configs), so give the public path its own KB_DESCRIPTION.
    if _is_operator(ctx):
        cfg = knowledge_mod.KnowledgeConfig(KB_COLLECTION_ID=KB_FULL,
                                            REFERENCE_COLLECTION_IDS=KB_REFERENCE)
    else:
        cfg = knowledge_mod.KnowledgeConfig(
            KB_COLLECTION_ID=KB_PUBLIC, REFERENCE_COLLECTION_IDS=KB_REFERENCE,
            KB_DESCRIPTION=("the household's services guide (how to use the home services) and "
                            "official docs for common software libraries"))
    result = knowledge_mod.knowledge_query(query, cfg=cfg, gov=gov)
    return result.text


@mcp.tool(annotations=_RO_CLOSED)
@metrics.instrument("research_search", "rag")
async def research_search(query: str, ctx: Context) -> str:
    """Search the operator's SAVED web research — pages previously read and deliberately
    saved into per-topic research collections (finance, health, cooking, …). Each hit
    carries its source URL and saved-at date: these are SNAPSHOTS, so flag their age and
    re-verify time-sensitive values (rates, prices) with the live web before relying on
    them. Use before searching the web on a topic the operator researches recurrently.
    """
    metrics.session_seen(f"mcp-{id(ctx.session)}")
    gov = knowledge_mod.kgov_state((f"mcp-{id(ctx.session)}", "research"))
    result = knowledge_mod.research_query(query, cfg=knowledge_mod.KnowledgeConfig(), gov=gov)
    return result.text


@mcp.tool(annotations=_RO_CLOSED)
@metrics.instrument("owui_chat", "local")
async def owui_chat(ctx: Context, chat_id: str | None = None, recent: int = 10) -> str:
    """Read the operator's Open WebUI chats — the planning/research done on the phone or in
    the browser. Without chat_id: lists the most recent chats (id · title · when). With
    chat_id: returns that chat as a distilled transcript to work from. Use this to pick up
    a plan or research thread the operator started in OWUI chat.
    """
    from mh_grounding import bridge
    refusal = _bridge_refusal(ctx)
    if refusal:
        return refusal
    if chat_id:
        title, transcript = bridge.get_chat(chat_id)
        if title is None:
            return transcript  # the error string
        return f"# {title}\n\n{transcript}"
    import datetime
    try:
        rows = bridge.list_chats(recent)
    except Exception as e:
        log.warning("owui_chat list error: %s", e)
        return "Could not list chats right now."
    if not rows:
        return "No chats found."

    def _when(ts):
        try:
            return f"{datetime.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}" if ts else "?"
        except Exception:
            return "?"
    lines = [f"{r[0]} · {r[1]} · {_when(r[2])}" for r in rows]
    return "Recent OWUI chats (id · title · updated):\n" + "\n".join(lines)


@mcp.tool(annotations=_WRITE_LOCAL)
@metrics.instrument("save_owui_note", "local")
async def save_owui_note(ctx: Context, title: str, markdown: str) -> str:
    """Save a document into the operator's Open WebUI Notes — the write-back half of the
    OWUI⇄agent bridge. Use it to hand results, summaries, or plans from this session back
    to the operator's OWUI (readable on their phone). Write a DISTILLED, self-contained
    markdown document — not a raw transcript.
    """
    from mh_grounding import bridge
    refusal = _bridge_refusal(ctx)
    if refusal:
        return refusal
    if not OWUI_API_KEY:
        return "The bridge is not configured (no OWUI_API_KEY in the env file)."
    note_id, err = await bridge.save_note(title, markdown, OWUI_API_BASE, OWUI_API_KEY)
    if err:
        return err
    return f"Saved to OWUI Notes: “{title}” (note id {note_id})."


@mcp.custom_route("/whoami", methods=["GET"])
async def whoami(request: Request) -> Response:
    """Debug: echoes the caller identity the server perceives (client host + the Tailscale
    Serve identity headers). Use from the laptop to verify the P5 identity-gating path."""
    return PlainTextResponse(
        f"client={request.client.host if request.client else '?'}\n"
        f"Tailscale-User-Login={request.headers.get('Tailscale-User-Login', '')}\n"
        f"Tailscale-User-Name={request.headers.get('Tailscale-User-Name', '')}\n")


@mcp.custom_route("/setup-goose.sh", methods=["GET"])
async def setup_script(request: Request) -> Response:
    """Household onboarding: serve the Goose setup script tailnet-only (no repo clone /
    no credentials needed on the target machine). Canonical copy lives in the
    home_networking repo; 404s gracefully if the checkout moves."""
    path = Path.home() / "code/home_networking/provisioning/goose/setup-goose.sh"
    if not path.exists():
        return PlainTextResponse("setup script not found on this host", status_code=404)
    return Response(path.read_text(), media_type="text/x-shellscript")


@mcp.custom_route("/grab-session.sh", methods=["GET"])
async def grab_script(request: Request) -> Response:
    """Post-session grab: serve the laptop-side push script tailnet-only (run on the
    member's laptop after a session to push Goose data to hephaestus for analysis)."""
    path = Path.home() / "code/home_networking/provisioning/goose/grab-session.sh"
    if not path.exists():
        return PlainTextResponse("grab script not found on this host", status_code=404)
    return Response(path.read_text(), media_type="text/x-shellscript")


@mcp.custom_route("/KARINA-GUIDE.md", methods=["GET"])
async def setup_guide(request: Request) -> Response:
    """Household onboarding: the non-technical CLI guide, same serving rationale as the script."""
    path = Path.home() / "code/home_networking/provisioning/goose/KARINA-GUIDE.md"
    if not path.exists():
        return PlainTextResponse("guide not found on this host", status_code=404)
    return Response(path.read_text(), media_type="text/markdown")


@mcp.custom_route("/DESKTOP-GUIDE.md", methods=["GET"])
async def desktop_guide(request: Request) -> Response:
    """Household onboarding: the non-technical guide for the Goose Desktop app (shares
    config with the CLI; reuses setup-goose.sh for settings)."""
    path = Path.home() / "code/home_networking/provisioning/goose/DESKTOP-GUIDE.md"
    if not path.exists():
        return PlainTextResponse("guide not found on this host", status_code=404)
    return Response(path.read_text(), media_type="text/markdown")


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
