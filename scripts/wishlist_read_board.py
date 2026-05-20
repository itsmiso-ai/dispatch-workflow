#!/usr/bin/env python3
"""Read normal-lane work from Dispatch.

Legacy filename retained for compatibility. GitHub Projects are deprecated and
must not be queried.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def dispatch_queue(lane: str) -> list[dict]:
    url = os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000").rstrip("/")
    token = os.environ.get("DISPATCH_AGENT_TOKEN", "")
    if not token:
        print("ERROR: DISPATCH_AGENT_TOKEN not set")
        return []
    req = urllib.request.Request(
        f"{url}/api/agents/saffron/queue?lane={lane}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []


def main() -> int:
    try:
        items = dispatch_queue("normal")
    except Exception as e:
        print(f"ERROR: Dispatch normal queue read failed: {e}")
        return 1

    for item in items:
        print(json.dumps({
            "issue_id": item.get("issueId"),
            "number": item.get("number"),
            "title": item.get("title"),
            "repo": item.get("repoFullName"),
            "labels": item.get("labels") or [],
            "status": item.get("status"),
            "lane": item.get("lane"),
            "url": item.get("url"),
        }))

    if not items:
        print("Pipeline is clear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
