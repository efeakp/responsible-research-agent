"""Create the roadmap's labels, milestones and issues on GitHub from .github/milestones.json.

Dry run by default: it prints what it would create. Add --apply to create.
It never edits or deletes anything: items whose title already exists are skipped,
so it is safe to run more than once.

    set GITHUB_TOKEN=<token with Issues: read & write on the repo>   (PowerShell: $env:GITHUB_TOKEN="...")
    python scripts/create_milestones.py efeakp/responsible-research-agent          # dry run
    python scripts/create_milestones.py efeakp/responsible-research-agent --apply  # create
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

PLAN = Path(__file__).resolve().parent.parent / ".github" / "milestones.json"


def paged(client: httpx.Client, url: str, **params) -> list[dict]:
    out, page = [], 1
    while True:
        r = client.get(url, params={**params, "per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", help="owner/name")
    ap.add_argument("--apply", action="store_true", help="actually create (default: dry run)")
    a = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Set GITHUB_TOKEN first.", file=sys.stderr)
        return 2
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    base = f"https://api.github.com/repos/{a.repo}"
    c = httpx.Client(headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/vnd.github+json",
                              "X-GitHub-Api-Version": "2022-11-28"}, timeout=30)

    def create(kind: str, url: str, payload: dict) -> dict | None:
        print(f"{'create' if a.apply else 'would create'} {kind}: {payload.get('title') or payload.get('name')}")
        if not a.apply:
            return None
        r = c.post(url, json=payload)
        if r.status_code >= 300:
            print(f"  failed: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
            r.raise_for_status()
        return r.json()

    existing_labels = {l["name"] for l in paged(c, f"{base}/labels")}
    for l in plan["labels"]:
        if l["name"] in existing_labels:
            print(f"skip label (exists): {l['name']}")
        else:
            create("label", f"{base}/labels", l)

    milestones = {m["title"]: m for m in paged(c, f"{base}/milestones", state="all")}
    issues = {i["title"] for i in paged(c, f"{base}/issues", state="all") if "pull_request" not in i}
    for m in plan["milestones"]:
        if m["title"] in milestones:
            print(f"skip milestone (exists): {m['title']}")
            number = milestones[m["title"]]["number"]
        else:
            payload = {"title": m["title"], "description": m["description"]}
            if m.get("due_on"):
                payload["due_on"] = m["due_on"]
            made = create("milestone", f"{base}/milestones", payload)
            number = made["number"] if made else None
        for i in m["issues"]:
            if i["title"] in issues:
                print(f"  skip issue (exists): {i['title']}")
                continue
            payload = {"title": i["title"], "body": i["body"], "labels": i["labels"]}
            if number:
                payload["milestone"] = number
            create("  issue", f"{base}/issues", payload)
    if not a.apply:
        print("\nDry run only. Re-run with --apply to create these.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
