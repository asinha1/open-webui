"""
title: Save Sources
author: mh-tools
version: 1.0.0
required_open_webui_version: 0.4.0

save_research.py — the self-generating-RAG save MODULE (RFC-MH-002).

The shared save LOGIC behind the deterministic "📌 Save sources" Action button (NOT a model-routed
tool — RFC-MH-002 §4/§9). This file's pure helpers are stdlib-only + import-safe, so it loads
standalone for offline testing (eval/save_research_test.py); the OWUI-integration save flow
lazy-imports open_webui inside its functions (the knowledge_search precedent) so the top level never
needs a live app.

Locked decisions (RFC-MH-002 §9, 2026-06-15):
  - Trigger = OWUI Action button (deterministic); the dialog always asks (no blind autosave).
  - Topic = structured taxonomy `research:<domain>/<subtopic>`; domains from research-topics.json
    (seed finance/health/cooking; new domains = operator edit). Model-proposal (if ever added) just
    pre-fills the dialog.
  - "This" = pages READ this chat (derived from the chat history's read_page sources), user-selected.
  - Re-fetch the FULL page at save time (uncapped, behind BYTE_CEILING), decoupled from read_page's
    60K model-return cap.
  - Read side: knowledge_search queries all research:* collections (separate file).

Verified fork building blocks (for the save flow below):
  - Action contract `utils/actions.py`: action(body, __request__, __user__, __event_call__,
    __event_emitter__, __model__) — full live app.state, unlike the harness tool-loop.
  - Create collection: Knowledges.insert_new_knowledge(user_id, KnowledgeForm{name, description,
    access_grants}) -> KnowledgeModel; its .id IS the vector collection_name.
  - Write: routers.retrieval.save_docs_to_vector_db(request, [Document], collection_name,
    metadata=..., add=True) — markdown-aware chunk + same MiniLM embedder + hash-dedup.
  - Read pages from the chat: Chats.get_chat_by_id(body['chat_id']) -> history.messages; read_page
    results carry {"source": {"name": title, "url": url}} in each assistant message's `sources`.
  - Re-fetch: reuse read_page's fetch+render (its MAX_CONTENT_CHARS cap is valve-bound, so call the
    internal fetch/render path uncapped for the save sink).
"""
import hashlib
import json
import os
import re

REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "research-topics.json")
SEED_REGISTRY = {"finance": [], "health": [], "cooking": []}
BYTE_CEILING = 4 * 1024 * 1024   # ~4MB: covers ~every real page; guards a pathological page from OOM

# Volatile-content signals -> short-TTL class (freshness hooks, RFC-MH-002 §6). Default = reference.
_VOLATILE_RE = re.compile(
    r"\b(rate|rates|price|prices|pricing|news|today|stock|stocks|deal|deals|job|jobs|salary|"
    r"menu|weather|forecast|score|scores|live|breaking|sale)\b|/20\d\d[/-]", re.I)


# ----------------------------------------------------------------------------- pure helpers (tested)

def _slug(s):
    """A path-safe slug segment: lower, non-alnum -> '-', collapsed, trimmed."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(s).lower())).strip("-")


def _registry():
    """{domain: [subtopic,...]} from research-topics.json; seed fallback if absent/unreadable."""
    try:
        with open(REGISTRY_PATH) as f:
            r = json.load(f)
        return r if isinstance(r, dict) else dict(SEED_REGISTRY)
    except (OSError, ValueError):
        return dict(SEED_REGISTRY)


def _resolve_topic(domain, subtopic):
    """'research:<domain>/<subtopic>' iff domain is an allowed (registry) domain, else None.

    The breadth cap: an unknown domain is rejected here (the dialog re-prompts); new domains require
    an operator edit to research-topics.json (RFC-MH-002 §5)."""
    d = _slug(domain)
    if d not in _registry():
        return None
    return f"research:{d}/{_slug(subtopic)}"


def _classify(url):
    """Coarse freshness class for the v2 TTL hook (RFC-MH-002 §6): 'volatile' | 'reference'."""
    return "volatile" if _VOLATILE_RE.search(url or "") else "reference"


def _meta(url, title, query, text, saved_at):
    """Flat, Chroma-legal provenance dict merged into every chunk (RFC-MH-002 §3a).

    `hash` = sha256(url) for URL-dedup; `content_hash` = sha256(text) for silent-edit detection."""
    return {
        "source": url,
        "title": title or url,
        "query": query or "",
        "saved_at": saved_at,
        "hash": hashlib.sha256((url or "").encode()).hexdigest(),
        "content_hash": hashlib.sha256((text or "").encode()).hexdigest(),
        "content_class": _classify(url),
    }


def _dedup_decision(prev_meta, new_meta):
    """ADD (new URL) | SKIP (same URL, content unchanged) | REINDEX (same URL, silently edited)."""
    if not prev_meta:
        return "ADD"
    if prev_meta.get("content_hash") == new_meta.get("content_hash"):
        return "SKIP"
    return "REINDEX"


def _cap_bytes(text, ceiling):
    """Cap to <= ceiling BYTES (utf-8); return (text, truncated?). The save-sink OOM guard."""
    b = (text or "").encode("utf-8")
    if len(b) <= ceiling:
        return text, False
    return b[:ceiling].decode("utf-8", "ignore"), True


# --------------------------------------------------------------- OWUI Action: "📌 Save sources"
# Deterministic trigger (RFC-MH-002 §4; decision A, 2026-06-15: a two-step native `input` dialog —
# OWUI's __event_call__ has only input/confirmation/execute, no rich form). Runs ONLY in OWUI;
# lazy-imports open_webui inside action() so the pure helpers above stay import-safe + offline-tested.
from pydantic import BaseModel, Field
import base64 as _b64
import datetime as _dt

# Button icon: Heroicons `archive-box-arrow-down` (outline) as an SVG data-URI — OWUI renders
# module.icon as an <img src> (emoji => broken image), special-cases data:image/svg for dark-mode
# invert, and themes via currentColor. (models.py reads getattr(module,'icon') at model-build time.)
_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" '
    'stroke-width="1.5" stroke="currentColor">'
    '<path stroke-linecap="round" stroke-linejoin="round" '
    'd="m20.25 7.5-.625 10.632a2.25 2.25 0 0 1-2.247 2.118H6.622a2.25 2.25 0 0 1-2.247-2.118L3.75 7.5'
    'M12 10.5v6m0 0-3-3m3 3 3-3'
    'M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375'
    'c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125Z" /></svg>'
)
_SAVE_ICON = "data:image/svg+xml;base64," + _b64.b64encode(_ICON_SVG.encode()).decode()


class Action:
    class Valves(BaseModel):
        byte_ceiling: int = Field(
            default=BYTE_CEILING,
            description="Max bytes saved per page (full re-fetch OOM guard, RFC-MH-002 §3a).")
        new_topic_description: str = Field(
            default="Saved research pages (self-generating RAG, RFC-MH-002).",
            description="Description stamped on a newly-created research:<domain>/<subtopic> collection.")

    def __init__(self):
        self.valves = self.Valves()
        self.icon = _SAVE_ICON

    async def action(self, body, __request__=None, __user__=None,
                     __event_call__=None, __event_emitter__=None):
        from starlette.concurrency import run_in_threadpool
        from langchain_core.documents import Document
        from open_webui.models.chats import Chats
        from open_webui.models.knowledge import Knowledges, KnowledgeForm
        from open_webui.routers.retrieval import get_content_from_url, save_docs_to_vector_db
        from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT

        async def status(msg, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": msg, "done": done}})

        uid = (__user__ or {}).get("id")
        if not uid:
            await status("Can't save — no user context.", done=True); return

        # 1) which pages were READ in this chat (http sources from read_page citations)
        pages = await self._read_pages(Chats, body)
        if not pages:
            await status("No web pages were read in this chat — nothing to save.", done=True); return

        # 2) dialog 1 — topic (registry-validated; one retry)
        topic = await self._ask_topic(__event_call__)
        if not topic:
            await status("Save cancelled.", done=True); return

        # 3) dialog 2 — which pages (`all` default, or e.g. `1,3`)
        chosen = await self._ask_pages(__event_call__, pages)
        if not chosen:
            await status("Save cancelled — no pages selected.", done=True); return

        # 4) re-fetch FULL page -> provenance + dedup -> save into research:<domain>/<subtopic>
        await status(f"Saving {len(chosen)} page(s) to {topic}…")
        cid = await self._collection_id(Knowledges, KnowledgeForm, uid, topic)
        saved = skipped = failed = 0
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        for p in chosen:
            try:
                content, _docs = await run_in_threadpool(get_content_from_url, __request__, p["url"])
                content, _trunc = _cap_bytes(content or "", self.valves.byte_ceiling)
                if not content.strip():
                    failed += 1; continue
                meta = _meta(p["url"], p["title"], p.get("query", ""), content, now)
                ex = VECTOR_DB_CLIENT.query(collection_name=cid, filter={"hash": meta["hash"]})
                if ex and getattr(ex, "ids", None) and ex.ids and ex.ids[0]:
                    stored = (ex.metadatas[0][0] if ex.metadatas and ex.metadatas[0] else {}) or {}
                    if _dedup_decision({"content_hash": stored.get("content_hash")}, meta) == "SKIP":
                        skipped += 1; continue
                    VECTOR_DB_CLIENT.delete(collection_name=cid, filter={"hash": meta["hash"]})  # REINDEX
                doc = Document(page_content=content, metadata=meta)
                await run_in_threadpool(
                    save_docs_to_vector_db, __request__, [doc], cid, None, False, True, True, None)
                saved += 1
            except Exception:
                failed += 1
        msg = f"✅ Saved {saved} page(s) to **{topic}**."
        if skipped:
            msg += f" {skipped} already saved (unchanged)."
        if failed:
            msg += f" {failed} couldn't be fetched."
        await status(msg, done=True)
        return None

    # ---- helpers (OWUI-context) ----
    async def _read_pages(self, Chats, body):
        """Pages read this chat = http `source.url`s from read_page citations, deduped, with the
        triggering user query (the assistant message's parent). KB chunks (non-http) are excluded."""
        chat_id = (body or {}).get("chat_id")
        if not chat_id:
            return []
        chat = await Chats.get_chat_by_id(chat_id)
        msgs = (((getattr(chat, "chat", None) or {}).get("history") or {}).get("messages")) or {}
        seen, pages = set(), []
        for m in msgs.values():
            if m.get("role") != "assistant":
                continue
            query = (msgs.get(m.get("parentId"), {}) or {}).get("content", "") or ""
            for s in (m.get("sources") or []):
                url = (s.get("source") or {}).get("url") or ""
                if not url.startswith("http") or url in seen:
                    continue
                seen.add(url)
                pages.append({"url": url, "title": (s.get("source") or {}).get("name") or url,
                              "query": query[:200]})
        return pages

    async def _ask_topic(self, event_call):
        if not event_call:
            return None
        reg = _registry()
        lines = "\n".join(f"• {d}: {', '.join(reg[d]) or '(none yet)'}" for d in reg)
        msg = "Save to which topic? Type `domain/subtopic`.\nAllowed domains + existing subtopics:\n" + lines
        for _ in range(2):
            raw = await event_call({"type": "input", "data": {
                "title": "📌 Save sources — topic", "message": msg, "placeholder": "finance/savings-rates"}})
            if not raw:
                return None
            parts = str(raw).split("/", 1)
            if len(parts) == 2:
                topic = _resolve_topic(parts[0], parts[1])
                if topic:
                    return topic
            msg = (f"`{raw}` isn't valid — use `domain/subtopic` with an allowed domain "
                   f"({', '.join(reg)}). New domains need an operator edit to research-topics.json.")
        return None

    async def _ask_pages(self, event_call, pages):
        if not event_call:
            return None
        listing = "\n".join(f"{i}. {p['title']} — {p['url']}" for i, p in enumerate(pages, 1))
        raw = await event_call({"type": "input", "data": {
            "title": "📌 Save sources — which pages",
            "message": f"{len(pages)} page(s) read this chat:\n{listing}\n\nType `all` or e.g. `1,3`.",
            "placeholder": "all", "value": "all"}})
        if raw is None:
            return None
        raw = str(raw).strip().lower()
        if raw in ("", "all"):
            return pages
        idx = {int(x) for x in re.findall(r"\d+", raw)}
        return [p for i, p in enumerate(pages, 1) if i in idx]

    async def _collection_id(self, Knowledges, KnowledgeForm, uid, topic):
        """The user's `research:<domain>/<subtopic>` Knowledge id (the vector collection_name); create
        it (Workspace-visible) if absent — RFC-MH-002 §5 (lazy creation, curation gate #2)."""
        for kb in (await Knowledges.get_knowledge_bases_by_user_id(uid)) or []:
            if getattr(kb, "name", None) == topic:
                return kb.id
        kb = await Knowledges.insert_new_knowledge(
            uid, KnowledgeForm(name=topic, description=self.valves.new_topic_description))
        return kb.id
