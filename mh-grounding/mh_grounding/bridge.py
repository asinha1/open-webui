"""mh_grounding.bridge — the OWUI ⇄ agent bridge (RFC-MH-005 §5 mech 2 / reach phase).

Carries context across the two faces of the stack: chats/plans made in OWUI (phone,
multi-user) become readable by the desktop agent, and agent results write back into an
OWUI **note** (the §7-#6 write-back target: notes are the freeform-doc home; a distilled
plan/spec beats a raw transcript, so the WRITER should distill before saving).

- READ side: `list_chats` / `get_chat` — read-only sqlite over webui.db (`chat` table,
  `chat` JSON → `history.messages`, the proven db-introspection pattern). Transcript is
  distilled by walking the `history.currentId` parent chain (the branch actually on
  screen), newest-last; falls back to timestamp order.
- WRITE side: `save_note` — POST the fork's notes API (`/api/v1/notes/create`) with the
  OPERATOR's OWUI key. That key makes this module IDENTITY-BEARING (plan §5b): the
  serving layer must gate these calls to the operator (loopback, or the matching
  Tailscale identity header) — enforcement lives in mh-mcp/server.py, not here.
"""

import json
import logging
import sqlite3
from pathlib import Path

import aiohttp

log = logging.getLogger("mh.bridge")

DEFAULT_WEBUI_DB = str(Path.home() / "service-data" / "open-webui" / "webui.db")


def list_chats(n=10, webui_db_path=DEFAULT_WEBUI_DB):
    """Most recently updated chats: [(id, title, updated_at_epoch)]. Read-only."""
    db = sqlite3.connect(f"file:{webui_db_path}?mode=ro", uri=True)
    try:
        return db.execute(
            "SELECT id, title, updated_at FROM chat WHERE archived = 0 "
            "ORDER BY updated_at DESC LIMIT ?", (max(1, min(int(n), 50)),)).fetchall()
    finally:
        db.close()


def get_chat(chat_id, max_chars=60000, webui_db_path=DEFAULT_WEBUI_DB):
    """One chat distilled to a markdown transcript (title + role-labelled turns).
    Returns (title, transcript) or (None, error_string)."""
    db = sqlite3.connect(f"file:{webui_db_path}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT title, chat FROM chat WHERE id = ?", (chat_id,)).fetchone()
    finally:
        db.close()
    if not row:
        return None, f"No chat with id {chat_id!r}. Use the chat list to find a valid id."
    title, blob = row[0], json.loads(row[1])
    history = blob.get("history") or {}
    msgs = history.get("messages") or {}
    if not msgs:
        return title, "(empty chat)"

    # Walk the on-screen branch: currentId -> parents; else timestamp order.
    chain = []
    cur = history.get("currentId")
    seen = set()
    while cur and cur in msgs and cur not in seen:
        seen.add(cur)
        chain.append(msgs[cur])
        cur = msgs[cur].get("parentId")
    if chain:
        chain.reverse()
    else:
        chain = sorted(msgs.values(), key=lambda m: m.get("timestamp") or 0)

    parts = []
    for m in chain:
        role = (m.get("role") or "?").upper()
        content = (m.get("content") or "").strip()
        if content:
            parts.append(f"### {role}\n{content}")
    transcript = "\n\n".join(parts)
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars].rstrip() + "\n\n[transcript truncated]"
    return title, transcript


async def save_note(title, markdown, api_base, api_key, timeout=30):
    """Create an OWUI note (data.content.md). Returns (note_id, None) or (None, error)."""
    payload = {"title": title, "data": {"content": {"md": markdown}},
               "meta": {"source": "mh-mcp bridge"}}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Authorization": f"Bearer {api_key}"},
        ) as session:
            async with session.post(f"{api_base}/api/v1/notes/create", json=payload) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    return None, f"OWUI note create failed (HTTP {resp.status}): {body}"
                data = await resp.json()
    except aiohttp.ClientError as e:
        log.warning("bridge save_note network error: %s", e)
        return None, "OWUI note create failed (network error)."
    return (data or {}).get("id"), None
