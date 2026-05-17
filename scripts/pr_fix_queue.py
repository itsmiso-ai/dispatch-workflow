#!/usr/bin/env python3
"""Persistent review-fix queue for cron-authored PRs.

The heartbeat follow-up watcher enqueues open PRs that need follow-up. The
wishlist workers consume queued PRs before selecting new project-board work.

Lane compatibility:
  - "escalated" is the canonical lane name (replaces legacy "gpt").
  - "gpt" is accepted as an alias and is auto-migrated to "escalated".
  - New items prefer "escalated"; stored items with "lane: gpt" are migrated.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_PATH = Path("/home/node/.openclaw/workspace-saffron/.state/pr_fix_queue.json")
# Canonical lanes; "gpt" is a legacy alias (see LANE_ALIASES).
VALID_LANES = {"normal", "escalated", "needs-human"}
LEGACY_LANE_ALIAS = {"gpt": "escalated"}
VALID_STATUSES = {"queued", "fixed", "blocked", "stale", "ignored"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_lane(lane: str) -> str:
    """Map legacy lane aliases to canonical names."""
    return LEGACY_LANE_ALIAS.get(lane, lane)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"items": {}}
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception:
        return {"items": {}}
    if not isinstance(data, dict):
        return {"items": {}}
    data.setdefault("items", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def migrate_legacy_lanes(state: dict[str, Any]) -> bool:
    """Return True if any items were migrated."""
    changed = False
    for key, item in state.get("items", {}).items():
        raw_lane = item.get("lane", "")
        canonical = normalize_lane(raw_lane)
        if canonical != raw_lane:
            item["lane"] = canonical
            ts = now_iso()
            item.setdefault("history", []).append(
                {"at": ts, "action": "migrate_lane", "from": raw_lane, "to": canonical}
            )
            item["updatedAt"] = ts
            changed = True
    return changed


def item_id(repo: str, pr: int | str) -> str:
    return f"{repo}#{int(pr)}"


def unique_append(values: list[Any], value: Any, max_items: int | None = None) -> list[Any]:
    if value in (None, ""):
        return values
    if value not in values:
        values.append(value)
    if max_items is not None and len(values) > max_items:
        return values[-max_items:]
    return values


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
    # Accept legacy aliases but canonicalize to new names.
    if lane not in VALID_LANES and normalize_lane(lane) not in VALID_LANES:
        lane = "needs-human"
    lane = normalize_lane(lane)

    state = load_state()
    # Migrate any legacy lane values on every enqueue.
    migrate_legacy_lanes(state)

    key = item_id(repo, pr)
    timestamp = now_iso()
    existing = state["items"].get(key)

    if existing:
        item = existing
        item.setdefault("feedback", [])
        item.setdefault("evidenceKeys", [])
        item.setdefault("history", [])
        duplicate = evidence_key and evidence_key in item["evidenceKeys"]
        if not duplicate:
            item["queuedAt"] = item.get("queuedAt") or timestamp
            item["updatedAt"] = timestamp
            item["status"] = "queued" if lane != "needs-human" else "blocked"
            item["lane"] = lane
            item["reason"] = reason
            item["feedback"] = unique_append(item["feedback"], feedback, max_items=12)
            item["evidenceKeys"] = unique_append(item["evidenceKeys"], evidence_key, max_items=40)
            item["history"].append({"at": timestamp, "action": "enqueue", "reason": reason, "evidenceKey": evidence_key})
    else:
        item = {
            "id": key,
            "repo": repo,
            "pr": int(pr),
            "issue": int(issue) if issue else None,
            "branch": branch,
            "lane": lane,
            "reason": reason,
            "feedback": [feedback] if feedback else [],
            "evidenceKeys": [evidence_key] if evidence_key else [],
            "status": "queued" if lane != "needs-human" else "blocked",
            "queuedAt": timestamp,
            "updatedAt": timestamp,
            "url": url,
            "title": title,
            "headSha": head_sha,
            "author": author,
            "history": [{"at": timestamp, "action": "enqueue", "reason": reason, "evidenceKey": evidence_key}],
        }
        state["items"][key] = item

    # Always refresh metadata when supplied.
    for field, value in {
        "issue": int(issue) if issue else None,
        "branch": branch,
        "url": url,
        "title": title,
        "headSha": head_sha,
        "author": author,
    }.items():
        if value not in (None, ""):
            item[field] = value

    save_state(state)
    return item


def queued_items(lane: str | None = None, include_blocked: bool = False) -> list[dict[str, Any]]:
    items = list(load_state().get("items", {}).values())
    allowed_statuses = {"queued"}
    if include_blocked:
        allowed_statuses.add("blocked")
    selected = [item for item in items if item.get("status") in allowed_statuses]
    if lane:
        canonical_lane = normalize_lane(lane)
        selected = [item for item in selected if normalize_lane(item.get("lane", "")) == canonical_lane]
    selected.sort(key=lambda item: (item.get("queuedAt") or "", item.get("repo") or "", item.get("pr") or 0))
    return selected


def mark(repo: str, pr: int, status: str, note: str | None = None) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    state = load_state()
    key = item_id(repo, pr)
    if key not in state.get("items", {}):
        raise KeyError(key)
    timestamp = now_iso()
    item = state["items"][key]
    item["status"] = status
    item["updatedAt"] = timestamp
    if note:
        item["lastNote"] = note
    item.setdefault("history", []).append({"at": timestamp, "action": "mark", "status": status, "note": note})
    save_state(state)
    return item


def print_jsonl(items: list[dict[str, Any]]) -> None:
    for item in items:
        print(json.dumps(item, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the Saffron PR review-fix queue")
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
        item = enqueue(
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
        items = list(load_state().get("items", {}).values())
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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
