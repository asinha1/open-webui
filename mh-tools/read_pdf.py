"""
title: Read PDF
author: mh-tools
version: 1.2.0
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
# per-page layout text + tables-as-Markdown -> byte-capped digest.
#
# ROBUSTNESS (v1.2): some PDFs render an overlaid double-stamped text layer that pdfplumber merges into
# doubled tokens ("MMaannaaggee"->"Manage", "aapppp"->"app", and — observed on Chase statements — whole
# SECTION HEADERS like "AACCCCOOUUNNTT AACCTTIIVVIITTYY"). The doubling is SCATTERED across promo boxes /
# headers, a few tokens per page, NOT concentrated on one page — so a per-page fraction gate (v1.1) missed
# it. v1.2 gates DOC-LEVEL then applies PER-TOKEN: if the whole doc carries >= DEDUP_MIN_TOKENS doubled
# tokens (an overlaid layer is present), char-collapse every doubled token in every page's layout text +
# table cells. Layout columns are preserved (we substitute within the layout string, not reconstruct);
# masked data ("XXXX") is guarded against collapse; a clean doc (few/no doubled tokens) is left untouched
# so a stray repeated-pair string (e.g. a hex value) isn't collapsed. The collapse helper is adapted from
# ~/repos/sage/src/sage/parsers/fingerprint.py (+ the masking guard, since ours feeds model-read DATA, not
# sage's detection text). Encrypted/owner-password PDFs (common for bank statements) decrypt via PyMuPDF
# (`fitz`) — a dep, like sage; the ImportError branch is a safety net. (A PDF needing a USER password to
# open still can't be read without it.) Spec: mh-tools/read_pdf.md.
# UPGRADE-CHECK: Files.get_file_by_id / Storage.get_file are OWUI internals — re-verify on bumps.
# DEPS: pdfplumber + pymupdf are provisioned manually (`uv pip install` into the venv) + pinned in
# pyproject.toml/requirements.txt. Do NOT add a frontmatter `requirements:` line — OWUI's frontmatter
# auto-install runs `python -m pip install ...`, which FAILS in this uv-managed venv (no pip module).

import asyncio
import logging
import re

from pydantic import BaseModel, Field

from open_webui.utils.telemetry.mh_tools import instrument  # [mh] tool-usage metrics

log = logging.getLogger("mh.read_pdf")

# --- Doubled-letter repair (overlaid double-stamp layer) ---------------------------------------------
# pdfplumber merges an overlaid double-stamped text layer into doubled tokens ("MMaannaaggee"->"Manage").
# We char-collapse such tokens. `_collapse_doubled_chars` is sage's primitive (verbatim); `_collapse_word`
# adds a masking guard for our DATA path; `_count_doubled` powers the doc-level gate.
_LETTER_RUN_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)   # letter runs >=4 — the only collapse candidates


def _collapse_doubled_chars(word):
    """Collapse a word to half length iff every adjacent char pair is identical. Conservative: only fires
    on even-length words >=4 where ALL pairs match, so real words ('coffee') are untouched. (sage verbatim)"""
    n = len(word)
    if n < 4 or n % 2 != 0:
        return word
    for i in range(0, n, 2):
        if word[i] != word[i + 1]:
            return word
    return word[::2]


def _collapse_word(word):
    """Masking-guarded collapse for the DATA path: skip when the collapsed result is a single repeated
    character (XXXX->XX, OOOO->OO) — that's masking/separator noise, never a doubled word. Real doubled
    words always yield >=2 distinct chars (MMaannaaggee->Manage, aapppp->app). Deliberate divergence from
    sage, whose collapse feeds only DETECTION text (where XXXX->XX is harmless); ours feeds text the model
    reads as DATA, so collapsing a masked account number 'XXXX'->'XX' must not happen."""
    c = _collapse_doubled_chars(word)
    return word if (c != word and len(set(c)) < 2) else c


def _count_doubled(text):
    """Count letter-run tokens in `text` that are the doubled pathology (masking-guarded). Drives the gate."""
    return sum(1 for r in _LETTER_RUN_RE.findall(text) if _collapse_word(r) != r)


def _clean_doubled(s):
    """Char-collapse every doubled letter-run token in `s`, in place — preserves all spacing/layout and
    non-letter chars (punctuation/symbols attached to a token are left; only the letter run collapses)."""
    return _LETTER_RUN_RE.sub(lambda mt: _collapse_word(mt.group()), s)


def _table_to_md(table, clean=False):
    def cell(c):
        s = "" if c is None else str(c).strip().replace("\n", " ")
        return _clean_doubled(s) if (clean and s) else s
    rows = [[cell(c) for c in row] for row in table if row]
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    rows = [r + [""] * (w - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * w) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _extract(path, max_chars, min_tokens):
    """Blocking pdfplumber extraction → Markdown (layout text + tables, doubled-letter-aware). Threadpool.

    Doubled-letter repair is DOC-LEVEL gated, then PER-TOKEN: pass 1 pulls each page's layout text + tables
    and counts doubled tokens across the whole doc; if that total >= min_tokens (an overlaid layer is
    present), pass 2 char-collapses every doubled token in every page (layout columns kept, masked data
    guarded). A clean doc is left untouched. min_tokens <= 0 disables the repair entirely."""
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
        # Pass 1: materialize per-page layout text + tables, tally doubled tokens doc-wide.
        pages, doubled_total = [], 0
        for page in pdf.pages:
            text = page.extract_text(layout=True) or ""
            tables = page.extract_tables() or []
            doubled_total += _count_doubled(text)
            pages.append((text, tables))
    finally:
        pdf.close()

    repair = min_tokens > 0 and doubled_total >= min_tokens
    # Pass 2: assemble the digest, collapsing doubled tokens everywhere iff the doc tripped the gate.
    parts, used, truncated = [], 0, False
    for i, (text, tables) in enumerate(pages, 1):
        if repair:
            text = _clean_doubled(text)
        block = []
        if text.strip():
            block.append(text)
        for t in tables:
            md = _table_to_md(t, clean=repair)
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
    if repair:
        log.info("[mh] read_pdf repaired %d doubled-letter token(s) across %d page(s)", doubled_total, n_pages)
    return {"pages": n_pages, "text": "".join(parts).strip(), "truncated": truncated,
            "doubled_tokens": doubled_total, "repaired": repair}, None


class Tools:
    class Valves(BaseModel):
        MAX_CHARS: int = Field(
            50000, description="Cap on the returned PDF content in characters (token-budget guard)."
        )
        DEDUP_MIN_TOKENS: int = Field(
            4,
            description="Doc-level gate for doubled-letter repair: the minimum number of doubled-pattern "
                        "tokens in the whole PDF before the overlaid double-stamp repair turns on (it then "
                        "collapses every doubled token, preserving layout + masked data). Higher = more "
                        "conservative; 0 disables the repair.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = True

    @instrument("read_pdf", "local")
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

        result, err = await asyncio.to_thread(
            _extract, local, max(1000, int(self.valves.MAX_CHARS)),
            max(0, int(self.valves.DEDUP_MIN_TOKENS)),
        )
        if err:
            await emit("PDF could not be read.", done=True)
            return f"{name}: {err}"
        await emit(f"Read {name} ({result['pages']} page(s)).", done=True)
        body = result["text"] or ("(no extractable text — the PDF may be scanned/image-only; "
                                  "OCR is not available in this tool.)")
        header = f"# {name} — {result['pages']} page(s)" + (" (truncated)" if result["truncated"] else "")
        return header + "\n" + body
