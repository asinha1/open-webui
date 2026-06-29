#!/usr/bin/env python3
"""
seed_home_networking.py — (re)ingest the home_networking repo docs into the
`home-networking-repo` KB. The collection was a one-time UI ingest (2026-05-29)
and went STALE (missing Phase 5 Canvas + all of Phase 6), so the model was
answering from outdated repo state. This makes the re-ingest repeatable and is
the basis for an auto-sync (a git-hook / cron calling `--reset`).

Self-contained (the same OWUI file-upload + /file/add API seed_reference.py uses;
chunk + MiniLM embed happen server-side under BYPASS=False).

  OWUI_API_KEY=… python seed_home_networking.py --reset   # clear + re-ingest the doc set
  python seed_home_networking.py --dry-run                # list files, no API writes

Scope = the durable "what is the setup" docs (overviews, roadmap, reference,
incidents, CLAUDE) — NOT the provisioning playbooks/build-logs (operational, huge,
and a different audience). Adjust PATTERNS to taste.
"""
import argparse, glob, os, sys, io

OWUI_BASE = os.environ.get("OWUI_BASE", "http://100.100.81.77:8080")
API = os.environ.get("OWUI_API_KEY", "")
REPO = os.environ.get("HN_REPO", os.path.expanduser("~/code/home_networking"))
COLLECTION = "home-networking-repo"

PATTERNS = [
    "CLAUDE.md",
    "devices/*/overview.md",
    "services/*/overview.md",
    "ideas/*.md",
    "reference/*.md",
    "incidents/*.md",
]


def _h():
    return {"Authorization": f"Bearer {API}"}


def find_or_create_kb(name, desc):
    import requests
    r = requests.get(f"{OWUI_BASE}/api/v1/knowledge/", headers=_h(), timeout=30); r.raise_for_status()
    for kb in r.json().get("items", []):
        if kb.get("name") == name:
            return kb["id"], False
    r = requests.post(f"{OWUI_BASE}/api/v1/knowledge/create", headers=_h(),
                      json={"name": name, "description": desc}, timeout=60); r.raise_for_status()
    return r.json()["id"], True


def reset_kb(kb_id):
    import requests
    requests.post(f"{OWUI_BASE}/api/v1/knowledge/{kb_id}/reset", headers=_h(), timeout=120).raise_for_status()


def upload_and_add(kb_id, filename, content):
    import requests
    fobj = io.BytesIO(content.encode("utf-8"))
    up = requests.post(f"{OWUI_BASE}/api/v1/files/?process_in_background=false", headers=_h(),
                       files={"file": (filename, fobj, "text/markdown")}, timeout=600); up.raise_for_status()
    fid = up.json()["id"]
    requests.post(f"{OWUI_BASE}/api/v1/knowledge/{kb_id}/file/add", headers=_h(),
                  json={"file_id": fid}, timeout=600).raise_for_status()
    return fid


def docs():
    out = []
    for p in PATTERNS:
        out += sorted(glob.glob(os.path.join(REPO, p)))
    return out


def flatname(path):
    return os.path.relpath(path, REPO).replace("/", "_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="clear the KB before ingesting (full refresh)")
    ap.add_argument("--dry-run", action="store_true", help="list the doc set; no API writes")
    a = ap.parse_args()

    files = docs()
    print(f"{len(files)} docs from {REPO}")
    if a.dry_run:
        for f in files:
            print(f"  {flatname(f):42} {os.path.getsize(f)//1024:4} KB")
        return
    if not API:
        print("ERROR: OWUI_API_KEY not set."); sys.exit(2)

    kb_id, created = find_or_create_kb(
        COLLECTION, "home_networking repo docs — the fleet's setup knowledge "
                    "(overviews, roadmap, reference, incidents). Re-ingest via seed_home_networking.py.")
    print(f"KB {COLLECTION} = {kb_id} ({'created' if created else 'exists'})")
    if a.reset:
        reset_kb(kb_id); print("  reset (cleared) the KB")

    ok = fail = 0
    for f in files:
        try:
            upload_and_add(kb_id, flatname(f), open(f, encoding="utf-8").read()); ok += 1
            print(f"  + {flatname(f)}")
        except Exception as e:
            print(f"  ! {flatname(f)}: {e}"); fail += 1
    print(f"\ndone — {ok} ingested, {fail} failed. (re-run after repo doc changes, or wire a git-hook.)")


if __name__ == "__main__":
    main()
