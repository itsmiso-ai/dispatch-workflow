#!/usr/bin/env python3
"""Create an audit issue without replaying an already-created finding.

GitHub issue creation is not idempotent. This wrapper serializes creates,
checks GitHub for an exact-title match in both states, and keeps a per-run
ledger so a timed-out request cannot be submitted twice in the same audit.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / ".state" / "audit-create-ledger.json"
DEFAULT_GH = "/home/node/.local/bin/gh"
UTC = dt.timezone.utc
TITLE_PRIORITY_RE = re.compile(r"^\s*\[P[0-3]\]\s*", re.IGNORECASE)


class IssueCreateError(RuntimeError):
    """Raised when a create cannot be proven safe to retry."""


def now_iso() -> str:
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def gh_binary() -> str:
    return os.environ.get("GH") or shutil.which("gh") or DEFAULT_GH


def normalize_title(title: str) -> str:
    # Priority changes do not make an otherwise identical finding distinct.
    return " ".join(TITLE_PRIORITY_RE.sub("", title).casefold().split())


def fingerprint(repo: str, title: str) -> str:
    value = f"{repo.casefold()}\0{normalize_title(title)}".encode()
    return hashlib.sha256(value).hexdigest()


def read_body(args: argparse.Namespace) -> str:
    if bool(args.body) == bool(args.body_file):
        raise IssueCreateError("provide exactly one of --body or --body-file")
    try:
        return args.body if args.body is not None else args.body_file.read_text()
    except OSError as exc:
        raise IssueCreateError(f"could not read issue body: {exc}") from exc


def validate_issue(title: str, body: str) -> None:
    if not TITLE_PRIORITY_RE.match(title):
        raise IssueCreateError("audit issue title must start with [P0], [P1], [P2], or [P3]")
    problem = body.find("**Problem:**")
    if problem < 0:
        raise IssueCreateError("audit issue body is missing **Problem:**")
    ask = body.find("Ask:")
    expected = body.find("Expected files:")
    if ask < 0 or ask > problem:
        raise IssueCreateError("Ask: must appear before **Problem:**")
    if expected < 0 or expected > problem:
        raise IssueCreateError("Expected files: must appear before **Problem:**")
    for section in ("**Evidence:**", "**Acceptance:**"):
        if body.find(section, problem) < 0:
            raise IssueCreateError(f"audit issue body is missing {section}")


def gh_issue_list(repo: str, title: str) -> list[dict[str, Any]]:
    command = [
        gh_binary(),
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        "1000",
        "--json",
        "number,title,state,url",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        raise IssueCreateError(f"GitHub duplicate check failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip().splitlines()
        raise IssueCreateError(
            f"GitHub duplicate check failed: {detail[-1] if detail else 'unknown error'}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise IssueCreateError("GitHub duplicate check returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise IssueCreateError("GitHub duplicate check returned a non-list")
    wanted = normalize_title(title)
    return [
        item
        for item in payload
        if isinstance(item, dict)
        and isinstance(item.get("title"), str)
        and normalize_title(item["title"]) == wanted
    ]


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise IssueCreateError(f"could not read create ledger: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        raise IssueCreateError("create ledger must contain an entries object")
    payload["version"] = 1
    return payload


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(ledger, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def ledger_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def issue_result(existing: dict[str, Any], *, recovered: bool = False) -> dict[str, Any]:
    return {
        "created": False,
        "duplicate": True,
        "recovered": recovered,
        "issue": {
            key: existing.get(key)
            for key in ("number", "title", "state", "url")
        },
    }


def remember(ledger: dict[str, Any], key: str, repo: str, title: str, result: dict[str, Any], run_id: str) -> None:
    ledger["entries"][key] = {
        "repo": repo,
        "title": title,
        "runId": run_id,
        "recordedAt": now_iso(),
        "result": result,
    }


def create_issue(args: argparse.Namespace, body: str) -> dict[str, Any]:
    key = fingerprint(args.repo, args.title)
    run_id = args.run_id or os.environ.get("AUDIT_RUN_ID") or "unspecified"
    with ledger_lock(args.ledger):
        ledger = load_ledger(args.ledger)
        prior = ledger["entries"].get(key)
        if prior and prior.get("runId") == run_id:
            result = dict(prior.get("result") or {})
            result.update({"created": False, "duplicate": True, "ledger": True})
            return result

        existing = gh_issue_list(args.repo, args.title)
        open_matches = [item for item in existing if str(item.get("state", "")).upper() == "OPEN"]
        if open_matches or (existing and not args.allow_regression):
            result = issue_result(open_matches[0] if open_matches else existing[0])
            remember(ledger, key, args.repo, args.title, result, run_id)
            save_ledger(args.ledger, ledger)
            return result

        command = [gh_binary(), "issue", "create", "--repo", args.repo, "--title", args.title, "--body-file", str(args.body_file)]
        for label in args.labels:
            command.extend(("--label", label))
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            result = None
            detail = str(exc)
        else:
            detail = (result.stderr or result.stdout or "unknown error").strip()

        # A request may have succeeded at GitHub after the client timed out.
        # Re-check before exposing an error or allowing a caller to retry.
        if result is None or result.returncode != 0:
            recovered = gh_issue_list(args.repo, args.title)
            if recovered:
                outcome = issue_result(recovered[0], recovered=True)
                remember(ledger, key, args.repo, args.title, outcome, run_id)
                save_ledger(args.ledger, ledger)
                return outcome
            raise IssueCreateError(f"GitHub issue create failed: {detail}")

        url = next((line.strip() for line in result.stdout.splitlines() if "/issues/" in line), result.stdout.strip())
        outcome = {"created": True, "duplicate": False, "url": url, "title": args.title}
        remember(ledger, key, args.repo, args.title, outcome, run_id)
        save_ledger(args.ledger, ledger)
        return outcome


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely create a deduplicated audit issue")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--title", required=True)
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="Issue body text")
    body.add_argument("--body-file", type=Path)
    parser.add_argument("--label", dest="labels", action="append", default=[])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--run-id")
    parser.add_argument("--allow-regression", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        body = read_body(args)
        validate_issue(args.title, body)
        # Keep the wrapper's validation separate from the file used by gh so a
        # caller can safely pass --body without relying on shell quoting.
        if args.body is not None:
            fd, temporary = tempfile.mkstemp(prefix="audit-issue-", suffix=".md")
            os.close(fd)
            args.body_file = Path(temporary)
            args.body_file.write_text(body)
            remove_body_file = True
        else:
            remove_body_file = False
        try:
            output = create_issue(args, body)
        finally:
            if remove_body_file:
                args.body_file.unlink(missing_ok=True)
    except IssueCreateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
