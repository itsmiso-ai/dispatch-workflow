#!/usr/bin/env python3
"""Dispatch-backed compatibility wrapper for Saffron PR review-fix work.

Dispatch owns PR review-fix queue state. This script keeps the old Saffron CLI
and Python function surface working while routing enqueue/list/next/mark calls to
Dispatch's `/api/pr-fix-queue/*` endpoints.

If Dispatch is unreachable, the read paths return an empty queue and write paths
fail loudly instead of silently resurrecting workspace-local JSON as source of
truth. The old local `.state/pr_fix_queue.json` is no longer authoritative.

Lane compatibility:
  - "escalated" is canonical for Saffron prompts.
  - Dispatch stores lanes as uppercase enum values.
  - "gpt" is accepted as a legacy alias for "escalated".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_PATH = Path("/home/node/.openclaw/workspace-saffron/.state/pr_fix_queue.json")
VALID_LANES = {"normal", "escalated", "needs-human"}
LEGACY_LANE_ALIAS = {"gpt": "escalated", "GPT": "escalated"}
VALID_STATUSES = {"queued", "fixed", "blocked", "stale", "ignored"}

LANE_TO_DISPATCH = {
    "normal": "normal",
    "escalated": "escalated",
    "needs-human": "needs-human",
}
DISPATCH_LANE_TO_LOCAL = {
    "NORMAL": "normal",
    "ESCALATED": "escalated",
    "GPT": "escalated",
    "NEEDS_HUMAN": "needs-human",
    "needs-human": "needs-human",
    "normal": "normal",
    "escalated": "escalated",
    "gpt": "escalated",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_lane(lane: str | None) -> str:
    """Map legacy lane aliases to canonical local names."""
    if not lane:
        return "normal"
    return LEGACY_LANE_ALIAS.get(lane, LEGACY_LANE_ALIAS.get(str(lane).lower(), str(lane).lower()))


def normalize_status(status: str | None) -> str:
    return str(status or "queued").lower().replace("_", "-")


def item_id(repo: str, pr: int | str) -> str:
    return f"{repo}#{int(pr)}"


def dispatch_base_url() -> str:
    return os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000").rstrip("/")


def dispatch_token() -> str:
    return os.environ.get("DISPATCH_AGENT_TOKEN", "")


def dispatch_request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 20) -> Any:
    token = dispatch_token()
    if not token:
        raise RuntimeError("DISPATCH_AGENT_TOKEN not set")

    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Name": "saffron",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"{dispatch_base_url()}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        if not raw:
            return None
        return json.loads(raw)


def to_local_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize Dispatch PR-fix item shape to the legacy Saffron shape."""
    repo = str(item.get("repo") or "")
    pr = int(item.get("pr") or 0)
    lane = normalize_lane(DISPATCH_LANE_TO_LOCAL.get(str(item.get("lane")), str(item.get("lane") or "normal")))
    status = normalize_status(item.get("status"))
    local = dict(item)
    local.update(
        {
            "id": item.get("id") or item_id(repo, pr),
            "repo": repo,
            "pr": pr,
            "lane": lane,
            "status": status,
            "reason": item.get("reason") or "",
            "feedback": item.get("feedback") or [],
            "evidenceKeys": item.get("evidenceKeys") or [],
            "queuedAt": item.get("queuedAt") or now_iso(),
            "updatedAt": item.get("updatedAt") or item.get("queuedAt") or now_iso(),
        }
    )
    return local


def state_from_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": {item_id(i["repo"], i["pr"]): i for i in items if i.get("repo") and i.get("pr")}}


def load_state() -> dict[str, Any]:
    """Compatibility helper for existing watcher code.

    Returns active Dispatch PR-fix items keyed as `repo#pr`. This intentionally
    does not read the old local JSON state file.
    """
    return state_from_items(queued_items(include_blocked=True))


def save_state(_state: dict[str, Any]) -> None:
    raise RuntimeError("PR fix queue state is owned by Dispatch; local save_state is disabled")


def migrate_legacy_lanes(_state: dict[str, Any]) -> bool:
    return False


def enqueue(
    *,
    repo: str,
    pr: int,
    lane: str,
    reason: str,
    feedback: str,
    evidence_key: str,
    issue: int | None = None,
    branch: str | None = None,
    url: str | None = None,
    title: str | None = None,
    head_sha: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    canonical_lane = normalize_lane(lane)
    if canonical_lane not in VALID_LANES:
        canonical_lane = "needs-human"
    payload: dict[str, Any] = {
        "repo": repo,
        "pr": int(pr),
        "lane": LANE_TO_DISPATCH[canonical_lane],
        "reason": reason,
        "feedback": feedback,
        "evidenceKey": evidence_key,
    }
    for key, value in {
        "issue": int(issue) if issue else None,
        "branch": branch,
        "url": url,
        "title": title,
        "headSha": head_sha,
        "author": author,
    }.items():
        if value not in (None, ""):
            payload[key] = value

    item = dispatch_request("/api/pr-fix-queue/enqueue", method="POST", payload=payload, timeout=30)
    if not isinstance(item, dict):
        raise RuntimeError(f"unexpected Dispatch enqueue response: {item!r}")
    return to_local_item(item)


def queued_items(lane: str | None = None, include_blocked: bool = False) -> list[dict[str, Any]]:
    params: dict[str, str] = {}
    if lane:
        canonical_lane = normalize_lane(lane)
        params["lane"] = LANE_TO_DISPATCH.get(canonical_lane, canonical_lane)
    if include_blocked:
        params["include_blocked"] = "true"
    query = f"?{urllib.parse.urlencode(params)}" if params else ""

    try:
        items = dispatch_request(f"/api/pr-fix-queue/queued{query}", timeout=20)
    except Exception as e:
        print(f"[pr-fix-queue] Dispatch queue read failed: {e}", file=sys.stderr)
        return []

    if not isinstance(items, list):
        print(f"[pr-fix-queue] Unexpected Dispatch queue response: {items!r}", file=sys.stderr)
        return []
    selected = [to_local_item(item) for item in items if isinstance(item, dict)]
    selected.sort(key=lambda item: (item.get("queuedAt") or "", item.get("repo") or "", item.get("pr") or 0))
    return selected


def mark(repo: str, pr: int, status: str, note: str | None = None) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    payload: dict[str, Any] = {"repo": repo, "pr": int(pr), "status": status}
    if note:
        payload["note"] = note
    item = dispatch_request("/api/pr-fix-queue/mark", method="POST", payload=payload, timeout=30)
    if not isinstance(item, dict):
        raise RuntimeError(f"unexpected Dispatch mark response: {item!r}")
    return to_local_item(item)


def print_jsonl(items: list[dict[str, Any]]) -> None:
    for item in items:
        print(json.dumps(item, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the Dispatch-backed Saffron PR review-fix queue")
    sub = parser.add_subparsers(dest="cmd", required=True)

    enq = sub.add_parser("enqueue")
    enq.add_argument("--repo", required=True)
    enq.add_argument("--pr", required=True, type=int)
    enq.add_argument("--lane", required=True, choices=sorted(VALID_LANES))
    enq.add_argument("--reason", required=True)
    enq.add_argument("--feedback", required=True)
    enq.add_argument("--evidence-key", required=True)
    enq.add_argument("--issue", type=int)
    enq.add_argument("--branch")
    enq.add_argument("--url")
    enq.add_argument("--title")
    enq.add_argument("--head-sha")
    enq.add_argument("--author")

    lst = sub.add_parser("list")
    lst.add_argument("--lane", choices=sorted(VALID_LANES))
    lst.add_argument("--include-blocked", action="store_true")

    nxt = sub.add_parser("next")
    nxt.add_argument("--lane", required=True, choices=sorted(VALID_LANES - {"needs-human"}))
    nxt.add_argument("--json", action="store_true", help="Print only JSON for queued item; otherwise human clear message when empty")

    mrk = sub.add_parser("mark")
    mrk.add_argument("--repo", required=True)
    mrk.add_argument("--pr", required=True, type=int)
    mrk.add_argument("--status", required=True, choices=sorted(VALID_STATUSES))
    mrk.add_argument("--note")

    ign = sub.add_parser("ignore")
    ign.add_argument("--repo", required=True)
    ign.add_argument("--pr", required=True, type=int)
    ign.add_argument("--evidence-key", required=True)
    ign.add_argument("--reason", default="not actionable")

    sub.add_parser("summary")

    args = parser.parse_args()

    try:
        if args.cmd == "enqueue":
            item = enqueue(
                repo=args.repo,
                pr=args.pr,
                lane=args.lane,
                reason=args.reason,
                feedback=args.feedback,
                evidence_key=args.evidence_key,
                issue=args.issue,
                branch=args.branch,
                url=args.url,
                title=args.title,
                head_sha=args.head_sha,
                author=args.author,
            )
            print(json.dumps(item, sort_keys=True))
            return 0

        if args.cmd == "list":
            print_jsonl(queued_items(args.lane, include_blocked=args.include_blocked))
            return 0

        if args.cmd == "next":
            items = queued_items(args.lane)
            if not items:
                if args.json:
                    print("{}")
                else:
                    print(f"PR fix queue is clear for {args.lane}.")
                return 0
            print(json.dumps(items[0], sort_keys=True))
            return 0

        if args.cmd == "mark":
            item = mark(args.repo, args.pr, args.status, args.note)
            print(json.dumps(item, sort_keys=True))
            return 0

        if args.cmd == "ignore":
            enqueue(
                repo=args.repo,
                pr=args.pr,
                lane="needs-human",
                reason=args.reason,
                feedback=args.reason,
                evidence_key=args.evidence_key,
            )
            item = mark(args.repo, args.pr, "ignored", args.reason)
            print(json.dumps(item, sort_keys=True))
            return 0

        if args.cmd == "summary":
            items = queued_items(include_blocked=True)
            counts: dict[str, int] = {}
            lane_counts: dict[str, int] = {}
            for item in items:
                status = item.get("status") or "unknown"
                lane = item.get("lane") or "unknown"
                counts[status] = counts.get(status, 0) + 1
                if status == "queued":
                    lane_counts[lane] = lane_counts.get(lane, 0) + 1
            print(json.dumps({"counts": counts, "queuedByLane": lane_counts}, sort_keys=True))
            return 0

    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, ValueError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
