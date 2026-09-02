#!/usr/bin/env python3
"""Select and record the oldest repository audits.

Dispatch owns the live repository inventory. Audit recency is Saffron workflow
state, bootstrapped from GitHub's audit-labelled issues on first use so a fresh
state file does not reset the rotation.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_FILE = ROOT / ".state" / "audit-rotation.json"
DEFAULT_DISPATCH_URL = "http://dispatch.llm:3000"
DEFAULT_GH = "/home/node/.local/bin/gh"
DEFAULT_LEASE_SECONDS = 5400
UTC = dt.timezone.utc


class SelectorError(RuntimeError):
    """Raised when the selector cannot establish a trustworthy selection."""


def now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def timestamp_or_none(value: object) -> str | None:
    parsed = parse_timestamp(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def dispatch_url() -> str:
    return os.environ.get("DISPATCH_URL", DEFAULT_DISPATCH_URL).rstrip("/")


def dispatch_token() -> str:
    return os.environ.get("DISPATCH_AGENT_TOKEN", "")


def fetch_tracked_repos() -> list[str]:
    """Fetch enabled repos from Dispatch; never fall back to a static list."""
    headers = {"Accept": "application/json"}
    token = dispatch_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        f"{dispatch_url()}/api/automation/repos/tracked",
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SelectorError(f"Dispatch tracked-repo lookup failed: {exc}") from exc

    if not isinstance(payload, list):
        raise SelectorError("Dispatch tracked-repo response was not a list")

    repos = {
        item.get("fullName")
        for item in payload
        if isinstance(item, dict)
        and item.get("enabled") is not False
        and isinstance(item.get("fullName"), str)
        and "/" in item["fullName"]
    }
    if not repos:
        raise SelectorError("Dispatch returned no enabled tracked repositories")
    return sorted(repos)


def gh_binary() -> str:
    configured = os.environ.get("GH")
    if configured:
        return configured
    discovered = shutil.which("gh")
    return discovered or DEFAULT_GH


def gh_issue_list(repo: str) -> list[dict[str, Any]]:
    command = [
        gh_binary(),
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--label",
        "audit",
        "--limit",
        "1000",
        "--json",
        "number,title,createdAt,labels",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SelectorError(f"GitHub audit lookup failed for {repo}: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip().splitlines()
        raise SelectorError(f"GitHub audit lookup failed for {repo}: {detail[-1] if detail else 'unknown error'}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SelectorError(f"GitHub audit lookup returned invalid JSON for {repo}") from exc
    if not isinstance(payload, list):
        raise SelectorError(f"GitHub audit lookup returned a non-list for {repo}")
    return [item for item in payload if isinstance(item, dict)]


def latest_audit_issue_at(repo: str) -> str | None:
    """Return the latest audit issue timestamp, or no timestamp if none exist.

    Closed issues are intentionally included: an audit finding can be resolved
    without making the repository newly audited.
    """
    timestamps = [
        parsed
        for issue in gh_issue_list(repo)
        if is_audit_finding(issue)
        and (parsed := parse_timestamp(issue.get("createdAt"))) is not None
    ]
    if not timestamps:
        return None
    return max(timestamps).isoformat().replace("+00:00", "Z")


def is_audit_finding(issue: dict[str, Any]) -> bool:
    """Distinguish direct audit findings from historical umbrella issues."""
    title = str(issue.get("title") or "").lower()
    labels = {
        label.get("name")
        for label in issue.get("labels", [])
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    }
    return "audit" in labels and not (
        "weekly tech debt audit:" in title
        or "tech debt audit:" in title
        or "umbrella" in labels
    )


def empty_entry() -> dict[str, Any]:
    return {
        "lastSuccessfulAuditAt": None,
        "lastAttemptAt": None,
        "lastStatus": None,
        "lastError": None,
        "bootstrapSource": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "repos": {}}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectorError(f"Could not read audit state: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("repos"), dict):
        raise SelectorError("Audit state must contain a repos object")
    payload["version"] = 1
    return payload


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def state_lock(path: Path):
    """Serialize selector/record operations across overlapping cron runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def lease_is_active(entry: dict[str, Any], now: dt.datetime) -> bool:
    lease_until = parse_timestamp(entry.get("leaseUntil"))
    return lease_until is not None and lease_until > now


def prepare_state(path: Path, repos: list[str]) -> dict[str, Any]:
    state = load_state(path)
    entries = state["repos"]
    changed = False

    # Only query GitHub for repositories that have not been seen by this state
    # file. Successful no-finding audits are recorded explicitly on completion.
    for repo in repos:
        raw_entry = entries.get(repo)
        if isinstance(raw_entry, dict) and "lastSuccessfulAuditAt" in raw_entry:
            continue

        entry = empty_entry()
        entry["lastSuccessfulAuditAt"] = latest_audit_issue_at(repo)
        entry["bootstrapSource"] = "github" if entry["lastSuccessfulAuditAt"] else "none"
        entries[repo] = entry
        changed = True

    # Drop repos removed from Dispatch so stale inventory cannot affect ties if
    # a repository is later re-added under a different lifecycle.
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
    now = dt.datetime.now(UTC)

    def sort_key(repo: str) -> tuple[int, dt.datetime, str]:
        timestamp = parse_timestamp(entries[repo].get("lastSuccessfulAuditAt"))
        status = entries[repo].get("lastStatus")
        status_rank = 0 if status in {"failed", "partial", "skipped"} else 1 if status is None else 2
        return (status_rank, timestamp or dt.datetime.min.replace(tzinfo=UTC), repo)

    available = [repo for repo in repos if not lease_is_active(entries[repo], now)]
    if len(available) < count:
        raise SelectorError(f"only {len(available)} repositories are available; another audit run may be active")
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
                "lastSuccessfulAuditAt": timestamp_or_none(entries[repo].get("lastSuccessfulAuditAt")),
                "lastAttemptAt": timestamp_or_none(entries[repo].get("lastAttemptAt")),
                "lastStatus": entries[repo].get("lastStatus"),
                "leaseUntil": timestamp_or_none(entries[repo].get("leaseUntil")),
                "leaseRunAt": timestamp_or_none(entries[repo].get("leaseRunAt")),
            }
            for repo in selected
        ],
    }


def select_repos(path: Path, count: int, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any]:
    with state_lock(path):
        return _select_repos(path, count, lease_seconds)


def _record_audit(
    path: Path,
    repo: str,
    status: str,
    error: str | None,
    completed_at: str | None,
    lease_run_at: str | None,
) -> dict[str, Any]:
    state = load_state(path)
    entries = state["repos"]
    if repo not in entries:
        raise SelectorError(f"repository is not in the current tracked-repo state: {repo}")
    entry = entries[repo]
    current_lease = timestamp_or_none(entry.get("leaseRunAt"))
    if lease_run_at and current_lease and timestamp_or_none(lease_run_at) != current_lease:
        raise SelectorError(f"audit lease is no longer owned by this run: {repo}")
    recorded_at = timestamp_or_none(completed_at) or now_iso()
    entry["lastAttemptAt"] = recorded_at
    entry.pop("leaseUntil", None)
    entry.pop("leaseRunAt", None)
    entry["lastStatus"] = status
    entry["lastError"] = error if status != "completed" else None
    if status == "completed":
        previous = parse_timestamp(entry.get("lastSuccessfulAuditAt"))
        current = parse_timestamp(recorded_at)
        if previous is None or (current is not None and current >= previous):
            entry["lastSuccessfulAuditAt"] = recorded_at
        entry["bootstrapSource"] = "workflow"
    save_state(path, state)
    return {"repo": repo, "status": status, "recordedAt": recorded_at}


def record_audit(
    path: Path,
    repo: str,
    status: str,
    error: str | None,
    completed_at: str | None,
    lease_run_at: str | None = None,
) -> dict[str, Any]:
    with state_lock(path):
        return _record_audit(path, repo, status, error, completed_at, lease_run_at)


def release_lease(path: Path, repo: str, lease_run_at: str | None = None) -> dict[str, Any]:
    """Release a lease when selection was made for a check but no audit ran."""
    with state_lock(path):
        state = load_state(path)
        entry = state["repos"].get(repo)
        if not isinstance(entry, dict):
            raise SelectorError(f"repository is not in the current tracked-repo state: {repo}")
        current_lease = timestamp_or_none(entry.get("leaseRunAt"))
        if lease_run_at and current_lease and timestamp_or_none(lease_run_at) != current_lease:
            raise SelectorError(f"audit lease is no longer owned by this run: {repo}")
        entry.pop("leaseUntil", None)
        entry.pop("leaseRunAt", None)
        save_state(path, state)
        return {"repo": repo, "released": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select and record rotating repository audits")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser("select", help="Select the oldest repositories")
    select.add_argument("--count", type=int, default=3)
    select.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)

    record = subparsers.add_parser("record", help="Record one audit result")
    record.add_argument("--repo", required=True)
    record.add_argument("--status", choices=("completed", "partial", "failed", "skipped"), required=True)
    record.add_argument("--error")
    record.add_argument("--completed-at")
    record.add_argument("--lease-run-at")

    release = subparsers.add_parser("release", help="Release an unused selection lease")
    release.add_argument("--repo", required=True)
    release.add_argument("--lease-run-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "select":
            if args.count < 1:
                raise SelectorError("--count must be positive")
            if args.lease_seconds < 1:
                raise SelectorError("--lease-seconds must be positive")
            output = select_repos(args.state_file, args.count, args.lease_seconds)
        elif args.command == "record":
            output = record_audit(
                args.state_file,
                args.repo,
                args.status,
                args.error,
                args.completed_at,
                args.lease_run_at,
            )
        else:
            output = release_lease(args.state_file, args.repo, args.lease_run_at)
    except SelectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
