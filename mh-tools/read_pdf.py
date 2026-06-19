"""
title: Read PDF
author: mh-tools
version: 1.0.0
required_open_webui_version: 0.4.0
"""
# Custom OWUI tool — reads an ATTACHED PDF's text + tables via pdfplumber (sage's general-extraction
# approach: pdf_utils.ParsedDocument), so the model gets clean, table-aware content instead of OWUI's
# default pypdf auto-extraction (leak-prone + mangles tables). PDF auto-injection is DISABLED for chat
# attachments (retrieval/utils.py skips application/pdf in get_sources_from_items; routers/files.py
# skips the chat-attachment extraction when there's no knowledge_id), so this tool is the SINGLE path
# for reading an attached PDF. (KB-ingested PDFs are unaffected — different code path.)
#
# Flow: __files__ -> find the attached PDF -> Files.get_file_by_id (ASYNC) -> owner/admin access-check
# -> Storage.get_file (storage-agnostic local path) -> pdfplumber in a threadpool (it's blocking) ->
# per-page layout text + tables-as-Markdown -> byte-capped digest. Encrypted/owner-password PDFs (common
# for bank statements) decrypt via PyMuPDF (`fitz`) — a dep, like sage; the ImportError branch is a safety
# net. (A PDF needing a USER password to open still can't be read without it.) Spec: mh-tools/read_pdf.md.
# UPGRADE-CHECK: Files.get_file_by_id / Storage.get_file are OWUI internals — re-verify on bumps.
# DEPS: pdfplumber + pymupdf are provisioned manually (`uv pip install` into the venv) + pinned in
# pyproject.toml/requirements.txt. Do NOT add a frontmatter `requirements:` line — OWUI's frontmatter
# auto-install runs `python -m pip install ...`, which FAILS in this uv-managed venv (no pip module).

import asyncio
import logging

from pydantic import BaseModel, Field

log = logging.getLogger("mh.read_pdf")


def _table_to_md(table):
    rows = [["" if c is None else str(c).strip().replace("\n", " ") for c in row] for row in table if row]
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    rows = [r + [""] * (w - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * w) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _extract(path, max_chars):
    """Blocking pdfplumber extraction → Markdown (layout text + tables). Runs in a threadpool."""
    import pdfplumber
    try:
        pdf = pdfplumber.open(path)
    except Exception:
        try:                                   # encrypted? decrypt via PyMuPDF (optional dep), like sage
            import io
            import fitz
            doc = fitz.open(path)
            dec = doc.tobytes(deflate=True, garbage=3)
            doc.close()
            pdf = pdfplumber.open(io.BytesIO(dec))
        except ImportError:
            return None, "This PDF looks encrypted/protected and PyMuPDF (fitz) isn't installed to decrypt it."
        except Exception as e:
            return None, f"could not open the PDF ({e})."
    try:
        n_pages = len(pdf.pages)
        parts, used, truncated = [], 0, False
        for i, page in enumerate(pdf.pages, 1):
            block = []
            text = page.extract_text(layout=True) or ""
            if text.strip():
                block.append(text)
            for t in (page.extract_tables() or []):
                md = _table_to_md(t)
                if md:
                    block.append(f"\n[table — page {i}]\n{md}")
            if not block:
                continue
            chunk = f"\n\n--- page {i} ---\n" + "\n".join(block)
            if used + len(chunk) > max_chars:
                parts.append(chunk[: max(0, max_chars - used)])
                truncated = True
                break
            parts.append(chunk)
            used += len(chunk)
        return {"pages": n_pages, "text": "".join(parts).strip(), "truncated": truncated}, None
    finally:
        pdf.close()


class Tools:
    class Valves(BaseModel):
        MAX_CHARS: int = Field(
            50000, description="Cap on the returned PDF content in characters (token-budget guard)."
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = True

    async def read_pdf(
        self,
        filename: str = "",
        __files__=None,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
    ) -> str:
        """
        Read the text and tables of a PDF the user attached to this message. Use this whenever the
        user attaches a PDF and asks you to read, summarize, analyse, or answer questions about it.
        It returns the document's text (layout-preserved) and any tables (as Markdown). Attached PDFs
        are NOT auto-loaded into the conversation — this tool is the only way to read them. If several
        PDFs are attached, pass `filename` to pick one.

        :param filename: optional — the attached PDF's filename, only needed if more than one PDF is attached.
        """

        async def emit(desc, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": desc, "done": done}})

        def _ct(it):
            f = it.get("file", {}) or {}
            return ((f.get("meta", {}) or {}).get("content_type") or f.get("content_type")
                    or it.get("content_type") or "")

        def _is_pdf(it):
            return it.get("type") == "file" and (
                _ct(it) == "application/pdf" or (it.get("name") or "").lower().endswith(".pdf"))

        pdfs = [it for it in (__files__ or []) if _is_pdf(it)]
        if not pdfs:
            return ("No PDF is attached to this message. Ask the user to attach the PDF "
                    "(the + menu → Upload Files), then call read_pdf again.")
        if filename:
            pdfs = [it for it in pdfs if (it.get("name") or "").lower() == filename.lower()] or pdfs
        if len(pdfs) > 1 and not filename:
            names = ", ".join((it.get("name") or "?") for it in pdfs)
            return f"Several PDFs are attached ({names}). Call read_pdf again with filename set to one of them."

        it = pdfs[0]
        name = it.get("name") or "document.pdf"
        if __request__ is None:
            return "read_pdf is unavailable in this context (no request handle)."

        await emit(f"Reading {name}…")
        try:
            from open_webui.models.files import Files
            from open_webui.storage.provider import Storage
            fo = await Files.get_file_by_id(it.get("id"))
            if fo is None:
                return f"Could not find the attached file {name}."
            u = __user__ or {}
            if not (u.get("role") == "admin" or fo.user_id == u.get("id")):
                ok = False
                try:
                    from open_webui.models.files import has_access_to_file
                    ok = await has_access_to_file(it.get("id"), "read", __user__)
                except Exception:
                    ok = False
                if not ok:
                    return f"You don't have access to {name}."
            local = await asyncio.to_thread(Storage.get_file, fo.path)
        except Exception as e:
            log.warning("read_pdf fetch error: %s", e)
            return f"Could not load the attached PDF ({name}): {e}"

        result, err = await asyncio.to_thread(_extract, local, max(1000, int(self.valves.MAX_CHARS)))
        if err:
            await emit("PDF could not be read.", done=True)
            return f"{name}: {err}"
        await emit(f"Read {name} ({result['pages']} page(s)).", done=True)
        body = result["text"] or ("(no extractable text — the PDF may be scanned/image-only; "
                                  "OCR is not available in this tool.)")
        header = f"# {name} — {result['pages']} page(s)" + (" (truncated)" if result["truncated"] else "")
        return header + "\n" + body
