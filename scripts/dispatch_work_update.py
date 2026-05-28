#!/usr/bin/env python3
"""Dispatch work update helper — checkpoint + issue status.

Usage:
    dispatch_work_update.py checkpoint --agent saffron-normal --checkpoint PR_OPENED --summary "Opened PR #72"
    dispatch_work_update.py status --agent saffron-normal --issue-id <id> --repo misospace/pr-reviewer-action --issue-number 72 --status in-review
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def dispatch_url() -> str:
    return os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000").rstrip("/")


def dispatch_token() -> str:
    token = os.environ.get("DISPATCH_AGENT_TOKEN", "")
    if not token:
        print("ERROR: DISPATCH_AGENT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return token


VALID_CHECKPOINTS = {
    "CLAIMED",
    "REPO_PREPARED",
    "BRANCH_CREATED",
    "CHANGES_MADE",
    "TESTS_RUNNING",
    "PR_OPENED",
    "DONE",
    "BLOCKED",
}

VALID_STATUSES = {
    "backlog",
    "ready",
    "in-progress",
    "in-review",
    "done",
}


def cmd_checkpoint(args: argparse.Namespace) -> None:
    if args.checkpoint not in VALID_CHECKPOINTS:
        print(
            f"ERROR: invalid checkpoint {args.checkpoint!r}. Valid: {VALID_CHECKPOINTS}",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = {
        "agentName": args.agent,
        "checkpoint": args.checkpoint,
        "summary": args.summary,
    }

    url = f"{dispatch_url()}/api/agent-work/checkpoint"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {dispatch_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"OK {resp.status} — {url}")
            print(json.dumps(result, indent=2))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else "(no body)"
        print(f"FAIL {e.code} — {url}", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    if args.status not in VALID_STATUSES:
        print(
            f"ERROR: invalid status {args.status!r}. Valid: {VALID_STATUSES}",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = {
        "issueId": args.issue_id,
        "repoFullName": args.repo,
        "issueNumber": args.issue_number,
        "status": args.status,
        "agentName": args.agent,
        "actor": args.agent,
    }

    url = f"{dispatch_url()}/api/issues/status"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {dispatch_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"OK {resp.status} — {url}")
            print(json.dumps(result, indent=2))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else "(no body)"
        print(f"FAIL {e.code} — {url}", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch work update helper")
    sub = parser.add_subparsers(dest="command")

    cp = sub.add_parser("checkpoint", help="Update active-work checkpoint")
    cp.add_argument("--agent", required=True, help="Agent name, e.g. saffron-normal")
    cp.add_argument(
        "--checkpoint",
        required=True,
        help=f"One of: {', '.join(sorted(VALID_CHECKPOINTS))}",
    )
    cp.add_argument("--summary", required=True, help="Human-readable summary")

    st = sub.add_parser("status", help="Update issue status")
    st.add_argument("--agent", required=True, help="Agent name, e.g. saffron-normal")
    st.add_argument("--issue-id", required=True, help="Dispatch issue ID")
    st.add_argument("--repo", required=True, help="e.g. misospace/pr-reviewer-action")
    st.add_argument("--issue-number", type=int, required=True, help="GitHub issue number")
    st.add_argument(
        "--status",
        required=True,
        help=f"one of: {', '.join(sorted(VALID_STATUSES))}",
    )

    args = parser.parse_args()

    if args.command == "checkpoint":
        cmd_checkpoint(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())