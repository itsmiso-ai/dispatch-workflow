#!/usr/bin/env python3
"""
Wrapper to run project_backlog_sync.py and capture a one-line summary.
"""

import subprocess
import sys

result = subprocess.run(
    ["python3", "/home/node/.openclaw/workspace-saffron/scripts/project_backlog_sync.py"],
    capture_output=True, text=True, timeout=120
)

# Parse key stats from output
lines = result.stdout.split("\n")
summary_line = ""
for line in lines:
    if "Total qualifying:" in line or "Added:" in line:
        summary_line = line.strip()
        break

if not summary_line and result.returncode != 0:
    print(f"ERROR: sync script failed: {result.stderr[:200]}")
    sys.exit(1)

# Extract key numbers
import re
added = re.search(r"Added: (\d+)", summary_line)
skipped = re.search(r"Skipped.*: (\d+)", summary_line)
qualifying = re.search(r"Total qualifying: (\d+)", summary_line)

added_n = int(added.group(1)) if added else 0
skipped_n = int(skipped.group(1)) if skipped else 0
qualifying_n = int(qualifying.group(1)) if qualifying else 0

print(f"sync:{qualifying_n} qualified,{added_n} added,{skipped_n} skipped")
sys.exit(0)