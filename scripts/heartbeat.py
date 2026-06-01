#!/usr/bin/env python3
"""Run Saffron heartbeat plumbing and report the result to Dispatch.

HEARTBEAT.md intentionally stays short; this script owns the command sequence.
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


def run_step(label: str, args: list[str]) -> tuple[int, str]:
    print(f"[*] {label}", file=sys.stderr)
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
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
    parser.add_argument("--skip-llm-grooming", action="store_true", help="Skip bounded LLM backlog enrichment")
    args = parser.parse_args()

    started_at = utc_now()
    status = "ok"
    warnings = 0
    combined_output: list[str] = []

    steps = [
        (
            "GitHub follow-up watcher",
            ["python3", str(ROOT / "scripts/github_followup_watcher.py")],
            True,
        ),
        (
            "Dispatch sync",
            ["python3", str(ROOT / "scripts/project_backlog_sync.py")],
            False,
        ),
        (
            "Deterministic Dispatch grooming",
            ["python3", str(ROOT / "scripts/project_groom.py"), "--no-sync"],
            True,
        ),
    ]
    if not args.skip_llm_grooming:
        steps.append(
            (
                "Bounded self-hosted backlog grooming",
                ["python3", str(ROOT / "scripts/backlog_groomer.py"), "--max", "3"],
                False,
            )
        )

    for label, command, fatal in steps:
        code, output = run_step(label, command)
        combined_output.append(output)
        if code != 0:
            if fatal:
                status = "error"
            else:
                warnings += 1
                if status == "ok":
                    status = "warning"

    finished_at = utc_now()
    summary = "Heartbeat ran follow-up watcher, sync, deterministic grooming, and bounded self-hosted backlog grooming."
    if args.skip_llm_grooming:
        summary = "Heartbeat ran follow-up watcher, sync, and deterministic grooming."
    if warnings:
        summary = f"{summary} Non-fatal warning steps: {warnings}."

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
