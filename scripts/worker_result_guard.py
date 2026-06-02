#!/usr/bin/env python3
"""Validate terminal output from Saffron worker cron sessions."""

from __future__ import annotations

import argparse
import re
import sys

TERMINAL_RE = re.compile(
    r"^(?:"
    r"Pipeline is clear\."
    r"|Escalated lane is clear\."
    r"|Done\. PR #\d+ opened for .+"
    r"|Done\. PR #\d+ updated for .+"
    r"|Done\. Decomposed .+"
    r"|Stuck: .+"
    r")",
    re.S,
)

MID_TASK_RE = re.compile(
    r"^(?:"
    r"Let me\b"
    r"|Now\b"
    r"|I(?:'|’)ll\b"
    r"|I need\b"
    r"|I have\b"
    r"|The checkpoint\b"
    r"|The test\b"
    r"|Good[, ]"
    r"|Claimed\b"
    r")",
    re.I,
)


def validate(text: str) -> tuple[bool, str]:
    stripped = text.strip()
    if not stripped:
        return False, "empty worker result"
    if TERMINAL_RE.match(stripped):
        return True, "terminal worker result"
    if MID_TASK_RE.match(stripped):
        return False, "mid-task narration is not a terminal worker result"
    return False, "worker result does not match the terminal contract"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Normal/Escalated worker terminal output")
    parser.add_argument("text", nargs="*", help="Result text. Reads stdin when omitted.")
    args = parser.parse_args()

    text = " ".join(args.text) if args.text else sys.stdin.read()
    ok, reason = validate(text)
    if ok:
        print(text.strip())
        return 0
    print(f"INVALID_WORKER_RESULT: {reason}", file=sys.stderr)
    if text.strip():
        print(text.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
