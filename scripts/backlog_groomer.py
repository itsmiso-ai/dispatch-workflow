#!/usr/bin/env python3
"""Collect backlog grooming candidates for Saffron heartbeat.

Heartbeat owns cadence and reporting. This wrapper owns the deterministic
handoff boundary: no overlap, bounded candidate count, a JSON request path, and
a separate Dispatch run record. Saffron or a Saffron sub-agent owns the
intelligence work and applies results through Dispatch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("SAFFRON_STATE_DIR", ROOT / ".state"))
LOCK_FILE = STATE_DIR / "backlog_groomer.lock"
REQUEST_DIR = STATE_DIR / "backlog_grooming_requests"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def touched_urls(text: str) -> list[str]:
    urls = re.findall(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/\d+", text)
    return sorted(dict.fromkeys(urls))


def report_to_dispatch(started_at: str, finished_at: str, status: str, summary: str, output: str) -> None:
    cmd = [
        "python3",
        str(ROOT / "scripts/dispatch_reporter.py"),
        "--started-at",
        started_at,
        "--finished-at",
        finished_at,
        "--status",
        status,
        "--summary",
        summary,
        "--run-type",
        "backlog-candidates",
    ]
    urls = touched_urls(output)
    if urls:
        cmd.append("--touched")
        cmd.extend(urls)
    subprocess.run(cmd, cwd=ROOT, env=os.environ.copy())


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect backlog candidates for Saffron-owned grooming")
    parser.add_argument("--max", type=int, default=3, help="Maximum backlog issues to process")
    parser.add_argument("--force", action="store_true", help="Compatibility flag; candidate collection is always current")
    parser.add_argument("--include-no-status", action="store_true", help="Also include no-status issues")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)

    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[*] Backlog groomer is already running; skipping this heartbeat pass")
            return 0

        started_at = utc_now()
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = REQUEST_DIR / f"backlog-candidates-{stamp}.json"
        env = os.environ.copy()

        cmd = [
            "python3",
            str(ROOT / "scripts/project_groom.py"),
            "--no-sync",
            "--groom-backlog",
            "--groom-backlog-only",
            "--groom-backlog-max",
            str(args.max),
            "--groom-backlog-report",
            str(report_path),
        ]
        if args.force:
            cmd.append("--groom-backlog-force")
        if args.include_no_status:
            cmd.append("--groom-backlog-include-no-status")

        print(f"[*] Backlog candidate collector: max={args.max} request={report_path}")
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        output = (proc.stdout or "") + (proc.stderr or "")
        if output:
            print(output, end="" if output.endswith("\n") else "\n")

        candidate_count = 0
        try:
            payload = json.loads(report_path.read_text())
            candidate_count = int(payload.get("candidateCount") or 0)
        except Exception:
            candidate_count = 0
        print(f"BACKLOG_CANDIDATES count={candidate_count} request={report_path}")

        finished_at = utc_now()
        status = "ok" if proc.returncode == 0 else "warning"
        summary = f"Backlog candidate collector found {candidate_count} issue(s); request={report_path}"
        if proc.returncode != 0:
            summary = f"Backlog candidate collector warning; request={report_path}"
            print(f"[!] Backlog candidate collector exited {proc.returncode}", file=sys.stderr)
        report_to_dispatch(started_at, finished_at, status, summary, output)
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
