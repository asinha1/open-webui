# export_document — hand the user a downloadable Markdown or PDF file

**Status: v1.0 BUILT 2026-06-06 — PENDING DEPLOY.** Paste into Workspace→Tools, enable on the Gemma
model (alongside `tavily_search` + `read_page`), then RESTART OWUI (tool binding isn't re-read live).
**No new dependency** — `markdown` (3.10) and `fpdf2` (`fpdf` 2.8) are already in the OWUI venv.

## Intent

Give the model a way to **produce a real downloadable file** — Markdown or PDF — from content it has
written, and return a link the user can click to download it. This is the capability behind requests
like *"generate a PDF,"* *"save this as a markdown file,"* *"make me a downloadable shortlist."*

## Why (the trigger — 2026-06-06 post-upgrade readout, NYC jobs chat)

Asked to *"generate a PDF with working links,"* then *"make it a markdown file,"* the model **correctly
declined** — it enumerated its tools, noted none could generate a file, cited the system prompt, and
offered a copy-into-Docs workaround. That was a **v8 capability-honesty win** (it didn't fake a file),
but the capability was genuinely missing. `export_document` closes the gap without weakening that
honesty: the model now has a real tool instead of having to pretend or refuse. See
`~/service-data/open-webui/soak-notes.md` post-upgrade readout "② NYC Green Jobs … capability-honesty"
and `post-soak-queue.md` "File / document export capability".

**Why a focused mh-tool, not OWUI's Code Interpreter:** the Code-Interpreter route (model writes
arbitrary Python) is a much larger trust surface and carries the same model-autonomy concern flagged
for Memory. This tool is narrow (render markdown → file, register it, return a link), MCP-promotable,
and adds no code-exec surface — the operator's chosen route.

## Model-facing surface (thin — 3 args, per the ≤4–5 rule)

```
export_document(content: str, filename: str = "document", format: str = "md") -> str
```

- **`content`** (required) — the full document body, written in **Markdown** (headings, lists, tables,
  links, bold/italic).
- **`filename`** (optional) — a short descriptive base name, no extension (e.g. `nyc_green_jobs`).
  Sanitized automatically (path stripped, non-`[A-Za-z0-9 ._-]` dropped, spaces→`_`, capped, any
  known extension removed). Empty/garbage → `document`.
- **`format`** (optional) — `md` (default; exact text, full UTF-8) or `pdf` (rendered).

Docstring steers routing: use it when the user wants to **take content away as a file** ("generate a
PDF", "save as markdown", "downloadable …") — NOT for showing content in the chat (just write that
normally).

## Behavior

1. **Render to bytes:**
   - `md` → the content encoded UTF-8 **verbatim** (no transformation; full Unicode).
   - `pdf` → `markdown` renders the body to HTML (`tables` + `fenced_code` + `sane_lists` extensions),
     then **fpdf2 `write_html`** lays it out (headings, lists, tables, bold/italic, links). fpdf2 core
     fonts are latin-1, so typography the model emits (em-dash, smart quotes, …, €, ✅) is mapped to
     safe equivalents first; anything still unencodable degrades to `?` rather than erroring.
2. **Persist:** `Storage.upload_file(BytesIO(bytes), "<uuid>_<name>", {})` writes the bytes under
   `DATA_DIR/uploads` (the uuid prefix keeps on-disk names unique; the user-facing name lives in
   `meta.name`).
3. **Register:** `Files.insert_new_file(__user__["id"], FileForm(...))` creates a file row **owned by
   the calling user**, with `meta.content_type` (`text/markdown` / `application/pdf`) and `meta.name`,
   plus a sha256 `hash`.
4. **Return** a markdown download link: `[<name>](/api/v1/files/<id>/content?attachment=true)`. The
   model presents it to the user; the logged-in browser's session authorizes the download.
5. **Emit** a status event ("Building PDF…" → "Exported <name> (N KB).") and **log** one INFO line.

## Safety / trust posture

- **Ownership-scoped download.** `GET /api/v1/files/{id}/content` requires the requesting user to own
  the file (`file.user_id == user.id`, or admin/shared). The row is created with `__user__["id"]`, so
  the link only works for that user's session — no anonymous/world-readable export.
- **No code execution.** Unlike the Code-Interpreter alternative, the tool only renders markdown via
  two fixed libraries — no arbitrary code path the model can steer.
- **Filename sanitization** prevents path traversal / odd on-disk names (the stored name is
  `<uuid>_<sanitized>` regardless).
- **Size guard:** `MAX_CONTENT_CHARS` (default 400 000 ≈ 150 pages) refuses runaway renders.
- This tool **writes** to the data volume (unlike read-only `read_page`/`tavily_search`). Files land in
  the normal OWUI uploads store and are managed/cleaned like any other upload.

## Valves (DB, never committed)

| Valve | Default | Purpose |
|---|---|---|
| `MAX_CONTENT_CHARS` | 400000 | refuse to export a document larger than this (runaway-render guard). |
| `MAX_FILENAME_CHARS` | 80 | cap on the user-facing base filename (before extension). |

## Errors degrade gracefully (model-readable strings, never a stack trace)

- empty content → "there's no content to export — give me the document text first."
- bad format → "I can export 'md' or 'pdf', not '<x>'."
- no `__user__` id → "couldn't identify your account … I can paste the content into the chat."
- PDF deps missing → "PDF export isn't available … I can give you a Markdown file instead." (md still
  works — it needs no third-party lib.)
- render / storage / DB failure → readable "couldn't render/save … paste into the chat instead."
- OWUI internals unreachable at load → the one tool call returns a readable "export tool needs
  attention" message (module still loads; `_OWUI_OK=False`).

## Acceptance criteria

1. **Markdown export:** `export_document(content, "notes", "md")` returns a working
   `[notes.md](/api/v1/files/<id>/content?attachment=true)` link; clicking it downloads the exact text
   (UTF-8, em-dash/smart-quotes intact).
2. **PDF export:** same with `format="pdf"` yields a valid PDF (`%PDF-` header) that opens, with
   headings/lists/**tables**/links laid out. **Render VALIDATED offline 2026-06-06** (valid v1.3 PDF,
   tables + typography); live download link is the deploy-time check.
3. **Ownership:** the download works for the creating user's session and 404s for a different
   non-admin user.
4. **Routing:** the model reaches for `export_document` on "generate a PDF / save as a file"
   requests, and does **not** use it for content that should just appear in the chat.
5. **Honesty preserved:** the v8 capability-honesty behavior is intact — the model uses the real tool
   instead of faking a file, and when a format genuinely can't be produced it says so.

## Deferred / out of scope

- `.docx` / `.csv` / other formats (add to `_FORMATS` + a renderer when a lived need appears).
- Rich PDF theming / page headers / fonts beyond fpdf2 core (latin-1). A Unicode TTF could be
  registered later for full-fidelity non-Latin output if needed.
- Embedding images. (Markdown image syntax renders as alt text / link, not an embedded raster.)
- Persisting exports into the self-generating RAG cache (separate post-soak idea).
