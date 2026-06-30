#!/usr/bin/env python3
"""Dedicated actuator for Saffron worker cron enabled/disabled state.

This is the only script allowed to mutate worker cron enabled state from the
heartbeat flow. The boundary is intentionally narrow:

Allowed:
- enable/disable only (`--enable` / `--disable`)
- only via this script, only with `--apply`
- via the hardcoded Dispatch cron IDs (env override allowed)

Forbidden (this script will not run them):
- changing schedule
- changing model
- changing prompt
- changing delivery
- changing alerts
- changing any other cron setting

Default mode is dry-run so an accidental invocation cannot mutate cron state.
Pass `--apply` to actually run `openclaw cron edit`.

Usage:
    python3 scripts/dispatch_worker_cron.py --lane local --enable --reason "probe has work" --apply --json
    python3 scripts/dispatch_worker_cron.py --lane normal --disable --reason "probe clear" --dry-run --json
    python3 scripts/dispatch_worker_cron.py --lane frontier --enable --reason "active follow-up" --apply --json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

# Hardcoded defaults — env vars can override but cannot disable the actuator.
DEFAULT_LOCAL_CRON_ID = "6b09bed4-cfbe-4c35-bbee-2b66c5ef17aa"
DEFAULT_CLOUD_CRON_ID = "8a6daaff-641b-4e1c-a263-f3814b043539"
DEFAULT_FRONTIER_CRON_ID = "1723278d-2eaa-435b-9fda-0efe8febb30b"

CRON_IDS = {
    "local": os.environ.get("DISPATCH_LOCAL_CRON_ID", DEFAULT_LOCAL_CRON_ID),
    "cloud": os.environ.get("DISPATCH_CLOUD_CRON_ID", DEFAULT_CLOUD_CRON_ID),
    "frontier": os.environ.get("DISPATCH_FRONTIER_CRON_ID", DEFAULT_FRONTIER_CRON_ID),
}

# Whitelist of allowed `openclaw cron edit` invocations. Anything outside this
# list is rejected so the actuator cannot accidentally touch schedule, model,
# prompt, delivery, or alerts.
def _build_command(cron_id: str, enabled: bool) -> list[str]:
    flag = "--enable" if enabled else "--disable"
    return ["openclaw", "cron", "edit", cron_id, flag]


def _resolve_cron_id(lane: str) -> str:
    if lane not in CRON_IDS:
        raise ValueError(f"unsupported lane: {lane!r}")
    return CRON_IDS[lane]


def run_actuator(
    lane: str,
    enabled: bool,
    reason: str,
    apply: bool,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Run the actuator in either dry-run or apply mode.

    In dry-run mode the actuator does not invoke openclaw. In apply mode it
    runs the whitelisted `openclaw cron edit` command and reports success.
    """
    cron_id = _resolve_cron_id(lane)
    cmd = _build_command(cron_id, enabled)

    result: dict[str, Any] = {
        "lane": lane,
        "cronId": cron_id,
        "enabled": enabled,
        "reason": reason,
        "dryRun": not apply,
        "applied": False,
        "command": list(cmd),
    }

    if not apply:
        return result

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except Exception as exc:  # pragma: no cover - defensive
        result["error"] = f"openclaw cron edit error: {exc}"
        return result

    if proc.returncode != 0:
        result["error"] = f"openclaw cron edit failed: {(proc.stderr or '').strip()[:200]}"
        return result

    result["applied"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Enable or disable a Saffron worker cron. Default mode is dry-run; "
            "pass --apply to actually run the whitelisted openclaw command."
        )
    )
    parser.add_argument(
        "--lane",
        required=True,
        choices=sorted(CRON_IDS.keys()),
        help="Worker lane: local, cloud, or frontier",
    )
    enable_group = parser.add_mutually_exclusive_group(required=True)
    enable_group.add_argument(
        "--enable",
        dest="enable",
        action="store_true",
        help="Enable the worker cron",
    )
    enable_group.add_argument(
        "--disable",
        dest="enable",
        action="store_false",
        help="Disable the worker cron",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for the enable/disable action",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        dest="apply",
        help="Actually run `openclaw cron edit`. Default is dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_false",
        dest="apply",
        help="(default) Print what would happen without invoking openclaw.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the result as JSON (default: short human-readable line).",
    )

    args = parser.parse_args()
    result = run_actuator(args.lane, args.enable, args.reason, args.apply)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        action_word = "ENABLED" if args.enable else "DISABLED"
        if result.get("dryRun"):
            print(
                f"DRY-RUN: would set {args.lane} cron ({result['cronId']}) "
                f"-> {action_word} ({args.reason})"
            )
        elif result.get("applied"):
            print(
                f"APPLIED: {args.lane} cron ({result['cronId']}) "
                f"-> {action_word} ({args.reason})"
            )
        else:
            print(
                f"FAILED: {args.lane} cron ({result['cronId']}) "
                f"-> {action_word} ({args.reason})"
            )
            if result.get("error"):
                print(f"  {result['error']}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
