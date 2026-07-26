"""Bridge identity gate — the security regression (mh-mcp test suite, area 2).

The load-bearing property: the bridge tools (`owui_chat`, `save_owui_note`) act with the
OPERATOR's OWUI credentials, so a non-operator caller must be refused BEFORE any database
read or any write with the operator's key. This suite locks that at three levels:

  1. GATE   (pure)  — _bridge_refusal() allows operator, refuses everyone else, fail-closed.
  2. SHORT-CIRCUIT (unit) — a refused call NEVER invokes bridge.list_chats/get_chat/save_note
                    (no DB read, no OWUI-key write) — proven by monkeypatching them to blow up.
  3. OPERATOR PATH (live) — loopback (operator) owui_chat list actually works (read-only).

Run: ../.venv/bin/python -m pytest tests/test_bridge.py -v   (from mh-mcp/)
"""
import pytest

from conftest import call_text

OPERATOR = "aashish.sinha94@gmail.com"
KARINA = "alkhasyankarina@gmail.com"


# ---- fake request context (construct any caller identity, no live tailnet needed) -----
def fake_ctx(host, login=""):
    req = type("Req", (), {})()
    req.client = type("Client", (), {"host": host})() if host else None
    req.headers = {"Tailscale-User-Login": login} if login else {}
    rc = type("RC", (), {"request": req})()
    ctx = type("Ctx", (), {"request_context": rc})()
    return ctx


def broken_ctx():
    """A ctx whose request access raises — the fail-closed path."""
    class Bad:
        @property
        def request_context(self):
            raise RuntimeError("no request context")
    return Bad()


# ---- 1. GATE (pure) --------------------------------------------------------------------

@pytest.mark.parametrize("ctx_args,allowed", [
    (("127.0.0.1", ""),               True),   # loopback = operator's own machine
    (("::1", ""),                     True),
    (("100.100.81.77", OPERATOR),     True),   # tailnet + operator identity
    (("100.100.81.77", OPERATOR.upper()), True),
    (("100.73.203.55", KARINA),       False),  # household member -> REFUSED
    (("100.73.203.55", ""),           False),  # no identity header -> REFUSED
    (("100.73.203.55", "spoof@evil.com"), False),
])
def test_gate_decisions(srv, ctx_args, allowed):
    refusal = srv._bridge_refusal(fake_ctx(*ctx_args))
    assert (refusal is None) is allowed


def test_gate_fails_closed_on_broken_ctx(srv):
    refusal = srv._bridge_refusal(broken_ctx())
    assert refusal is not None and "operator" in refusal.lower()


def test_refusal_message_leaks_no_chat_content(srv):
    """The refusal names the caller identity but must be a fixed message — never chat data."""
    msg = srv._bridge_refusal(fake_ctx("100.73.203.55", KARINA))
    assert KARINA in msg and "credentials" in msg.lower()
    assert "\n" not in msg  # single-line fixed message, not a dumped transcript


# ---- 2. SHORT-CIRCUIT (unit) — a refused call touches NOTHING --------------------------

@pytest.mark.asyncio
async def test_owui_chat_refused_never_reads_db(srv, monkeypatch):
    tripped = []
    monkeypatch.setattr("mh_grounding.bridge.list_chats", lambda *a, **k: tripped.append("list") or [])
    monkeypatch.setattr("mh_grounding.bridge.get_chat", lambda *a, **k: tripped.append("get") or (None, ""))
    out = await srv.owui_chat(fake_ctx("100.73.203.55", KARINA), recent=5)
    assert "operator" in out.lower() and "only available" in out.lower()
    assert tripped == [], f"refused owui_chat still hit the DB: {tripped}"


@pytest.mark.asyncio
async def test_save_note_refused_never_writes(srv, monkeypatch):
    """The critical one: a non-operator must NOT get a note written with the operator's key."""
    wrote = []
    async def _boom(*a, **k):
        wrote.append("save"); return ("id", None)
    monkeypatch.setattr("mh_grounding.bridge.save_note", _boom)
    out = await srv.save_owui_note(fake_ctx("100.73.203.55", KARINA), "t", "body")
    assert "operator" in out.lower()
    assert wrote == [], "refused save_owui_note still called bridge.save_note (would write with operator key!)"


@pytest.mark.asyncio
async def test_operator_owui_chat_reaches_bridge(srv, monkeypatch):
    """Control: an OPERATOR (loopback) call DOES pass the gate and reach the bridge."""
    reached = []
    monkeypatch.setattr("mh_grounding.bridge.list_chats",
                        lambda *a, **k: reached.append("list") or [("id1", "Chat One", 1700000000)])
    out = await srv.owui_chat(fake_ctx("127.0.0.1", ""), recent=5)
    assert reached == ["list"], "operator owui_chat did not reach the bridge"
    assert "Chat One" in out


# ---- 3. OPERATOR PATH (live) — loopback list actually works ---------------------------

@pytest.mark.asyncio
async def test_live_operator_can_list_chats(mcp_session):
    txt = await call_text(mcp_session, "owui_chat", {"recent": 3})
    # operator path: either lists chats or says none — but NOT the refusal
    assert "operator's OWUI credentials" not in txt, f"operator was refused on loopback: {txt[:100]}"
    assert ("Recent OWUI chats" in txt) or ("No chats" in txt)
