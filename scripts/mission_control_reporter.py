#!/usr/bin/env python3
"""Best-effort reporter for Mission Control agent run events.

Usage:
    python3 mission_control_reporter.py --started-at ISO8601 [--finished-at ISO8601]
                                         [--status ok|warning|error]
                                         [--summary TEXT]
                                         [--touched URL [URL ...]]
                                         [--run-type RUN_TYPE]

Exit code: 0 even on failure (best-effort reporting).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def report_to_mission_control(
    started_at: str,
    finished_at: Optional[str],
    status: str,
    summary: Optional[str],
    touched_urls: list[str],
    run_type: str = "heartbeat",
) -> bool:
    """POST an agent-run event to Mission Control. Returns True on success."""
    url = os.environ.get("MISSION_CONTROL_URL")
    token = os.environ.get("MISSION_CONTROL_AGENT_TOKEN")

    if not url:
        log("[mission-control-reporter] MISSION_CONTROL_URL not set — skipping report")
        return False
    if not token:
        log("[mission-control-reporter] MISSION_CONTROL_AGENT_TOKEN not set — skipping report")
        return False

    payload = {
        "agentName": "saffron",
        "runType": run_type,
        "status": status,
        "startedAt": started_at,
    }
    if finished_at:
        payload["finishedAt"] = finished_at
    if summary:
        payload["summary"] = summary
    if touched_urls:
        payload["touchedIssueUrls"] = touched_urls

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/agent-runs",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200 or resp.status == 201:
                log(f"[mission-control-reporter] reported run status={status} to Mission Control")
                return True
            else:
                log(f"[mission-control-reporter] unexpected response {resp.status} — skipping")
                return False
    except urllib.error.URLError as e:
        log(f"[mission-control-reporter] could not reach Mission Control: {e.reason} — skipping")
        return False
    except Exception as e:
        log(f"[mission-control-reporter] unexpected error: {e} — skipping")
        return False


def parse_timestamp(value: Optional[str]) -> Optional[str]:
    """Normalise a timestamp to ISO8601 UTC. Returns None if unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Report heartbeat run to Mission Control")
    parser.add_argument("--started-at", required=True, help="Run start time (ISO8601)")
    parser.add_argument("--finished-at", help="Run finish time (ISO8601)")
    parser.add_argument(
        "--status",
        default="ok",
        choices={"ok", "warning", "error"},
        help="Run status (default: ok)",
    )
    parser.add_argument("--summary", help="Short human-readable summary")
    parser.add_argument("--touched", nargs="*", default=[], help="GitHub issue/PR URLs touched this run")
    parser.add_argument(
        "--run-type",
        default="heartbeat",
        help="Type of run event (default: heartbeat)",
    )
    args = parser.parse_args()

    started = parse_timestamp(args.started_at) or args.started_at
    finished = parse_timestamp(args.finished_at) if args.finished_at else None

    report_to_mission_control(
        started_at=started,
        finished_at=finished,
        status=args.status,
        summary=args.summary,
        touched_urls=args.touched,
        run_type=args.run_type,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
