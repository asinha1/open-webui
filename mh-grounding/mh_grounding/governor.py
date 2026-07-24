"""Over-search governor (Thread #2, v1.2 cross-tool) — the SINGLE copy.

Moved verbatim from the block formerly MIRRORED byte-for-byte in mh-tools/tavily_search.py
and mh-tools/deep_research.py (DB-stored OWUI tools can't import a sibling module; this
library ends that duplication).

State model is unchanged: a process-global sys.modules sentinel ("_mh_governor_store") so
that (a) inside OWUI, both tool wrappers and the eval harness reach the SAME per-chat
budget + dedup set, exactly as before, and (b) inside mh-mcp, all served tools share one
store. NOTE: shared code is NOT shared state — the OWUI process and the mh-mcp process
each hold their own store; an OWUI chat and an agent MCP session budget independently
(deliberate — they are different sessions). Ephemeral (lost on restart — fine, over-search
is within-conversation). Swap point for a future multi-worker deploy: back gov_store()
with a Redis dict — see composition-design.md.
"""

import re
import sys
import types

GOV_MAX_CHATS = 200
GOV_STOPWORDS = frozenset(
    "the a an of to in for on at and or vs with from by is are be as what which how when "
    "where who whom current latest list find show me get all any near".split()
)
# Collapse number / salary-syntax variants to one <num> token so cosmetic facet-repeats dedup:
#   "100,000" == "$100k" == "100,000..200,000" == "$100,000 - $150,000".
GOV_NUM_RE = re.compile(
    r"[\$£€]?\d[\d,\.]*\s*[kKmM]?(?:\s*(?:\.\.|-|to)\s*[\$£€]?\d[\d,\.]*\s*[kKmM]?)?"
)


def gov_store():
    """Process-global shared store (sys.modules sentinel) — the SAME dict for every
    importer (tools, adapters, the eval harness) within one process."""
    m = sys.modules.get("_mh_governor_store")
    if m is None:
        m = types.ModuleType("_mh_governor_store")
        m.CHAT = {}    # chat_id -> {"norm":[frozenset], "raw":[str], "urls":set(), "searches":int}
        m.ORDER = []   # LRU order of chat_ids
        sys.modules["_mh_governor_store"] = m
    return m


def gov_normalize(q):
    """Query -> token SET for Jaccard near-dup detection (number/salary variants -> <num>,
    site:<domain> kept, stopwords dropped)."""
    q = (q or "").lower().replace('"', " ").replace("'", " ")
    q = GOV_NUM_RE.sub(" <num> ", q)
    toks = set()
    for raw in q.split():
        t = raw.strip(".,;()[]{}!?")  # trim edge punctuation; keeps site:foo.bar and <num> intact
        if t and t not in GOV_STOPWORDS:
            toks.add(t)
    return frozenset(toks)


def gov_jaccard(a, b):
    return (len(a & b) / len(a | b)) if (a and b) else 0.0


def gov_state(chat_id):
    """Get-or-create per-chat state on the shared store (LRU-bounded)."""
    store = gov_store()
    st = store.CHAT.get(chat_id)
    if st is None:
        st = {"norm": [], "raw": [], "urls": set(), "searches": 0}
        store.CHAT[chat_id] = st
        store.ORDER.append(chat_id)
        while len(store.ORDER) > GOV_MAX_CHATS:
            store.CHAT.pop(store.ORDER.pop(0), None)
    return st


def gov_near_dup(st, query, threshold):
    """If `query` is a near-duplicate of a prior search THIS CHAT (any tool), return the skip note;
    else None. Conservative: catches cosmetic repeats, leaves genuinely different facets."""
    nq = gov_normalize(query)
    best, best_raw = 0.0, None
    for pn, pr in zip(st["norm"], st["raw"]):
        j = gov_jaccard(nq, pn)
        if j > best:
            best, best_raw = j, pr
    if best >= threshold and best_raw is not None:
        hint = (" Open a page you already found with read_page to get a specific value, or search a "
                "genuinely different facet." if st["urls"] else
                " Refine to a genuinely different facet, or read a result with read_page.")
        return (f"[over-search guard] Skipped: nearly identical to your earlier search «{best_raw}» "
                f"(similarity {best:.0%}); re-running won't surface new results.{hint}")
    return None


def gov_record_search(st, query, urls):
    """Record a real web search (tavily OR deep_research query-mode) into the shared per-chat state."""
    st["norm"].append(gov_normalize(query))
    st["raw"].append(query)
    st["searches"] += 1
    for u in urls:
        if isinstance(u, str) and u.startswith("http"):
            st["urls"].add(u)


def gov_note_urls(st, urls):
    """Note URLs a READ surfaced (deep_research urls-mode) without counting a search."""
    for u in urls:
        if isinstance(u, str) and u.startswith("http"):
            st["urls"].add(u)


def gov_nudge(st, soft_k):
    """Read-nudge after K combined searches with URLs in hand; escalates to a firm stop at 2K."""
    n = st["searches"]
    if n >= max(1, soft_k) and st["urls"]:
        if n >= 2 * max(1, soft_k):
            return (f"\n[over-search guard] You've run {n} web searches this conversation. Stop "
                    f"searching — you have enough sources; synthesize an answer from what you've "
                    f"found, or open a specific listing with read_page. More broad searches won't help.")
        return (f"\n[over-search guard] You've run {n} searches this conversation and already have "
                f"specific page URLs. Open the most relevant result with read_page to verify exact "
                f"values rather than searching again.")
    return None
