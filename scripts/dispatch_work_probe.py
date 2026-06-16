#!/usr/bin/env python3
"""Shared "would this lane/agent do work?" probe for heartbeat/grooming.

The deterministic Dispatch worker preflight already knows how to answer that
question — it covers the PR-fix queue, resumable active work, evidence-based
active follow-up, and fresh ready queue. Heartbeat and grooming reporting used
to re-implement their own queue-only signal, which under-counted work and let
the heartbeat disable/suppress worker crons that the worker preflight would
have kept enabled.

This module wraps `build_packet(..., claim=False)` so heartbeat/grooming share
one source of truth with the workers themselves. It is strictly read-only: it
never claims work, never mutates status, and never edits labels.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from typing import Any

# Probe runs from the same scripts/ directory as the preflight, so a plain
# sibling import works whether it is invoked as a module or as a script.
try:
    from dispatch_worker_preflight import (  # type: ignore
        LANE_AGENT_DEFAULTS,
        VALID_LANES,
        build_packet,
    )
except ImportError:  # pragma: no cover - script invocation path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dispatch_worker_preflight import (  # type: ignore
        LANE_AGENT_DEFAULTS,
        VALID_LANES,
        build_packet,
    )


# Action -> (hasWork, shouldRunWorker, needsAttention) classification shared
# with project_groom.py reporting and cron enable/disable logic.
#
# Rules (per task spec):
#   - clear                          : hasWork=false, shouldRunWorker=false
#   - stuck                          : hasWork=false, shouldRunWorker=true,
#                                      needsAttention=true
#   - pr-fix, resume-active-work,
#     active-follow-up, ready-issue,
#     claim-ready-issue,
#     claim-conflict                 : hasWork=true,  shouldRunWorker=true
WORK_ACTIONS = frozenset(
    {
        "pr-fix",
        "resume-active-work",
        "active-follow-up",
        "ready-issue",
        "claim-ready-issue",
        "claim-conflict",
    }
)


def classify_action(action: str | None) -> dict[str, bool]:
    """Map a preflight action to heartbeat/grooming reporting fields.

    Returns a dict with `hasWork`, `shouldRunWorker`, and (when relevant)
    `needsAttention`. The shape is stable so callers and tests can rely on it.
    """
    if action == "stuck":
        return {"hasWork": False, "shouldRunWorker": True, "needsAttention": True}
    if action in WORK_ACTIONS:
        return {"hasWork": True, "shouldRunWorker": True}
    # Default: treat unknown actions as no work, no run.
    return {"hasWork": False, "shouldRunWorker": False}


def probe_work(lane: str, agent_name: str | None = None) -> dict[str, Any]:
    """Return a normalized probe result for one lane/agent pair.

    `claim=False` is hardcoded — the probe must never claim work.

    The returned dict always has these keys:
      - lane                : echoed lane
      - agentName           : resolved agent name
      - shouldRunWorker     : bool
      - hasWork             : bool
      - action              : preflight action string ("clear", "stuck", ...)
      - reason              : short human-readable reason (when available)
      - packet              : raw preflight packet (so callers can introspect)

    On preflight error, the probe returns a synthetic "stuck" packet so the
    caller does not have to special-case exceptions.
    """
    if lane not in VALID_LANES:
        return {
            "lane": lane,
            "agentName": agent_name,
            "shouldRunWorker": False,
            "hasWork": False,
            "action": "stuck",
            "reason": f"invalid lane: {lane!r}",
            "packet": None,
        }

    resolved_agent = agent_name or LANE_AGENT_DEFAULTS[lane]

    try:
        packet = build_packet(lane, resolved_agent, claim=False)
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, json.JSONDecodeError) as e:
        packet = {
            "action": "stuck",
            "terminal": f"Stuck: worker preflight failed: {e}.",
            "reason": f"worker preflight failed: {e}",
            "lane": lane,
            "agentName": resolved_agent,
        }

    action = packet.get("action") if isinstance(packet, dict) else None
    flags = classify_action(action)
    reason = ""
    if isinstance(packet, dict):
        reason = str(packet.get("reason") or packet.get("terminal") or "")

    result: dict[str, Any] = {
        "lane": lane,
        "agentName": resolved_agent,
        "shouldRunWorker": flags["shouldRunWorker"],
        "hasWork": flags["hasWork"],
        "action": action or "unknown",
        "reason": reason,
        "packet": packet,
    }
    if flags.get("needsAttention"):
        result["needsAttention"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe whether a Saffron worker lane/agent would do work."
    )
    parser.add_argument("--lane", choices=sorted(VALID_LANES), default="normal")
    parser.add_argument(
        "--agent-name",
        help="Dispatch worker agent name; defaults from lane if omitted",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Always print JSON (default for non-clear/stuck actions)",
    )
    args = parser.parse_args()

    result = probe_work(args.lane, args.agent_name)

    if args.json or result["action"] not in {"clear", "stuck"}:
        print(json.dumps(result, sort_keys=True, default=str))
    else:
        # Mirror preflight's compact terminal line for clear/stuck so existing
        # log scrapers keep working.
        terminal = ""
        packet = result.get("packet")
        if isinstance(packet, dict):
            terminal = str(packet.get("terminal") or "")
        print(terminal or f"{args.lane} probe: action={result['action']} hasWork={result['hasWork']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
