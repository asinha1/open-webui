"""
title: Export Document
author: mh-tools
version: 1.0.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — version-controlled in the open-webui fork under mh-tools/.
# Hands the user a DOWNLOADABLE file (Markdown or PDF) built from content the model produced.
# Closes the "generate a PDF / save this as a file" gap: before this, the model correctly
# DECLINED (it has no file-gen capability — a v8 capability-honesty win), but the capability
# was genuinely missing. See post-soak-queue "File / document export capability".
#
# How delivery works (OWUI-specific glue — that's why this is an mh-tool, not an MCP server):
#   1. render content -> bytes (md = utf-8 as-is; pdf = markdown -> HTML -> fpdf2.write_html)
#   2. Storage.upload_file(...) writes the bytes under DATA_DIR/uploads (uuid-prefixed name)
#   3. Files.insert_new_file(...) registers a file row OWNED BY THE CALLING USER
#   4. return a markdown link to GET /api/v1/files/<id>/content?attachment=true
# The content endpoint requires the requesting user to own the file (file.user_id == user.id),
# so the row MUST be created with __user__["id"]; the logged-in browser's session authorizes
# the download. Deployed via Workspace -> Tools (DB-stored), then RESTART OWUI (tool binding is
# not reliably re-read live). Design + acceptance: mh-tools/export_document.md.
#
# Dependencies: `markdown` and `fpdf2` (import name `fpdf`) — BOTH already in the OWUI venv, so
# this adds NO new fork dependency. Markdown export needs neither (plain utf-8 write). If a PDF
# dep is ever absent the tool degrades to a readable message and still offers Markdown.

import asyncio
import hashlib
import io
import logging
import os
import re
import uuid
from typing import Optional

from pydantic import BaseModel, Field

log = logging.getLogger("mh.export_document")

# OWUI internals — available because this tool runs inside the OWUI process. Guarded so the
# module still loads (with a clear runtime error) if a future OWUI version moves these.
try:
    from open_webui.models.files import Files, FileForm
    from open_webui.storage.provider import Storage
    _OWUI_OK = True
except (Exception, SystemExit) as _e:  # SystemExit too: OWUI config can hard-exit on import
    Files = FileForm = Storage = None      # (e.g. WEBUI_SECRET_KEY unset outside the server)
    _OWUI_OK = False
    logging.getLogger("mh.export_document").warning("OWUI file internals unavailable: %s", _e)

_FORMATS = {
    "md": (".md", "text/markdown"),
    "markdown": (".md", "text/markdown"),
    "pdf": (".pdf", "application/pdf"),
}

# fpdf2 core fonts encode as latin-1; map the typography the model actually emits so a PDF
# doesn't drop an em-dash or a smart quote to '?'. (Markdown export is full UTF-8 — this is
# only for the PDF render path.) Anything still unencodable degrades to '?' via the final pass.
_TYPO = {
    "—": "-", "–": "-", "‘": "'", "’": "'", "“": '"',
    "”": '"', "…": "...", "•": "- ", " ": " ", " ": " ",
    " ": " ", "→": "->", "←": "<-", "™": "(TM)", "®": "(R)",
    "€": "EUR ", "✅": "[x] ", "☑": "[x] ", "●": "- ",
}


class Tools:
    class Valves(BaseModel):
        MAX_CONTENT_CHARS: int = Field(
            400000, description="Refuse to export documents larger than this many characters "
                                "(guards against a runaway render). ~150 pages of text."
        )
        MAX_FILENAME_CHARS: int = Field(
            80, description="Cap on the user-facing base filename (before extension)."
        )

    def __init__(self):
        self.valves = self.Valves()

    async def export_document(
        self,
        content: str,
        filename: str = "document",
        format: str = "md",
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Create a DOWNLOADABLE file from content you have written and give the user a link to it.
        Use this when the user asks you to "generate a PDF", "save this as a file / markdown /
        document", "make a downloadable …", or otherwise wants to take content away as a file —
        NOT for showing content in the chat (just write that normally). Pass the finished document
        body as Markdown; it is saved verbatim for `md`, or rendered to a styled `pdf`. Returns a
        markdown download link to present to the user.

        :param content: the full document body, written in Markdown (headings, lists, tables, links, bold/italic all supported).
        :param filename: a short descriptive base name for the file, without extension (e.g. "nyc_green_jobs"). Sanitized automatically.
        :param format: "md" for a Markdown file (default, exact text) or "pdf" for a rendered PDF.
        """

        async def emit_status(desc, done=False):
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": desc, "done": done}}
                )

        if not _OWUI_OK:
            return ("I can't export files right now — the document-export tool couldn't reach "
                    "Open WebUI's file storage. Tell the operator the export_document tool needs "
                    "attention; meanwhile I can paste the content directly into the chat.")

        content = content or ""
        if not content.strip():
            return "There's no content to export — give me the document text first."
        if len(content) > self.valves.MAX_CONTENT_CHARS:
            return (f"That document is too large to export ({len(content)} chars; limit "
                    f"{self.valves.MAX_CONTENT_CHARS}). Trim it or split it into parts.")

        fmt = (format or "md").strip().lower()
        if fmt not in _FORMATS:
            return f"I can export 'md' or 'pdf', not '{format}'. Pick one of those."
        ext, mime = _FORMATS[fmt]

        user_id = (__user__ or {}).get("id")
        if not user_id:
            return ("I couldn't identify your account to attach the file to, so the download "
                    "link wouldn't work. Try again, or I can paste the content into the chat.")

        await emit_status(f"Building {fmt.upper()} …")

        # ---- render to bytes -------------------------------------------------
        try:
            data = self._render_bytes(content, fmt)
        except _PdfUnavailable:
            return ("PDF export isn't available on this server (the PDF renderer is missing). "
                    "I can give you a Markdown file instead — say the word and I'll export `.md`.")
        except Exception as e:  # render failure -> readable, not a stack trace
            log.warning("export_document render failed fmt=%s: %s", fmt, e)
            return (f"I couldn't render that as a {fmt.upper()} ({e}). A Markdown export usually "
                    "works — want me to try `.md` instead?")

        # ---- persist + register ---------------------------------------------
        base = self._safe_base(filename, self.valves.MAX_FILENAME_CHARS)
        display_name = f"{base}{ext}"
        file_id = str(uuid.uuid4())
        stored_name = f"{file_id}_{display_name}"
        sha = hashlib.sha256(data).hexdigest()

        try:
            _contents, path = await asyncio.to_thread(
                Storage.upload_file, io.BytesIO(data), stored_name, {}
            )
            rec = await Files.insert_new_file(
                user_id,
                FileForm(
                    id=file_id,
                    filename=display_name,
                    path=path,
                    hash=sha,
                    meta={"name": display_name, "content_type": mime, "size": len(data),
                          "source": "export_document"},
                ),
            )
        except Exception as e:
            log.exception("export_document persist failed: %s", e)
            return ("I rendered the document but couldn't save it to the server's file store, so "
                    "there's no download link. I can paste the content into the chat instead.")

        if rec is None:
            return ("I rendered the document but the file record didn't save, so the download "
                    "link wouldn't work. I can paste the content into the chat instead.")

        url = f"/api/v1/files/{file_id}/content?attachment=true"
        kb = max(1, round(len(data) / 1024))
        await emit_status(f"Exported {display_name} ({kb} KB).", done=True)
        log.info("export_document id=%s name=%s fmt=%s bytes=%d user=%s",
                 file_id, display_name, fmt, len(data), user_id)

        return (f"Created **[{display_name}]({url})** ({kb} KB) — give the user this download "
                f"link. (Format: {fmt.upper()}.)")

    # ---- rendering -----------------------------------------------------------

    @staticmethod
    def _render_bytes(content: str, fmt: str) -> bytes:
        """content (Markdown) -> file bytes. md = utf-8 verbatim; pdf = md->HTML->fpdf2."""
        if fmt in ("md", "markdown"):
            return content.encode("utf-8")
        # pdf
        try:
            import markdown as _md
            from fpdf import FPDF
        except ImportError:
            raise _PdfUnavailable()
        html = _md.markdown(
            content, extensions=["tables", "fenced_code", "sane_lists"], output_format="html"
        )
        html = Tools._latin1_safe(html)
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.set_title("Document")
        pdf.add_page()
        pdf.set_font("helvetica", size=11)
        pdf.write_html(html)
        out = pdf.output()  # fpdf2 2.x returns a bytearray
        return bytes(out)

    @staticmethod
    def _latin1_safe(s: str) -> str:
        for k, v in _TYPO.items():
            s = s.replace(k, v)
        return s.encode("latin-1", "replace").decode("latin-1")

    @staticmethod
    def _safe_base(name: str, cap: int) -> str:
        """Sanitize a user-supplied filename to a safe base (no path, no extension, ascii-ish)."""
        base = os.path.basename((name or "").strip())
        base = re.sub(r"\.(md|markdown|pdf|txt|doc|docx)$", "", base, flags=re.I)
        base = re.sub(r"[^A-Za-z0-9 ._-]", "", base).strip().strip(".")
        base = re.sub(r"\s+", "_", base) or "document"
        return base[:cap]


class _PdfUnavailable(Exception):
    """Raised when the PDF render dependencies aren't importable (graceful md fallback)."""
