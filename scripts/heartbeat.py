#!/usr/bin/env python3
"""Run deterministic Saffron heartbeat plumbing and report the result to Dispatch.

HEARTBEAT.md owns the agent-intelligence work after this script returns.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

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


def dispatch_base_url() -> str:
    return os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000").rstrip("/")


def dispatch_token() -> str:
    return os.environ.get("DISPATCH_AGENT_TOKEN", "")


def dispatch_request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 120) -> Any:
    token = dispatch_token()
    data = None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"{dispatch_base_url()}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def run_dispatch_pr_followup_sync() -> tuple[int, str]:
    print("[*] Dispatch PR follow-up sync", file=sys.stderr)
    try:
        result = dispatch_request("/api/pr-followup/sync", method="POST", payload={}, timeout=120)
    except Exception as exc:
        output = f"Dispatch PR follow-up sync failed: {exc}\n"
        print(output, end="")
        return 1, output

    output = (
        "Dispatch PR follow-up sync: "
        f"repos={result.get('reposScanned', 0)} "
        f"prs={result.get('prsScanned', 0)} "
        f"enqueued={result.get('enqueued', 0)} "
        f"skipped={result.get('skipped', 0)}\n"
    )
    print(output, end="")
    return 0, output


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Saffron heartbeat")
    args = parser.parse_args()

    started_at = utc_now()
    status = "ok"
    combined_output: list[str] = []

    code, output = run_dispatch_pr_followup_sync()
    combined_output.append(output)
    if code != 0:
        status = "error"

    steps = [
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
    summary = "Heartbeat ran Dispatch PR follow-up sync, scheduled sync, and deterministic Dispatch grooming."
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
