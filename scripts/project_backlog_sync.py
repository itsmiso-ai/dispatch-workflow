#!/usr/bin/env python3
"""Dispatch scheduled issue sync wrapper.

GitHub Projects are deprecated. This script name is kept only so existing
heartbeat wiring continues to work while using Dispatch as the work system.
Dispatch v0.3 owns primary cache freshness through the scheduled sync runner;
heartbeat calls this as a best-effort freshness check, not as the source of
truth for queue selection.

Usage: python3 project_backlog_sync.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def dispatch_base_url() -> str:
    return os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000").rstrip("/")


def dispatch_token() -> str:
    return os.environ.get("DISPATCH_AGENT_TOKEN", "")


def post_sync() -> dict:
    token = dispatch_token()
    if not token:
        raise RuntimeError("DISPATCH_AGENT_TOKEN not set")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(
        f"{dispatch_base_url()}/api/sync/scheduled",
        data=b"{}",
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Dispatch scheduled sync")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without syncing")
    parser.add_argument("--repo", action="append", help="Ignored; Dispatch sync owns repo selection")
    args = parser.parse_args()

    if args.dry_run:
        print("Dispatch scheduled sync dry-run: no changes made")
        return 0

    try:
        result = post_sync()
    except urllib.error.HTTPError as e:
        print(f"ERROR: Dispatch sync failed: HTTP {e.code} {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: Dispatch sync failed: {e}", file=sys.stderr)
        return 1

    if not result.get("success"):
        print(f"ERROR: Dispatch scheduled sync returned unsuccessful response: {str(result)[:300]}", file=sys.stderr)
        return 1

    issue_result = result.get("issues") or result
    repos = issue_result.get("repos", 0)
    synced = issue_result.get("syncedCount", 0)
    results = issue_result.get("results") or []
    errors = [r for r in results if r.get("error")]

    print(f"Dispatch scheduled sync: repos={repos} synced={synced} errors={len(errors)}")
    for item in results:
        repo = item.get("repo", "?")
        count = item.get("synced", 0)
        error = item.get("error")
        if error:
            print(f"  [!] {repo}: {error}")
        else:
            print(f"  {repo}: synced={count}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
