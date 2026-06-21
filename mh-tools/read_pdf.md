# read_pdf

Reads an **attached PDF**'s text + tables via **pdfplumber** (sage's general-extraction approach — see
`~/repos/sage/src/sage/parsers/pdf_utils.py`), and returns a Markdown digest. This is the **single path**
for reading an attached PDF: OWUI's default `pypdf` auto-extraction is **disabled** for chat-attached PDFs
(see "Disable the default" below), so the model gets clean, table-aware content instead of mangled/leaky
pypdf text.

## Why
`pypdf` (OWUI's default `CONTENT_EXTRACTION_ENGINE`) has known memory leaks and handles tables/layout poorly.
`pdfplumber` is pure-Python, leak-free, and extracts **tables** + layout-preserving text — the same library
sage's statement parsers are built on. Owned tool > a Tika/Docling service (the tabled alternative).

## Signature
`read_pdf(filename: str = "") -> str`  — injected: `__files__`, `__request__`, `__user__`, `__event_emitter__`.

- Finds the attached PDF in `__files__` (item `type=='file'` + `content_type=='application/pdf'` or `.pdf`).
- `Files.get_file_by_id(id)` (**async**) → owner/admin access-check → `Storage.get_file(path)` → local path.
- `pdfplumber` runs in a threadpool (it's blocking): per page, `extract_text(layout=True)` + `extract_tables()`
  → Markdown tables. Byte-capped by the `MAX_CHARS` valve (default 50 000).
- Encrypted/owner-password PDFs: decrypt via **PyMuPDF** (`fitz`, a dep). A user-password PDF still needs the password.
- `filename` disambiguates when several PDFs are attached.

## Doubled-letter robustness (v1.2)
Some PDFs render an overlaid double-stamped text layer that pdfplumber merges into doubled tokens
(`"MMaannaaggee"` → `"Manage"`, and — observed on real Chase statements — whole **section headers** like
`"AACCCCOOUUNNTT AACCTTIIVVIITTYY"` → `"ACCOUNT ACTIVITY"`). When the headers are garbled the model can't
navigate the document (the live failure that drove this: it reported "no Account Services section" because
the titles were doubled).

**Why doc-level, not per-page (the v1.1 → v1.2 lesson):** the doubling is **scattered** — a few tokens per
page in promo boxes / headers, *not* concentrated on one page. v1.1's per-page fraction gate (option C) only
repaired the single densest page and left the rest garbled. v1.2 gates **doc-level**, applies **per-token**:

1. Pass 1 — pull each page's `layout=True` text + tables and count doubled tokens across the **whole doc**
   (`_count_doubled`, masking-guarded).
2. If the doc total ≥ `DEDUP_MIN_TOKENS` (an overlaid layer is present), pass 2 char-collapses **every**
   doubled token in **every** page's layout text + table cells (`_clean_doubled` — a regex sub that preserves
   all spacing/columns and attached punctuation; only the letter run collapses). A clean doc (few/no doubled
   tokens) is left untouched, so a stray repeated-pair string (e.g. a hex value) isn't collapsed.

**Masking guard** (`_collapse_word`): skip collapse when the result is a single repeated char (`XXXX` → `XX`)
— masking, not a doubled word. This is the one divergence from sage's verbatim `_collapse_doubled_chars`,
which only ever feeds *detection* text (where `XXXX`→`XX` is harmless); ours feeds text the model reads as
**data**. Helpers adapted from `~/repos/sage/src/sage/parsers/fingerprint.py` — copied, not imported (mh-tools
can't import each other, and installing the sage package would pull `psycopg`/typer into the lean uv venv).
A `[mh]` INFO log records `repaired N doubled-letter token(s) across M page(s)`. Validated on a real 4-page
Chase statement: 39 doubled tokens repaired, **0 residual**, all section headers clean; in-UI confirmed.

## Valves
- `MAX_CHARS` (50000) — cap on the returned content (token-budget guard).
- `DEDUP_MIN_TOKENS` (4) — doc-level gate: minimum doubled-pattern tokens in the whole PDF before the repair
  turns on (then it collapses every doubled token). Higher = more conservative; `0` disables the repair.

## Deps
- **`pdfplumber`** + **`pymupdf`** — both installed in the venv + pinned in `pyproject.toml`/`backend/requirements.txt`. `pdfplumber` does text+tables; `pymupdf`/`fitz` decrypts encrypted/owner-password PDFs (e.g. bank statements). A PDF needing a *user* password to open still can't be read without it.

## Disable the default (so this is the only path)
1. **No auto-injection:** `retrieval/utils.py` `get_sources_from_items` — the `[mh]` image-skip in the
   `type=='file'` branch is generalized to also skip `application/pdf` (no `<source>`, no citation, no
   chunked-retrieval). PDF stays in `metadata['files']` so `read_pdf` (via `__files__`) still finds it.
2. **No upload extraction (kills the pypdf leak):** `routers/files.py` `process_uploaded_file` — the
   chat-attachment documents branch skips PDF extraction **when `knowledge_id` is absent**. KB-ingested PDFs
   (which carry a `knowledge_id`) still extract + embed normally — that path is untouched.

## Deploy
Load via Workspace → Tools (generates `specs`), add `read_pdf` to the chat + Forge models' `meta.toolIds`,
restart. The `<attached_files>` tag still lists the PDF so the model knows to call `read_pdf`.

## Limits / v2
- No OCR (scanned/image-only PDFs return "no extractable text"). Add an OCR path later if needed.
- This is the **general** extractor only. **v2** = a `parse_statement` tool reusing sage's per-institution
  parsers (Chase/Amex/Schwab/Fidelity/…) → structured transactions — deferred (coupling decision: install
  sage into the venv vs subprocess its CLI).
