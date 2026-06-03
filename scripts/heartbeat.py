#!/usr/bin/env python3
"""Run deterministic Saffron heartbeat plumbing and report the result to Dispatch.

HEARTBEAT.md owns the agent-intelligence work after this script returns.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_step(label: str, args: list[str], timeout: int) -> tuple[int, str]:
    print(f"[*] {label}", file=sys.stderr)
    try:
        proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        print(f"[!] {label} timed out after {timeout}s", file=sys.stderr)
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        return 124, output

    output = (proc.stdout or "") + (proc.stderr or "")
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    if proc.returncode != 0:
        print(f"[!] {label} exited {proc.returncode}", file=sys.stderr)
    return proc.returncode, output


def touched_urls(text: str) -> list[str]:
    urls = re.findall(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/\d+", text)
    return sorted(dict.fromkeys(urls))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Saffron heartbeat")
    args = parser.parse_args()

    started_at = utc_now()
    status = "ok"
    combined_output: list[str] = []

    steps = [
        (
            "GitHub follow-up watcher",
            ["python3", str(ROOT / "scripts/github_followup_watcher.py")],
            True,
            180,
        ),
        (
            "Dispatch sync",
            ["python3", str(ROOT / "scripts/project_backlog_sync.py")],
            False,
            120,
        ),
        (
            "Deterministic Dispatch grooming",
            ["python3", str(ROOT / "scripts/project_groom.py"), "--no-sync"],
            True,
            240,
        ),
    ]

    for label, command, fatal, timeout in steps:
        code, output = run_step(label, command, timeout)
        combined_output.append(output)
        if code != 0:
            if fatal:
                status = "error"
            else:
                if status == "ok":
                    status = "warning"

    finished_at = utc_now()
    summary = "Heartbeat ran follow-up watcher, sync, and deterministic Dispatch grooming."
    if status == "warning":
        summary = f"{summary} One or more non-fatal deterministic steps warned."

    report_cmd = [
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
    ]
    urls = touched_urls("\n".join(combined_output))
    if urls:
        report_cmd.append("--touched")
        report_cmd.extend(urls)

    subprocess.run(report_cmd, cwd=ROOT, env=os.environ.copy())
    return 1 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
