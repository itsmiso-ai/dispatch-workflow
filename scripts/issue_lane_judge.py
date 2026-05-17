#!/usr/bin/env python3
"""Judge whether a GitHub issue belongs in the normal or escalated lane.

Lane compatibility:
  - "escalated" is the canonical lane name (replaces legacy "gpt").
  - The model prompt returns "escalated"; legacy "gpt" values are auto-mapped.

This is intentionally model-backed. Scripts may identify structural audit parents,
but concrete issue routing should be an engineering judgment, not keyword soup.
"""
import argparse
import json
import os
import subprocess
import sys

GH = os.environ.get("GH", "/home/node/.local/bin/gh")
OPENCLAW = os.environ.get("OPENCLAW", "openclaw")
DEFAULT_MODEL = "litellm/self-hosted"

# Legacy lane aliases → canonical names.
LANE_ALIASES = {"gpt": "escalated"}


def normalize_lane(lane: str) -> str:
    """Map legacy lane aliases to canonical names."""
    return LANE_ALIASES.get(lane, lane)


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def gh_issue(repo: str, number: int) -> dict:
    r = run([
        GH,
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "title,body,labels,state,comments,url",
    ])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "gh issue view failed").strip())
    return json.loads(r.stdout)


def truncate(value: str, limit: int) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def build_prompt(repo: str, number: int, issue: dict) -> str:
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    comments = issue.get("comments", [])[-5:]
    comment_text = "\n\n".join(
        f"[{c.get('author', {}).get('login', 'unknown')}] {c.get('body', '')}"
        for c in comments
    )
    return f"""You are Saffron's heartbeat backlog lane judge.

Decide which execution lane should own this GitHub issue.

Return ONLY compact JSON with this exact schema:
{{"lane":"normal"|"escalated"|"backlog","confidence":"high"|"medium"|"low","reason":"short reason"}}

Lane definitions:
- normal: concrete, scoped, testable implementation work suitable for the self-hosted 35B worker.
- escalated: requires GPT-level judgment before/during work: audit parent decomposition, architecture/security/API/auth boundary design, database/schema migration strategy, distributed/cross-service design, ambiguous product behavior, broad refactor with unclear safe slice, explicit RFC/design/alternatives decision.
- backlog: not actionable yet, placeholder, missing enough detail, already decomposed parent with no direct work.

Rules:
- Do not route to escalated just because labels include needs-escalation, needs-gpt, priority/p1, or because the issue came from an audit.
- Do route audit parent/umbrella issues with broad findings to escalated for decomposition/design unless already decomposed.
- Documentation, tests, CI, lint, release/version drift, bounded backend/frontend fixes, and concrete follow-up issues usually go normal.
- If the issue already chooses a reasonable implementation approach and has acceptance criteria, prefer normal.
- If confidence is low, choose backlog rather than guessing.

Issue:
repo: {repo}
number: {number}
url: {issue.get('url', '')}
title: {issue.get('title', '')}
state: {issue.get('state', '')}
labels: {', '.join(labels)}

body:
{truncate(issue.get('body') or '', 12000)}

recent comments:
{truncate(comment_text, 8000)}
"""


def parse_json_response(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def judge(repo: str, number: int, model: str = DEFAULT_MODEL) -> dict:
    issue = gh_issue(repo, number)
    prompt = build_prompt(repo, number, issue)
    # Use --local to avoid routing to main agent session via --gateway.
    # --gateway routes through the agent runtime which can send requests to
    # the default agent (Miso/main) instead of running as Saffron's own context.
    r = run([
        OPENCLAW,
        "infer",
        "model",
        "run",
        "--local",
        "--model",
        model,
        "--prompt",
        prompt,
    ], timeout=180)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "model judge failed").strip())
    data = parse_json_response(r.stdout)
    lane = normalize_lane(data.get("lane", ""))
    confidence = data.get("confidence")
    if lane not in {"normal", "escalated", "backlog"}:
        raise ValueError(f"invalid lane: {lane!r}")
    if confidence not in {"high", "medium", "low"}:
        raise ValueError(f"invalid confidence: {confidence!r}")
    return {
        "repo": repo,
        "number": number,
        "lane": lane,
        "confidence": confidence,
        "reason": str(data.get("reason") or "").strip()[:300],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("number", type=int)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()
    try:
        print(json.dumps(judge(args.repo, args.number, args.model)))
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
