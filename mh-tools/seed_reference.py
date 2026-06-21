#!/usr/bin/env python3
"""
seed_reference.py — ingest the reference corpus into OWUI's `reference:python` KB (RFC-MH-003).

Fetch lives in fetch_docs.py (dump-path: llms_full / github_docs sparse-clone @tag / github_readme /
docstrings / web_page; rag-stack-design.md §1). This file is the INGEST half: create/clear the KB and
push each lib's assembled markdown through OWUI's standard file-upload + /file/add API — which embeds
correctly under BYPASS=False (RFC-MH-004). Chunk + MiniLM embed happen server-side (zero embedder drift).

  OWUI_API_KEY=… python seed_reference.py --reset        # clear + re-seed all 17 (the rebuild)
  OWUI_API_KEY=… python seed_reference.py --lib pydantic  # one lib
  python seed_reference.py --dry-run                       # fetch only (delegates to fetch_docs)
"""
import argparse
import io
import json
import os
import sys

import requests

from fetch_docs import SOURCES, fetch_docs

OWUI_BASE = os.environ.get("OWUI_BASE", "http://100.100.81.77:8080")
API = os.environ.get("OWUI_API_KEY", "")


def _h():
    return {"Authorization": f"Bearer {API}"}


def find_or_create_kb(name, desc):
    r = requests.get(f"{OWUI_BASE}/api/v1/knowledge/", headers=_h(), timeout=30)
    r.raise_for_status()
    for kb in r.json().get("items", []):
        if kb.get("name") == name:
            return kb["id"], False
    r = requests.post(f"{OWUI_BASE}/api/v1/knowledge/create", headers=_h(),
                      json={"name": name, "description": desc}, timeout=60)
    r.raise_for_status()
    return r.json()["id"], True


def reset_kb(kb_id):
    r = requests.post(f"{OWUI_BASE}/api/v1/knowledge/{kb_id}/reset", headers=_h(), timeout=120)
    r.raise_for_status()


def upload_and_add(kb_id, filename, content):
    # process_in_background=false -> fully extracted+embedded before /file/add (avoids the async race)
    fobj = io.BytesIO(content.encode("utf-8"))
    up = requests.post(f"{OWUI_BASE}/api/v1/files/?process_in_background=false", headers=_h(),
                       files={"file": (filename, fobj, "text/markdown")}, timeout=600)
    up.raise_for_status()
    fid = up.json()["id"]
    add = requests.post(f"{OWUI_BASE}/api/v1/knowledge/{kb_id}/file/add", headers=_h(),
                        json={"file_id": fid}, timeout=600)
    add.raise_for_status()
    return fid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", help="seed only this dist")
    ap.add_argument("--dry-run", action="store_true", help="fetch only; no API writes")
    ap.add_argument("--reset", action="store_true", help="clear the KB before seeding (full rebuild)")
    args = ap.parse_args()

    cfg = json.load(open(SOURCES))
    libs = [l for l in cfg["libraries"] if l.get("seed")]
    if args.lib:
        libs = [l for l in libs if l["dist"].lower() == args.lib.lower()]

    if not args.dry_run and not API:
        print("ERROR: OWUI_API_KEY not set."); sys.exit(2)

    kb_id = None
    if not args.dry_run:
        kb_id, created = find_or_create_kb(
            cfg["collection"],
            "L2b reference corpus — official docs for dev-env direct deps (RFC-MH-003, dump-path).")
        print(f"KB {cfg['collection']} = {kb_id} ({'created' if created else 'exists'})")
        if args.reset:
            reset_kb(kb_id)
            print("  reset (cleared) the KB")

    ok = fail = 0
    tot = 0
    for lib in libs:
        d = lib["dist"]
        try:
            text, n, strat = fetch_docs(lib)
        except Exception as e:
            print(f"  FAIL {d:24} fetch: {e}"); fail += 1; continue
        if not text.strip():
            print(f"  FAIL {d:24} empty ({strat})"); fail += 1; continue
        nbytes = len(text.encode("utf-8"))
        tot += nbytes
        print(f"  {d:24} {nbytes//1024:6}KB  {strat}")
        if args.dry_run:
            continue
        try:
            fname = f"reference-python__{d}__{lib['version']}.md".replace("/", "-")
            upload_and_add(kb_id, fname, text)
            ok += 1
        except Exception as e:
            print(f"       ! ingest: {e}"); fail += 1

    print(f"\n{'DRY ' if args.dry_run else ''}done — {ok} seeded, {fail} failed, {tot//1024} KB total.")
    if not args.dry_run:
        print(f"KB id = {kb_id}. Next: eval/reference_coverage_test.py (the gate), then §6 + K5.")


if __name__ == "__main__":
    main()
