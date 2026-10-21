#!/usr/bin/env python3
"""Select and record rotating repository simplify passes.

Mirrors select_audit_repos.py machinery (Dispatch inventory, flock'd state,
lease acquire/release) but tracks simplify recency: per-repo
``lastSimplifiedAt`` plus the exact HEAD each successful pass observed
(``lastSimplifiedSha``) as metadata for churn maps, and coarse
``recentlyExamined`` coverage hints. These are informational: a pass may
read and edit anywhere in the repository. Simplify state lives in its own
file and never touches audit state.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from select_audit_repos import (  # noqa: E402
    DEFAULT_LEASE_SECONDS,
    ROOT,
    SelectorError,
    fetch_tracked_repos,
    lease_is_active,
    load_state,
    now_iso,
    parse_timestamp,
    save_state,
    state_lock,
    timestamp_or_none,
)

DEFAULT_STATE_FILE = ROOT / ".state" / "simplify-rotation.json"
HISTORY_CAP = 25


def empty_entry() -> dict[str, Any]:
    return {
        "lastSimplifiedAt": None,
        "lastSimplifiedSha": None,
        "recentlyExamined": None,
        "lastAttemptAt": None,
        "lastStatus": None,
        "lastError": None,
        "bootstrapSource": "none",
    }


def prepare_state(path: Path, repos: list[str]) -> dict[str, Any]:
    state = load_state(path)
    if "repos" not in state:
        state["repos"] = {}
    entries = state["repos"]
    changed = False
    for repo in repos:
        raw = entries.get(repo)
        if isinstance(raw, dict) and "lastSimplifiedSha" in raw:
            continue
        entries[repo] = empty_entry()
        changed = True
    for repo in list(entries):
        if repo not in repos:
            del entries[repo]
            changed = True
    if changed:
        save_state(path, state)
    return state


def _select_repos(path: Path, count: int, lease_seconds: int) -> dict[str, Any]:
    repos = fetch_tracked_repos()
    state = prepare_state(path, repos)
    entries = state["repos"]
    now = dt.datetime.now(dt.timezone.utc)

    def sort_key(repo: str) -> tuple[int, dt.datetime, str]:
        entry = entries[repo]
        status = entry.get("lastStatus")
        rank = 0 if status in {"failed", "partial", "skipped"} else 1 if status is None else 2
        ts = parse_timestamp(entry.get("lastSimplifiedAt")) or dt.datetime.min.replace(
            tzinfo=dt.timezone.utc
        )
        return (rank, ts, repo)

    available = [repo for repo in repos if not lease_is_active(entries[repo], now)]
    if len(available) < count:
        raise SelectorError(
            f"only {len(available)} repositories are available; another simplify run may be active"
        )
    selected = sorted(available, key=sort_key)[:count]
    lease_until = now + dt.timedelta(seconds=lease_seconds)
    for repo in selected:
        entries[repo]["leaseUntil"] = lease_until.isoformat().replace("+00:00", "Z")
        entries[repo]["leaseRunAt"] = now.isoformat().replace("+00:00", "Z")
    save_state(path, state)
    return {
        "generatedAt": now_iso(),
        "trackedRepoCount": len(repos),
        "stateFile": str(path),
        "repos": [
            {
                "fullName": repo,
                "lastSimplifiedAt": timestamp_or_none(entries[repo].get("lastSimplifiedAt")),
                "lastSimplifiedSha": entries[repo].get("lastSimplifiedSha"),
                "recentlyExamined": entries[repo].get("recentlyExamined"),
                "lastStatus": entries[repo].get("lastStatus"),
                "leaseRunAt": timestamp_or_none(entries[repo].get("leaseRunAt")),
                "leaseUntil": timestamp_or_none(entries[repo].get("leaseUntil")),
            }
            for repo in selected
        ],
    }


def _append_history(entry: dict[str, Any], record: dict[str, Any]) -> None:
    history = entry.get("history")
    if not isinstance(history, list):
        history = []
    history.append(record)
    entry["history"] = history[-HISTORY_CAP:]


def _record(
    path: Path,
    repo: str,
    status: str,
    error: str | None,
    completed_at: str | None,
    lease_run_at: str | None,
    from_sha: str | None,
    to_sha: str | None,
    model: str | None,
    lane: str | None,
    outcome: str | None,
    pr_url: str | None,
    examined: str | None,
) -> dict[str, Any]:
    if status == "completed" and not to_sha:
        raise SelectorError("completed simplify runs must record the observed HEAD: --to-sha is required")
    if outcome == "pr" and not pr_url:
        raise SelectorError("outcome 'pr' requires --pr-url")
    state = load_state(path)
    entries = state["repos"]
    if repo not in entries:
        raise SelectorError(f"repository is not in the current tracked-repo state: {repo}")
    entry = entries[repo]
    current_lease = timestamp_or_none(entry.get("leaseRunAt"))
    if lease_run_at and current_lease and timestamp_or_none(lease_run_at) != current_lease:
        raise SelectorError(f"simplify lease is no longer owned by this run: {repo}")

    recorded_at = timestamp_or_none(completed_at) or now_iso()
    entry["lastAttemptAt"] = recorded_at
    entry["lastStatus"] = status
    entry["lastError"] = error if status != "completed" else None
    entry.pop("leaseUntil", None)
    entry.pop("leaseRunAt", None)

    examined_areas = [a.strip() for a in (examined or "").split(",") if a.strip()] or None
    if examined_areas:
        # Coverage hints are informational; any pass that actually read areas
        # (even one that later failed) refreshes them.
        entry["recentlyExamined"] = examined_areas
    if status == "completed":
        entry["lastSimplifiedAt"] = recorded_at
        if to_sha:
            entry["lastSimplifiedSha"] = to_sha

    _append_history(
        entry,
        {
            "recordedAt": recorded_at,
            "status": status,
            "outcome": outcome,
            "fromSha": from_sha,
            "toSha": to_sha,
            "model": model,
            "lane": lane,
            "prUrl": pr_url,
            "examined": examined_areas,
            "error": error,
        },
    )
    save_state(path, state)
    return {
        "repo": repo,
        "status": status,
        "recordedAt": recorded_at,
        "lastSimplifiedSha": entry.get("lastSimplifiedSha"),
    }


def _release(path: Path, repo: str, lease_run_at: str | None) -> dict[str, Any]:
    state = load_state(path)
    entry = state["repos"].get(repo)
    if not isinstance(entry, dict):
        raise SelectorError(f"repository is not in the current tracked-repo state: {repo}")
    current_lease = timestamp_or_none(entry.get("leaseRunAt"))
    if lease_run_at and current_lease and timestamp_or_none(lease_run_at) != current_lease:
        raise SelectorError(f"simplify lease is no longer owned by this run: {repo}")
    entry.pop("leaseUntil", None)
    entry.pop("leaseRunAt", None)
    save_state(path, state)
    return {"repo": repo, "released": True}


def _show(path: Path) -> dict[str, Any]:
    state = load_state(path)
    entries = state["repos"]
    out: dict[str, Any] = {}
    for repo, entry in sorted(entries.items()):
        out[repo] = {
            "lastSimplifiedAt": timestamp_or_none(entry.get("lastSimplifiedAt")),
            "lastSimplifiedSha": entry.get("lastSimplifiedSha"),
            "recentlyExamined": entry.get("recentlyExamined"),
            "lastStatus": entry.get("lastStatus"),
            "lastError": entry.get("lastError"),
            "recentHistory": (entry.get("history") or [])[-3:],
        }
    return {"generatedAt": now_iso(), "stateFile": str(path), "repos": out}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select and record rotating simplify passes")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser("select", help="Select the repo gone longest without simplify")
    select.add_argument("--count", type=int, default=1)
    select.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)

    record = subparsers.add_parser("record", help="Record one simplify result")
    record.add_argument("--repo", required=True)
    record.add_argument("--status", choices=("completed", "partial", "failed", "skipped"), required=True)
    record.add_argument("--error")
    record.add_argument("--completed-at")
    record.add_argument("--lease-run-at")
    record.add_argument("--from-sha")
    record.add_argument("--to-sha")
    record.add_argument("--model")
    record.add_argument("--lane")
    record.add_argument("--outcome", choices=("clean", "pr"))
    record.add_argument("--pr-url")
    record.add_argument("--examined", help="comma-separated coarse areas actually examined")

    release = subparsers.add_parser("release", help="Release an unused selection lease")
    release.add_argument("--repo", required=True)
    release.add_argument("--lease-run-at")

    subparsers.add_parser("show", help="Dump per-repo simplify state and recent history")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "select":
            if args.count < 1:
                raise SelectorError("--count must be positive")
            if args.lease_seconds < 1:
                raise SelectorError("--lease-seconds must be positive")
            output = _select_repos(args.state_file, args.count, args.lease_seconds)
        elif args.command == "record":
            output = _record(
                args.state_file,
                args.repo,
                args.status,
                args.error,
                args.completed_at,
                args.lease_run_at,
                args.from_sha,
                args.to_sha,
                args.model,
                args.lane,
                args.outcome,
                args.pr_url,
            args.examined,
            )
        elif args.command == "release":
            output = _release(args.state_file, args.repo, args.lease_run_at)
        else:
            output = _show(args.state_file)
    except SelectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
