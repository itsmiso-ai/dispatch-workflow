#!/usr/bin/env python3
"""Run Dispatch issue sync and emit a compact heartbeat summary."""

from __future__ import annotations

import re
import subprocess
import sys

result = subprocess.run(
    [
        "python3",
        "/home/node/.openclaw/workspace-saffron/mission-control-workflow/scripts/project_backlog_sync.py",
    ],
    capture_output=True,
    text=True,
    timeout=120,
)

if result.returncode != 0:
    print(f"ERROR: dispatch sync failed: {(result.stderr or result.stdout)[:200]}")
    sys.exit(1)

match = re.search(r"Dispatch (?:(?:scheduled|legacy) )?sync: repos=(\d+) synced=(\d+) errors=(\d+)", result.stdout)
if not match:
    print("sync:unknown")
    sys.exit(0)

repos, synced, errors = match.groups()
print(f"sync:{repos} repos,{synced} synced,{errors} errors")
sys.exit(0)
