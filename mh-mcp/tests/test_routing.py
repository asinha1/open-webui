"""Home-networking RAG routing — the security regression (mh-mcp test suite, area 1).

The load-bearing property: household members must NOT be able to retrieve infra secrets
via knowledge_search. Enforced by IDENTITY-ROUTED corpus selection — operator → full
`home-networking-repo`; everyone else → the whitelist-authored `home-networking-public`.
This suite locks that down at three levels so a refactor can't silently regress it:

  1. IDENTITY  (pure)  — is_operator_identity() decides operator-vs-other correctly.
  2. ROUTING   (pure)  — that decision selects the right KB collection id.
  3. ISOLATION (data)  — the public corpus contains NO infra content (not filtered —
                         absent), the full corpus does, and the operator live-path gets it.

Run: ../.venv/bin/python -m pytest tests/test_routing.py -v   (from mh-mcp/)
"""
import pytest

from conftest import call_text, open_mcp, server_up

OPERATOR = "aashish.sinha94@gmail.com"
KARINA = "alkhasyankarina@gmail.com"
# adversarial infra probes — must return NOTHING useful to a non-operator
INFRA_PROBES = [
    "how is SSH bound on janus",
    "nginx tailnet bind port 443",
    "incident postmortem root cause",
    "AdGuard DNS listener configuration",
    "cloudflare token secret location",
]
# things deliberately kept OUT of the public corpus (not-yet-public)
EXCLUDED_TOPICS = ["canvas image generation", "comfyui face swap", "render bridge", "governor profile"]
INFRA_MARKERS = ("listenaddress", "100.111", "sshd", "cloudflare", "adguard", "comfy",
                 "canvas", "render", "governor", "incident")


# ---- 1. IDENTITY (pure) — the single source of truth for operator-vs-other -------------

@pytest.mark.parametrize("host,login,expect", [
    ("127.0.0.1", "",                 True),   # loopback = operator's own machine
    ("::1",       "",                 True),   # ipv6 loopback
    ("127.0.0.1", "anyone@x.com",     True),   # loopback wins regardless of header
    ("100.100.81.77", OPERATOR,       True),   # tailnet + operator login
    ("100.100.81.77", OPERATOR.upper(), True), # case-insensitive
    ("100.100.81.77", f"  {OPERATOR} ", True), # whitespace-tolerant
    ("100.73.203.55", KARINA,         False),  # tailnet + household member -> NOT operator
    ("100.73.203.55", "",             False),  # tailnet + no identity header -> NOT operator
    ("100.73.203.55", "spoof@evil.com", False),# wrong login -> NOT operator
    (None,            "",             False),  # no host handle -> fail closed
])
def test_identity_branches(srv, host, login, expect):
    assert srv.is_operator_identity(host, login) is expect


def test_identity_is_the_routing_gate(srv):
    """The bridge gate and corpus routing MUST use the same identity core (no divergence)."""
    # operator → full corpus; non-operator → public corpus (mirrors server.knowledge_search)
    assert (srv.KB_FULL if srv.is_operator_identity("127.0.0.1", "") else srv.KB_PUBLIC) == srv.KB_FULL
    assert (srv.KB_FULL if srv.is_operator_identity("100.73.203.55", KARINA) else srv.KB_PUBLIC) == srv.KB_PUBLIC


# ---- 2. ROUTING (pure) — decision → collection id -------------------------------------

def test_corpus_ids_distinct_and_set(srv):
    assert srv.KB_FULL and srv.KB_PUBLIC and srv.KB_FULL != srv.KB_PUBLIC
    assert srv.KB_REFERENCE  # reference corpus shared by all


# ---- 3. ISOLATION (data) — the public corpus cannot leak infra ------------------------

@pytest.fixture(scope="module")
def kq():
    from mh_grounding import knowledge as K
    return K


def _public_cfg(kq, srv):
    """Mirror the server's non-operator config, incl. the public KB_DESCRIPTION (so the
    not-found message doesn't name the full corpus's coverage)."""
    return kq.KnowledgeConfig(
        KB_COLLECTION_ID=srv.KB_PUBLIC, REFERENCE_COLLECTION_IDS="",
        KB_DESCRIPTION=("the household's services guide (how to use the home services) and "
                        "official docs for common software libraries"))


@pytest.mark.parametrize("probe", INFRA_PROBES + EXCLUDED_TOPICS)
def test_public_corpus_returns_no_infra_content(srv, kq, probe):
    """A non-operator query for infra/excluded topics returns NO sensitive DOCUMENT content
    — because it was never ingested (whitelist), not merely filtered. Empty = pass; if any
    doc IS returned, it must carry no infra markers."""
    r = kq.knowledge_query(probe, cfg=_public_cfg(kq, srv))
    if r.empty:
        return  # nothing retrieved = nothing leaked (the expected case)
    leaked = [m for m in INFRA_MARKERS if m in str(r.hits).lower()]
    assert not leaked, f"public corpus returned infra content {leaked!r} for {probe!r}"


@pytest.mark.parametrize("probe", INFRA_PROBES + EXCLUDED_TOPICS)
def test_public_notfound_message_names_no_infra(srv, kq, probe):
    """Even the 'not found' MESSAGE must not name the full corpus's coverage (no
    'incidents'/infra hint to a household member) — the metadata-leak regression."""
    r = kq.knowledge_query(probe, cfg=_public_cfg(kq, srv))
    if not r.empty:
        return
    hinted = [m for m in ("incident", "cloudflare", "sshd", "listenaddress", "comfy", "render bridge")
              if m in r.text.lower()]
    assert not hinted, f"public not-found message hints {hinted!r} for {probe!r}"


def test_full_corpus_HAS_infra_for_operator(srv, kq):
    """Control: the operator's full corpus DOES contain the infra detail (routing isn't
    just breaking retrieval for everyone)."""
    cfg = kq.KnowledgeConfig()  # defaults = full home-networking-repo
    r = kq.knowledge_query("how is SSH bound on janus", cfg=cfg)
    assert not r.empty and r.hits, "full corpus should return SSH-bind detail for the operator"
    assert any(h[0] >= 0.69 for h in r.hits)  # CRAG threshold clears on-domain


def test_public_corpus_serves_household_queries(srv, kq):
    """The public corpus is USEFUL — a household 'what services can I use' hits it."""
    cfg = kq.KnowledgeConfig(KB_COLLECTION_ID=srv.KB_PUBLIC, REFERENCE_COLLECTION_IDS="")
    r = kq.knowledge_query("what services can I use at home", cfg=cfg)
    assert not r.empty and r.hits, "public corpus should answer household service questions"


# ---- 3b. ISOLATION (live) — the operator path over MCP returns the full corpus --------

@pytest.mark.asyncio
async def test_live_operator_gets_full_corpus():
    """End-to-end: loopback (operator) knowledge_search on an infra query reaches the full
    corpus. (The non-operator live path can't be tested from the operator's own box — it's
    a documented manual check from a household device; see tests/README.md.)"""
    if not await server_up():
        pytest.skip("mh-mcp server not running")
    async with open_mcp() as s:
        txt = await call_text(s, "knowledge_search", {"query": "how is SSH bound on janus"})
    assert "Found" in txt and "relevant document" in txt, f"operator live path did not reach full corpus: {txt[:120]}"
