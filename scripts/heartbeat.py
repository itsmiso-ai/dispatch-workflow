#!/usr/bin/env python3
"""Thin Dispatch-native heartbeat runner.

Calls ``POST /api/agents/{agentName}/heartbeat`` which handles:
- best-effort issue sync
- best-effort reconciliation (stale statuses, closed issues, lane cleanup)
- AgentRun recording

After the server-side heartbeat returns, this script probes all lanes for
work availability and surfaces the result for the Saffron agent to act on.

This script does NOT:
- run local grooming heuristics
- call GitHub Projects
- hardcode tracked repos
- mutate labels, lanes, or issue statuses
- run backlog candidate collection

All of that is owned by Dispatch server-side.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dispatch_base_url() -> str:
    return os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000").rstrip("/")


def dispatch_token() -> str:
    return os.environ.get("DISPATCH_AGENT_TOKEN", "")


def dispatch_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
) -> Any:
    token = dispatch_token()
    if not token:
        raise RuntimeError("DISPATCH_AGENT_TOKEN not set")

    data = None
    headers = {"Authorization": f"Bearer {token}"}
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


def run_server_heartbeat(agent_name: str) -> dict[str, Any]:
    """Call the Dispatch server-side heartbeat endpoint."""
    print(f"[*] Dispatch heartbeat for {agent_name}", file=sys.stderr)
    try:
        result = dispatch_request(
            f"/api/agents/{agent_name}/heartbeat",
            method="POST",
            payload={},
            timeout=180,
        )
    except Exception as exc:
        output = f"Heartbeat call to Dispatch failed: {exc}\n"
        print(output, end="")
        return {
            "status": "error",
            "errors": [str(exc)],
            "warnings": [],
            "summary": output.strip(),
            "touchedIssueUrls": [],
        }

    if not isinstance(result, dict):
        return {
            "status": "error",
            "errors": ["Unexpected heartbeat response format"],
            "warnings": [],
            "summary": "Unexpected heartbeat response format",
            "touchedIssueUrls": [],
        }

    status = result.get("status", "ok")
    warnings = result.get("warnings", [])
    errors = result.get("errors", [])
    summary = result.get("summary", "Heartbeat completed")
    touched = result.get("touchedIssueUrls", [])

    print(f"  status={status}")
    if warnings:
        print(f"  warnings: {len(warnings)}")
        for w in warnings[:5]:
            print(f"    - {w}")
    if errors:
        print(f"  errors: {len(errors)}")
        for e in errors[:5]:
            print(f"    - {e}")
    if touched:
        print(f"  touched: {len(touched)} issue(s)")

    return result


def probe_lane(lane: str) -> dict[str, Any] | None:
    """Run the read-only work probe for a lane and return its JSON output."""
    print(f"[*] Probing {lane} lane", file=sys.stderr)
    try:
        proc = subprocess.run(
            [
                "python3",
                str(SCRIPTS / "dispatch_work_probe.py"),
                "--lane",
                lane,
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            print(f"  [!] {lane} probe exited {proc.returncode}", file=sys.stderr)
            return None
        return json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        print(f"  [!] {lane} probe timed out", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"  [!] {lane} probe failed: {exc}", file=sys.stderr)
        return None


def toggle_worker_crons(lane_probes: dict[str, dict[str, Any] | None]) -> None:
    """Enable or disable worker crons based on probe results."""
    for lane, probe in lane_probes.items():
        if probe is None:
            print(f"  [!] {lane} probe unavailable; leaving cron unchanged", file=sys.stderr)
            continue

        if probe.get("action") == "stuck" or probe.get("needsAttention"):
            print(f"  [!] {lane} lane needs attention; leaving cron unchanged")
            continue

        flag = "--enable" if probe.get("hasWork") else "--disable"
        reason = probe.get("reason") or ("work available" if probe.get("hasWork") else "clear")
        cmd = [
            "python3",
            str(SCRIPTS / "dispatch_worker_cron.py"),
            "--lane",
            lane,
            flag,
            "--reason",
            reason,
            "--apply",
            "--json",
        ]
        try:
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                print(f"  [!] {lane} cron toggle failed: {(proc.stderr or proc.stdout).strip()[:200]}", file=sys.stderr)
            else:
                print(f"  {lane} cron: {flag.replace('--', '')}")
        except Exception as exc:
            print(f"  [!] {lane} cron toggle error: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Dispatch-native Saffron heartbeat")
    parser.add_argument("--agent-name", default=os.environ.get("DISPATCH_AGENT_NAME", "saffron"), help="Agent name for heartbeat")
    parser.add_argument("--no-probe", action="store_true", help="Skip lane probing and cron toggle")
    parser.add_argument("--no-cron-toggle", action="store_true", help="Probe but do not toggle crons")
    args = parser.parse_args()

    started_at = utc_now()

    # 1. Server-side heartbeat (sync + reconcile + AgentRun recording)
    hb_result = run_server_heartbeat(args.agent_name)

    # 2. Probe lanes for work availability
    lane_probes: dict[str, dict[str, Any] | None] = {}
    if not args.no_probe:
        for lane in ("local", "cloud", "frontier"):
            lane_probes[lane] = probe_lane(lane)

    # 3. Toggle worker crons based on probe verdicts
    if not args.no_probe and not args.no_cron_toggle:
        toggle_worker_crons(lane_probes)

    # 4. Surface grooming work if Dispatch has untriaged issues
    try:
        groom_task = dispatch_request(
            f"/api/agents/{args.agent_name}/next-task?mode=groom",
            timeout=15,
        )
        if isinstance(groom_task, dict) and groom_task.get("type") == "groom":
            issue = groom_task.get("issue", {})
            print(f"\n[*] Grooming work available: {issue.get('repoFullName', '?')} #{issue.get('number', '?')}: {issue.get('title', '')[:80]}")
            print(f"    Groom via: POST /api/issues/groom with action=promote_to_ready|escalate|mark_needs_info|mark_blocked|mark_not_ready")
    except Exception as exc:
        print(f"  [!] Grooming task check failed (non-fatal): {exc}", file=sys.stderr)

    # 5. Report
    finished_at = utc_now()
    status = hb_result.get("status", "ok")
    summary = hb_result.get("summary", "Heartbeat completed")

    report_cmd = [
        "python3",
        str(SCRIPTS / "dispatch_reporter.py"),
        "--started-at",
        started_at,
        "--finished-at",
        finished_at,
        "--status",
        status,
        "--summary",
        summary,
    ]
    touched = hb_result.get("touchedIssueUrls", [])
    if touched:
        report_cmd.append("--touched")
        report_cmd.extend(touched)

    subprocess.run(report_cmd, cwd=ROOT, env=os.environ.copy())

    return 1 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
