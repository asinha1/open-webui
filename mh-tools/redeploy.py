#!/usr/bin/env python3
"""[mh] Redeploy mh-tools from source into a running OWUI via the tools-update
API — the supported path that RE-PARSES the spec (never a raw webui.db
content-swap). Automates the README's manual "edit → re-paste in the UI" step.

  OWUI_API_KEY=<admin key>  [OWUI_URL=http://100.100.81.77:8080]  \
      python redeploy.py [tool_id ...]

Default: redeploys every mh-tool below. After it runs, RESTART OWUI so the
running uvicorn process loads the new content (a live DB write is not reliably
re-read — the documented lesson):  ~/service-data/restart-service.sh open-webui
"""
import json
import os
import pathlib
import sys

import requests

URL = os.environ.get("OWUI_URL", "http://100.100.81.77:8080").rstrip("/")
KEY = os.environ.get("OWUI_API_KEY")
HERE = pathlib.Path(__file__).resolve().parent

# OWUI tool id  ->  source file in this directory
TOOLS = {
    "tavily_web_search": "tavily_search.py",
    "read_page": "read_page.py",
    "deep_research": "deep_research.py",
    "knowledge_search": "knowledge_search.py",
    "research_search": "research_search.py",
    "export_document": "export_document.py",
    "read_pdf": "read_pdf.py",
}


def main():
    if not KEY:
        sys.exit("set OWUI_API_KEY (an admin key) in the environment")
    h = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    ids = sys.argv[1:] or list(TOOLS)
    rc = 0
    for tid in ids:
        f = TOOLS.get(tid)
        if not f:
            print(f"  ? {tid}: unknown tool id, skip")
            continue
        content = (HERE / f).read_text()
        g = requests.get(f"{URL}/api/v1/tools/id/{tid}", headers=h, timeout=15)
        if g.status_code != 200:
            print(f"  x {tid}: GET {g.status_code} {g.text[:160]}")
            rc = 1
            continue
        cur = g.json()
        form = {"id": tid, "name": cur["name"], "content": content, "meta": cur.get("meta") or {}}
        u = requests.post(
            f"{URL}/api/v1/tools/id/{tid}/update", headers=h, data=json.dumps(form), timeout=30
        )
        if u.status_code == 200:
            print(f"  ok {tid}: redeployed from {f}")
        else:
            print(f"  x {tid}: update {u.status_code} {u.text[:200]}")
            rc = 1
    print("\nNOW RESTART OWUI:  ~/service-data/restart-service.sh open-webui")
    return rc


if __name__ == "__main__":
    sys.exit(main())
