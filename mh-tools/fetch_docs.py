#!/usr/bin/env python3
"""
fetch_docs.py — dump-path doc fetcher for the reference corpus (RFC-MH-003 / rag-stack-design.md §1).

Replaces the brittle whole-site crawl with: prefer a clean, version-exact SOURCE.
Per lib (driven by reference-sources.json `source_type` + `repo`):
  llms_full     -> the published llms-full.txt (chromadb, PyMuPDF)
  github_docs   -> sparse-clone the repo's docs/ folder AT THE RELEASE TAG (blobless, shallow), walk .md/.rst
  github_readme -> raw README at the tag (README *is* the docs: pdfplumber, markdownify, PyYAML)
  docstrings    -> import the installed package, walk public API, inspect.getdoc (langchain-core, playwright)
  web_page      -> single-page bs4->md (beautifulsoup4: one self-contained doc page)
  + supplement_docstrings: append docstrings to a github_docs lib (autodoc-heavy: numpy, aiohttp, …)

Dry-run (`--dry-run`, or no OWUI_API_KEY) reports per-lib: strategy, size, and PROBE-symbol presence
(the §2 coverage gate, checked on the fetched blob BEFORE ingest). Tag auto-resolves from the installed
version via `git ls-remote` (handles v-prefix + monorepo `<dist>==<ver>` forms).
"""
import importlib, inspect, json, os, re, shutil, subprocess, sys, tempfile
import certifi, requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(HERE, "reference-sources.json")
UA = {"User-Agent": "mh-fetch-docs/1.0"}

# path SEGMENTS that are noise for a coding reference (release notes, NEPs, dev/build scaffolding)
_NOISE_DIR = re.compile(
    r"(^|/)(release|releases|release[-_]?notes|neps?|changelog|whatsnew|whats[-_]new|"
    r"dev|devel|development|contributing|governance|roadmap|_templates|_static|_build|\.github)(/|$)",
    re.I,
)

# §2 coverage probe symbols — presence in the fetched blob = Tier-1 pre-ingest check
PROBES = {
    "pydantic": ["field_validator", "BaseModel", "model_config"],
    "chromadb": ["create_collection", "n_results"],
    "langchain-core": ["Runnable", "invoke"],
    "sentence-transformers": ["encode", "similarity"],
    "pdfplumber": ["extract_text", "extract_tables"],
    "PyMuPDF": ["get_text", "get_pixmap"],
    "beautifulsoup4": ["find_all", "BeautifulSoup"],
    "aiohttp": ["ClientSession", "RouteTableDef"],
    "numpy": ["ndarray", "reshape"],
    "pillow": ["resize", "convert"],
    "playwright": ["page", "locator"],
    "starlette": ["JSONResponse", "Middleware"],
    "python-socketio": ["AsyncServer", "emit"],
    "fpdf2": ["add_page", "set_font"],
    "Markdown": ["extensions", "Extension"],
    "markdownify": ["heading_style", "markdownify"],
    "PyYAML": ["safe_load", "dump"],
}


def _get(url, timeout=25):
    r = requests.get(url, headers=UA, timeout=timeout, verify=certifi.where())
    r.raise_for_status()
    return r.text


def _resolve_tag(repo, version, dist):
    """Find the git tag matching the installed version (v-prefix, monorepo <dist>==<ver>, fuzzy)."""
    out = subprocess.run(["git", "ls-remote", "--tags", f"https://github.com/{repo}"],
                         capture_output=True, text=True, timeout=90).stdout
    tags = [ln.split("refs/tags/")[1] for ln in out.splitlines()
            if "refs/tags/" in ln and not ln.strip().endswith("^{}")]
    cands = [version, "v" + version, f"{dist}=={version}", f"{dist}-{version}",
             f"{dist.lower()}=={version}", f"release-{version}", version.replace(".", "_")]
    for c in cands:
        if c in tags:
            return c
    hits = [t for t in tags if version in t]
    return hits[0] if hits else None


def _sparse_clone(repo, tag, paths):
    d = tempfile.mkdtemp(prefix="refdump_")
    subprocess.run(["git", "clone", "--depth", "1", "--branch", tag, "--filter=blob:none",
                    "--sparse", f"https://github.com/{repo}", d],
                   capture_output=True, timeout=240, check=True)
    subprocess.run(["git", "-C", d, "sparse-checkout", "set", *paths], capture_output=True, timeout=60)
    return d


def fetch_llms(lib):
    p = lib["docs_url"]
    root = p if p.endswith("/") else p.rsplit("/", 1)[0] + "/"
    for name in ("llms-full.txt", "llms.txt"):
        try:
            t = _get(root + name)
            if len(t.strip()) > 500 and not t.lstrip()[:6].lower().startswith(("<!doct", "<html")):
                return f"# {lib['dist']} {lib['version']} — llms\n\n{t}", 1, f"llms_full ({name})"
        except Exception:
            pass
    return None, 0, "no llms.txt"


def fetch_github_docs(lib):
    repo, ver, dist = lib["repo"], lib["version"], lib["dist"]
    tag = _resolve_tag(repo, ver, dist)
    if not tag:
        return None, 0, f"no tag for {ver}"
    d = _sparse_clone(repo, tag, ["docs", "doc", "documentation"])
    files = []
    for dp, _, fs in os.walk(d):
        if "/.git" in dp:
            continue
        for f in fs:
            if f.endswith((".md", ".rst", ".txt")):
                rel = os.path.relpath(os.path.join(dp, f), d)
                if _NOISE_DIR.search(rel):   # skip release-notes/NEPs/dev/build noise (numpy etc.)
                    continue
                files.append(os.path.join(dp, f))
    parts = [f"# {dist} {ver} — docs ({repo} @ {tag})\n"]
    for fp in sorted(files):
        try:
            txt = open(fp, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        parts.append(f"\n\n---\n## {os.path.relpath(fp, d)}\n\n{txt}")
    shutil.rmtree(d, ignore_errors=True)
    return "".join(parts), len(files), f"github_docs {tag} ({len(files)} files)"


def fetch_github_readme(lib):
    repo, ver, dist = lib["repo"], lib["version"], lib["dist"]
    tag = _resolve_tag(repo, ver, dist) or "HEAD"
    for name in ("README.md", "README.rst", "readme.md", "Readme.md"):
        try:
            t = _get(f"https://raw.githubusercontent.com/{repo}/{tag}/{name}")
            if len(t) > 200:
                return f"# {dist} {ver} — README ({repo}@{tag})\n\n{t}", 1, f"github_readme {name}@{tag}"
        except Exception:
            pass
    return None, 0, "no readme"


def fetch_docstrings(lib, deep=None):
    """deep=True (primary docstrings libs) walks submodules; deep=False (supplements) stays top-level."""
    import pkgutil
    root = lib["import_root"]
    if deep is None:
        deep = (lib.get("source_type") == "docstrings")
    try:
        mod = importlib.import_module(root)
    except Exception as e:
        return None, 0, f"import failed: {e}"
    parts = [f"# {lib['dist']} {lib['version']} — API docstrings ({root})\n"]
    seen, n, chars = set(), 0, 0
    CAP = 2_000_000  # ~2MB ceiling so a big package (langchain) can't dominate the corpus

    def add(name, obj):
        nonlocal n, chars
        if chars > CAP:
            return
        try:
            doc = inspect.getdoc(obj)
        except Exception:
            return
        if doc and len(doc) > 40 and name not in seen:
            seen.add(name)
            parts.append(f"\n\n---\n## {name}\n\n{doc}")
            n += 1
            chars += len(doc)

    mods = [(root, mod)]
    if deep and hasattr(mod, "__path__"):
        count = 0
        for mi in pkgutil.walk_packages(mod.__path__, prefix=root + "."):
            if mi.name.count(".") > 3 or any(x in mi.name for x in ("._", ".test", ".tests", "_internal")):
                continue
            try:
                mods.append((mi.name, importlib.import_module(mi.name)))
                count += 1
            except Exception:
                continue
            if count >= 300:
                break
    for mname, m in mods:
        add(mname, m)
        members = getattr(m, "__all__", None) or [a for a in dir(m) if not a.startswith("_")]
        for nm in members:
            try:
                obj = getattr(m, nm)
            except Exception:
                continue
            if inspect.isclass(obj) or inspect.isfunction(obj):
                add(f"{mname}.{nm}", obj)
                if inspect.isclass(obj):
                    for mn in [a for a in dir(obj) if not a.startswith("_")]:
                        try:
                            add(f"{mname}.{nm}.{mn}", getattr(obj, mn))
                        except Exception:
                            pass
    return "".join(parts), n, f"docstrings ({n} symbols{', deep' if deep else ''})"


def fetch_web(lib):
    try:
        html = _get(lib["docs_url"])
    except Exception as e:
        return None, 0, f"web fetch failed: {e}"
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        t.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    txt = md(str(main), heading_style="ATX")
    return f"# {lib['dist']} {lib['version']} — {lib['docs_url']}\n\n{txt}", 1, "web_page"


def fetch_docs(lib):
    st = lib["source_type"]
    fn = {"llms_full": fetch_llms, "github_docs": fetch_github_docs,
          "github_readme": fetch_github_readme, "docstrings": fetch_docstrings,
          "web_page": fetch_web}.get(st)
    if not fn:
        return "", 0, "skip"
    blob, n, strat = fn(lib)
    if lib.get("supplement_docstrings"):
        ds, dn, _ = fetch_docstrings(lib)
        if ds:
            blob = (blob or "") + "\n\n" + ds
            strat += f" +docstrings({dn})"
    return blob or "", n, strat


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
    cfg = json.load(open(SOURCES))
    print(f"{'lib':24} {'size':>8}  probes   strategy")
    for lib in cfg["libraries"]:
        if not lib.get("seed"):
            continue
        if only and lib["dist"].lower() != only.lower():
            continue
        try:
            blob, n, strat = fetch_docs(lib)
        except Exception as e:
            print(f"{lib['dist']:24} {'ERR':>8}  -------  {e}")
            continue
        syms = PROBES.get(lib["dist"], [])
        hit = sum(1 for s in syms if s in blob)
        flag = "  <-- THIN" if (not blob or hit < max(1, len(syms) - 1)) else ""
        print(f"{lib['dist']:24} {len(blob)//1024:6}KB  {hit}/{len(syms)}     {strat}{flag}")
