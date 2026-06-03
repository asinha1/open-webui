# read_page — fetch one specific URL and return its readable content

**Status: v1.2 BUILT 2026-06-02 — PENDING RE-DEPLOY.** v1.0 was deployed in the v6/`--parallel 1`
window; the first lived test (soak-notes Day 4) exposed the renderer's limits, and a calibration pass
(operator + agent walking real pages: Wikipedia, derekthompson, theargumentmag, two Substack shares)
drove v1.2. **The deployed copy is still v1.0** — re-paste into Workspace→Tools + OWUI restart to go
live (bundle with the sidecar window). v1.2 adds:

- **HTML → Markdown** (`markdownify`, a new fork dep) instead of flat text — preserves headings,
  lists, **links**, and **tables** (Gemma reads markdown natively). Replaces v1.0's `get_text` which
  flattened tables into unparseable cell-streams.
- **Links preserved + absolutized** — relative hrefs resolve to absolute against the page URL, so the
  model can `read_page` them next ("navigate around"). **Images → ALT text only** (`![alt]`), URL
  dropped (CDN/avatar URLs were pure noise).
- **Fragment-focus** — a `#anchor` in the URL renders just that section (heading + its content, in
  document order, to the next same/higher heading), e.g. `…/List_of_largest_cities#List` → the 86-row
  table alone, not the 60 K whole page.
- **Client-side redirect following** — meta-refresh / JS `location.replace` "open in app" share
  interstitials (e.g. `open.substack.com/pub/…`, the Substack **Android** share format) are followed
  to the canonical article. SSRF-re-checked per hop.
- **Feeds** → compact `N. title — date — link` enumeration (140-episode feed in ~18 K, was chopped to
  ~15 at the old 8 K cap). **JSON** content-sniffed (iTunes `text/javascript`). **Cap → 60 K** (~22 K
  tok, holds a full long article). SSRF guard unchanged.

**Validated v1.2** (all live, 2026-06-02): theargumentmag 14 K/32 links; Substack Android share →
meta-refresh → canonical 22 K/37 links; Wikipedia `#List` → 86-row markdown table, 264 links
absolutized; audioboom RSS 140 episodes; loopback + tailnet SSRF refused. Offline: every renderer +
billion-laughs guard.

**Dependency:** `markdownify==1.2.2` (+`six`) — pinned in the fork's `pyproject.toml` +
`backend/requirements.txt`, and installed in the OWUI venv via `uv` (`.venv` has no pip). Graceful
fallback to flat text if the import is ever absent.

Design + acceptance below.

## Intent

Give the model a way to **read a specific page or feed** — one it (or the user) already has a
URL for — and get back the page's *full* readable content, not a search snippet. This is the
companion to `tavily_search`: search *finds* pages and returns ~1500-char snippets; `read_page`
*opens* one and returns its body.

## Why (the trigger — 2026-06-02 contended-profile test)

In lived use the model was asked to "list every episode of a 140+ episode podcast with links."
It correctly **declined** (snippet search can't enumerate; podcast directories are infinite-scroll
web apps that expose only ~10–20 recent episodes per page load) — good anti-confabulation, but a
real capability gap. See `~/service-data/open-webui/soak-notes.md` Day 4. `read_page` closes it:

- **Podcast RSS feeds list *every* episode** (title + date + link) in one static XML document.
  `read_page(<rss-url>)` returns the lot.
- **iTunes lookup chaining (docstring hint):** from an Apple Podcasts id (e.g. `id1695608652`),
  `read_page("https://itunes.apple.com/lookup?id=1695608652")` returns JSON containing `feedUrl`;
  then `read_page(feedUrl)` returns all episodes. The model can do this two-step itself.

Deferred since 2026-05-30 with the rule "build only if lived use shows snippet depth
insufficient." Lived use now shows it. This is that build.

## Model-facing surface (thin — 2 args, per the ≤4–5 rule)

```
read_page(url: str, max_chars: Optional[int] = None) -> str
```

- **`url`** (required) — the absolute http(s) URL to fetch.
- **`max_chars`** (optional) — override the default content cap for this call (e.g. raise it when
  the user explicitly wants a long enumeration like a full episode list). Clamped to a Valve ceiling.

Docstring must steer routing to avoid overlap with `tavily_search`:
> Use this to read a specific web page or feed when you already have its URL — e.g. a page a search
> surfaced, a link the user gave you, or a podcast/blog **RSS feed** (which lists *all* items, unlike
> a search snippet). Do NOT use it to search the web for a topic — use `tavily_search` for discovery.
> For an exhaustive list (every podcast episode, every post), fetch the site's RSS/Atom feed; if you
> only have an Apple Podcasts id, first read `https://itunes.apple.com/lookup?id=<id>` to get `feedUrl`.

## Behavior

1. **Fetch** via `aiohttp` GET (no SDK), browser-like `User-Agent`, `TIMEOUT` Valve (default 30s),
   manual redirect following (each hop SSRF-checked), response size hard-capped while streaming
   (`MAX_FETCH_BYTES`). **Also follows client-side redirects** — a `<meta http-equiv=refresh>` or JS
   `location.replace(...)` in the page head (the "open in app" share interstitials) is treated as
   another hop, SSRF-re-checked.
2. **Render by content-type:**
   - `text/html` → **convert to Markdown with `markdownify`** (main-content targeting: `<article>` /
     `<main>` / `<body>`; drop `script/style/nav/footer/aside/button/iframe`). Preserves headings,
     lists, **links** (relative → absolute so they're followable), and **tables** (→ markdown tables).
     **Images render as ALT text only** (`![alt]`, src dropped). A **`#fragment`** focuses rendering on
     that section (the anchor's heading + its content in document order, to the next same/higher
     heading; or the element itself if the id is on a container). Falls back to flat `get_text` if the
     `markdownify`/`bs4` deps are absent.
   - `application/rss+xml`, `application/atom+xml`, `text/xml`, `application/xml`, **or any body that
     sniffs as a feed** (`<rss`/`<feed`/`<channel`) → **parse into a compact enumeration**: one
     `N. <title> — <date> — <link>` line per `<item>`/`<entry>` (RSS link-text or Atom `href`),
     capped at `MAX_FEED_ITEMS`. **(Fix 2026-06-02 — raw passthrough was the original design but a
     218 KB / 140-episode feed hit the char cap at ~15 items; raw XML is ~10× larger per item than the
     enumeration needs. Compact parse fits a full 140-episode feed in ~18 K chars.)** Falls back to raw
     (collapsed) if the XML doesn't parse. DOCTYPE/ENTITY refused (billion-laughs guard; stdlib ET).
   - `application/json` → pass through pretty-ish (truncated) — for the iTunes lookup step.
   - `text/plain` → as-is. Other/binary types → refuse with a readable note (don't dump bytes).
3. **Truncate** to `max_chars` (call arg, clamped to `MAX_CONTENT_CHARS` Valve ceiling); append `…`
   and a note that content was truncated so the model knows the list may be incomplete.
4. **Emit a Source chip** (citation event) for the fetched URL, same pattern as `tavily_search`.
5. **Log** one INFO line (`mh.read_page`) with url, status, content-type, bytes, returned chars.

## Safety — SSRF guard (REQUIRED, this is the load-bearing bit)

A URL-fetch tool on a tailnet box is an SSRF vector — the model must **never** be able to fetch
internal services (`127.0.0.1:8081` llama-server, `:8080` OWUI, AdGuard, Home Assistant, the
tailnet, the LAN). Before fetching:

- Scheme must be `http`/`https` (reject `file:`, `ftp:`, `gopher:`, etc.).
- **Resolve the host and reject** if any resolved IP is loopback (`127/8`, `::1`), private
  (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`), link-local (`169.254/16`, `fe80::/10`),
  **CGNAT / Tailscale (`100.64/10`)**, or unspecified/multicast. Also reject bare `*.local` /
  `*.internal` hostnames. Re-validate after redirects (a public URL can 302 to an internal one).
- On a blocked host return a readable refusal string ("I can't fetch internal/private addresses"),
  never an exception.

## Valves (DB, never committed)

| Valve | Default | Purpose |
|---|---|---|
| `MAX_CONTENT_CHARS` | 60000 | ceiling on returned chars (~22K tok), sized to hold a full long article. `max_chars` arg clamps to this. **(8000 → 24000 → 60000 over 2026-06-02 as lived use showed feeds/articles/checklists chopped at the smaller caps.)** |
| `MAX_FEED_ITEMS` | 500 | max items enumerated from a feed (covers a 140-episode podcast; guards a pathological feed). |
| `MAX_FETCH_BYTES` | 5_000_000 | stop reading the response body past this (don't OOM on a huge file). |
| `TIMEOUT` | 30 | HTTP timeout (s). |
| `USE_TAVILY_EXTRACT` | false | if true, JS-heavy pages that return near-empty text fall back to Tavily Extract (`/extract`, reuses `TAVILY_API_KEY`, costs credits). Off by default — direct fetch handles RSS/JSON, which is the point. |
| `TAVILY_API_KEY` | "" | only read when `USE_TAVILY_EXTRACT` is on; mirror the `tavily_search` Valve. |

Keep `extract_depth`/credit cost out of the model's view — it's a Valve decision, not a per-call arg.

## Errors degrade gracefully (model-readable strings, never a stack trace)

- timeout → "reading that page timed out after Ns; try again or work from what you have."
- non-200 → "that page returned HTTP <code>; the URL may be wrong or the page gone."
- blocked host (SSRF guard) → refusal string above.
- empty/binary content → "that URL returned no readable text (it may be a media/binary file or a
  JS-only app)." (If `USE_TAVILY_EXTRACT`, try the fallback before giving up.)

## Acceptance criteria

1. **RSS enumeration (the trigger):** `read_page(<podcast rss feedUrl>)` returns episode titles +
   links well beyond the ~10–20 a snippet search surfaced; the model can then build the full table.
   **VALIDATED 2026-06-02 (post-fix):** the audioboom feed for "Movies You Forgot You Forgot" returns
   **all 140 episodes** compactly (~18 K chars, latest-first, title — date — link). Pre-fix it
   truncated to ~15 — the lived failure in the 06-02 enumeration test.
2. **HTML → readable text:** a normal article URL returns clean prose (no `<script>`/nav cruft),
   capped at `MAX_CONTENT_CHARS`, with the truncation note when cut.
3. **JSON passthrough:** `read_page("https://itunes.apple.com/lookup?id=<id>")` returns parseable
   JSON the model can pull `feedUrl` from.
4. **SSRF guard holds:** `read_page("http://127.0.0.1:8081/health")`,
   `http://100.x.y.z/…` (tailnet), and `http://192.168.1.10/…` (janus LAN) are all refused without
   a network call; a public URL that redirects to an internal host is refused post-redirect.
5. **Source chip** renders for the fetched URL (operator eyes — log can't show it).
6. **Routing:** the model reaches for `read_page` when given/holding a URL or asked to enumerate a
   feed, and for `tavily_search` (not `read_page`) for open-ended discovery. Watch for it trying to
   `read_page` a URL it guessed/hallucinated — prefer search-then-read.

## Deferred / out of scope

- HTML-table extraction into structured rows (return readable text; let the model tabulate).
- Pagination/crawl-following (one URL per call by design; the model chains calls).
- Headless-browser rendering beyond the optional Tavily Extract fallback.
