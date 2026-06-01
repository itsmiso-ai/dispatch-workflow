#!/usr/bin/env python3
"""Bounded self-hosted backlog grooming step for Saffron heartbeat.

Heartbeat owns cadence and reporting. This wrapper owns the model-backed
grooming boundary: no overlap, bounded item count, a JSONL report path, and a
separate Dispatch run record.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("SAFFRON_STATE_DIR", ROOT / ".state"))
LOCK_FILE = STATE_DIR / "backlog_groomer.lock"
REPORT_DIR = STATE_DIR / "backlog_grooming_reports"
DEFAULT_MODEL = os.environ.get("BACKLOG_GROOMING_MODEL", "litellm/self-hosted")


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
        "backlog-grooming",
    ]
    urls = touched_urls(output)
    if urls:
        cmd.append("--touched")
        cmd.extend(urls)
    subprocess.run(cmd, cwd=ROOT, env=os.environ.copy())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded self-hosted backlog grooming")
    parser.add_argument("--max", type=int, default=3, help="Maximum backlog issues to process")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LiteLLM model for grooming")
    parser.add_argument("--dry-run", action="store_true", help="Do not apply Dispatch/GitHub updates")
    parser.add_argument("--force", action="store_true", help="Re-groom unchanged issues")
    parser.add_argument("--include-no-status", action="store_true", help="Also groom no-status issues")
    parser.add_argument("--no-comment", action="store_true", help="Do not add GitHub grooming comments")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[*] Backlog groomer is already running; skipping this heartbeat pass")
            return 0

        started_at = utc_now()
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = REPORT_DIR / f"backlog-grooming-{stamp}.jsonl"
        env = os.environ.copy()
        env["BACKLOG_GROOMING_MODEL"] = args.model

        cmd = [
            "python3",
            str(ROOT / "scripts/project_groom.py"),
            "--no-sync",
            "--groom-backlog",
            "--groom-backlog-use-llm",
            "--groom-backlog-only",
            "--groom-backlog-max",
            str(args.max),
            "--groom-backlog-report",
            str(report_path),
        ]
        if not args.dry_run:
            cmd.append("--groom-backlog-apply")
        if args.force:
            cmd.append("--groom-backlog-force")
        if args.include_no_status:
            cmd.append("--groom-backlog-include-no-status")
        if args.no_comment:
            cmd.append("--groom-backlog-no-comment")

        print(f"[*] Backlog groomer: max={args.max} model={args.model} report={report_path}")
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        output = (proc.stdout or "") + (proc.stderr or "")
        if output:
            print(output, end="" if output.endswith("\n") else "\n")

        finished_at = utc_now()
        status = "ok" if proc.returncode == 0 else "warning"
        summary = f"Backlog groomer processed up to {args.max} issue(s) with {args.model}; report={report_path}"
        if proc.returncode != 0:
            summary = f"Backlog groomer warning with {args.model}; report={report_path}"
            print(f"[!] Backlog groomer exited {proc.returncode}", file=sys.stderr)
        report_to_dispatch(started_at, finished_at, status, summary, output)
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
