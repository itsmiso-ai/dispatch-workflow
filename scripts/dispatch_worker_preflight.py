#!/usr/bin/env python3
"""Deterministic Dispatch worker preflight for Saffron cron workers.

This script does the queue plumbing that should not depend on an LLM:
- read PR-fix queue
- read/verify active work
- evidence-based active follow-up pass for in-progress/in-review owned items
- select one ready queue item
- optionally claim the selected issue

It prints either compact JSON or the worker terminal line for an empty lane.
Implementation work still belongs to the worker session.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GH = os.environ.get("GH", "/home/node/.local/bin/gh")
VALID_LANES = {"normal", "escalated"}
LANE_AGENT_DEFAULTS = {
    "normal": "saffron-normal",
    "escalated": "saffron-escalated",
}
RENOVATE_TITLE_RE = re.compile(r"(?:dependency dashboard|^update (?:dependency|image|deps?)|renovate)", re.I)

# Conclusions that indicate a check run definitively failed.
FAILED_CHECK_CONCLUSIONS = frozenset({"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"})

# Maximum PRs to check per preflight run.
ACTIVE_FOLLOWUP_CAP = 10


def dispatch_base_url() -> str:
    return os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000").rstrip("/")


def dispatch_token() -> str:
    return os.environ.get("DISPATCH_AGENT_TOKEN", "")


def dispatch_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
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


def labels_of(item: dict[str, Any]) -> set[str]:
    labels = item.get("labels") or []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            names.append(str(label.get("name") or ""))
        else:
            names.append(str(label))
    return {name.lower() for name in names if name}


def status_of(item: dict[str, Any]) -> str | None:
    status = item.get("status")
    if status:
        return str(status).lower()
    for label in labels_of(item):
        if label.startswith("status/"):
            return label
    return None


def lane_of(item: dict[str, Any]) -> str | None:
    lane = item.get("lane") or item.get("currentLane")
    if lane:
        lane_s = str(lane).lower()
        return "escalated" if lane_s == "gpt" else lane_s
    return None


def issue_key(repo: str, number: int | str) -> str:
    return f"{repo}#{int(number)}"


def repo_workspace(repo_full_name: str) -> str:
    repo_name = repo_full_name.rsplit("/", 1)[-1]
    return f"/data/git/{repo_name}"


def repo_workspace_ok(repo_full_name: str) -> tuple[bool, str]:
    if not repo_full_name or "/" not in repo_full_name:
        return False, "repoFullName missing or invalid"
    path = repo_workspace(repo_full_name)
    if not os.path.isdir(path):
        return False, f"repo workspace missing: {path}"
    if not os.path.isdir(os.path.join(path, ".git")):
        return False, f"repo workspace is not a git checkout: {path}"
    return True, path


def terminal_clear(lane: str) -> str:
    return "Escalated lane is clear." if lane == "escalated" else "Pipeline is clear."


def is_renovate(item: dict[str, Any]) -> bool:
    return bool(RENOVATE_TITLE_RE.search(str(item.get("title") or ""))) or bool(
        labels_of(item) & {"renovate", "dependencies", "automated"}
    )


def infer_lane_from_issue(issue: dict[str, Any], agent_name: str) -> tuple[str | None, str]:
    explicit = lane_of(issue)
    if explicit:
        return explicit, "dispatch-issue-lane"

    labels = labels_of(issue)
    if "needs-gpt" in labels or "needs-escalation" in labels or "escalated" in labels:
        return "escalated", "labels"
    if f"agent/{agent_name}".lower() in labels:
        if agent_name.endswith("-normal"):
            return "normal", "agent-label"
        if agent_name.endswith("-escalated"):
            return "escalated", "agent-label"
    return None, "unverified"


def find_dispatch_issue(repo: str, number: int) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"repo": repo, "limit": "200"})
    data = dispatch_request(f"/api/issues?{query}", timeout=20)
    if not isinstance(data, list):
        return None
    for issue in data:
        if not isinstance(issue, dict):
            continue
        try:
            if int(issue.get("number") or issue.get("issueNumber")) == int(number):
                return issue
        except (TypeError, ValueError):
            continue
    return None


def required_tools_ok() -> tuple[bool, list[str]]:
    missing: list[str] = []
    for tool in ("git", "python3"):
        if not shutil.which(tool):
            missing.append(tool)
    if not os.path.exists(GH) and not shutil.which("gh"):
        missing.append(GH)
    return not missing, missing


def _gh_binary() -> str:
    """Resolve the gh CLI binary path."""
    gh = os.environ.get("GH", "")
    if gh and os.path.exists(gh):
        return gh
    candidate = shutil.which("gh")
    if candidate:
        return candidate
    return GH


def _repo_of(issue: dict[str, Any]) -> str | None:
    """Extract the full repo name (owner/name) from a Dispatch issue object."""
    repo = issue.get("repository")
    if isinstance(repo, dict):
        return repo.get("fullName")
    return issue.get("repoFullName")


def pr_fix_next(lane: str, agent_name: str) -> dict[str, Any] | None:
    env = os.environ.copy()
    env["DISPATCH_AGENT_NAME"] = agent_name
    cmd = [
        "python3",
        "/home/node/.openclaw/workspace-saffron/scripts/pr_fix_queue.py",
        "next",
        "--lane",
        lane,
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "PR-fix queue read failed").strip())
    text = proc.stdout.strip()
    if not text:
        return None
    data = json.loads(text)
    return data if isinstance(data, dict) and data else None


def active_work_packet(lane: str, agent_name: str) -> dict[str, Any] | None:
    data = dispatch_request(f"/api/agents/{agent_name}/active-work", timeout=20)
    if not isinstance(data, dict) or not data.get("hasActiveWork"):
        return None
    context = data.get("context") if isinstance(data.get("context"), dict) else data

    repo = str(context.get("repoFullName") or "")
    number_raw = context.get("issueNumber") or context.get("number")
    try:
        number = int(number_raw)
    except (TypeError, ValueError):
        return {
            "action": "stuck",
            "terminal": "Stuck: active work issue number missing or invalid.",
            "reason": "active work issue number missing or invalid",
            "activeWork": context,
        }

    issue = find_dispatch_issue(repo, number) if repo else None
    verified_lane = lane_of(context)
    verification_source = "active-work"
    if not verified_lane and issue:
        verified_lane, verification_source = infer_lane_from_issue(issue, agent_name)

    if verified_lane != lane:
        return {
            "action": "stuck",
            "terminal": "Stuck: active work lane mismatch or could not be verified.",
            "reason": "active work lane mismatch or could not be verified",
            "expectedLane": lane,
            "verifiedLane": verified_lane,
            "verificationSource": verification_source,
            "activeWork": context,
            "issue": issue,
        }

    return {
        "action": "resume-active-work",
        "terminal": None,
        "lane": lane,
        "agentName": agent_name,
        "verificationSource": verification_source,
        "activeWork": context,
        "issue": issue,
        "nextAction": context.get("nextAction"),
        "checkpoint": context.get("checkpoint"),
    }


def active_followup_check_pr_gh(repo: str, pr_number: int) -> dict[str, Any] | None:
    """Check a PR for evidence that it needs follow-up action using gh CLI.

    Uses `gh pr view --json` to get the current aggregate PR state:
    reviewDecision, statusCheckRollup, mergeable, mergeStateStatus, state, isDraft.

    Returns an evidence dict if actionable evidence is found, or None otherwise.
    Each GitHub API failure is non-fatal — the function returns None for that
    lookup, allowing the caller to try the next item.
    """
    bin_gh = _gh_binary()
    cmd = [
        bin_gh, "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "reviewDecision,statusCheckRollup,mergeable,mergeStateStatus,state,isDraft",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return None

    if proc.returncode != 0:
        return None

    try:
        pr = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    # Skip closed / merged PRs — no follow-up needed
    if pr.get("state") not in ("OPEN",):
        return None

    # Skip draft PRs — rarely need immediate attention
    if pr.get("isDraft") is True:
        return None

    evidence: dict[str, Any] = {}

    # ── Review decision (current aggregate state, not historical reviews) ──
    review_decision = (pr.get("reviewDecision") or "").upper()
    if review_decision == "CHANGES_REQUESTED":
        evidence["changes_requested"] = True

    # ── Check runs (statusCheckRollup includes both CheckRun and StatusContext) ──
    status_rollup = pr.get("statusCheckRollup") or []
    failing_checks: list[dict[str, str]] = []
    for check in status_rollup:
        if not isinstance(check, dict):
            continue
        # CheckRun entries have "conclusion"; StatusContext entries have "state"
        conclusion = (check.get("conclusion") or "").upper()
        state = (check.get("state") or "").upper()
        name = check.get("name") or check.get("context") or "unknown"
        if conclusion in FAILED_CHECK_CONCLUSIONS:
            failing_checks.append({"name": name, "conclusion": conclusion})
        elif state in ("ERROR", "FAILURE"):
            failing_checks.append({"name": name, "conclusion": state})
    if failing_checks:
        evidence["failing_checks"] = failing_checks[:10]

    # ── Merge conflict detection (clear conflict states only) ──
    mergeable = (pr.get("mergeable") or "").upper()
    merge_state = (pr.get("mergeStateStatus") or "").upper()

    # CONFLICTING = genuine merge conflict
    if mergeable == "CONFLICTING":
        evidence["has_merge_conflict"] = True

    # DIRTY = GitHub detected merge conflict in the branch
    if merge_state == "DIRTY":
        evidence["merge_dirty"] = True

    # BLOCKED = merge blocked by branch protection, pending reviews, etc.
    # Not actionable by itself — only counts when paired with concrete evidence
    # (failing checks or CHANGES_REQUESTED) that the worker can act on.
    if merge_state == "BLOCKED" and (failing_checks or evidence.get("changes_requested")):
        evidence["merge_blocked"] = "BLOCKED"

    # BEHIND = branch needs rebase; only counts as evidence if paired with
    # failing checks (a clean behind is routine, not urgent)
    if merge_state == "BEHIND" and failing_checks:
        evidence["behind_with_failing"] = True

    return evidence if evidence else None


def _resolve_pr_url_for_issue(repo: str, issue_number: int, agent_work_by_issue: dict[str, str]) -> tuple[str | None, int | None]:
    """Resolve a PR URL for a Dispatch issue.

    Resolution order:
    1. AgentWork prUrl (if a matching AgentWork record exists)
    2. gh issue view --json closedByPullRequestsReferences (linked PRs from issue body)

    Returns (pr_url, pr_number) or (None, None).
    Does not invent PR URLs.
    """
    key = f"{repo}#{issue_number}"

    # 1. Prefer AgentWork prUrl — it's the most reliable source
    pr_url = agent_work_by_issue.get(key)
    if pr_url:
        pr_match = re.search(r"/pull/(\d+)", str(pr_url))
        if pr_match:
            return pr_url, int(pr_match.group(1))

    # 2. Fall back to gh issue view for linked PRs
    bin_gh = _gh_binary()
    cmd = [
        bin_gh, "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "closedByPullRequestsReferences",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            refs = data.get("closedByPullRequestsReferences") or []
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                ref_url = ref.get("url", "")
                ref_number = ref.get("number")
                # Only consider PRs in the same repo
                if ref_url and f"/{repo}/pull/" in ref_url:
                    return ref_url, ref_number
                # If URL is absent but number exists, construct it
                if ref_number:
                    return f"https://github.com/{repo}/pull/{ref_number}", ref_number
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    return None, None


def active_followup_pass(lane: str, agent_name: str) -> dict[str, Any] | None:
    """Evidence-based active follow-up pass for in-progress/in-review items owned by this agent.

    Starts from /api/issues (which has labels, lane, agent info) and cross-references
    with /api/agent-work for prUrl when available. Falls back to gh issue view for
    linked PRs when no AgentWork record exists.

    Checks each (up to ACTIVE_FOLLOWUP_CAP) for:
      - Dispatch status/in-progress or status/in-review
      - An agent/{agent_name} label
      - A discoverable PR URL (from AgentWork or linked PRs)
      - Actionable GitHub PR evidence (failing checks, merge conflicts, CHANGES_REQUESTED)

    Returns an action:"active-follow-up" packet for the first item with concrete
    evidence, or None to fall through to fresh queue selection.

    Guarantees:
      - Only inspects this agent's own issues (agent label filter).
      - Only considers status/in-progress or status/in-review on Dispatch.
      - Never mutates issue status or labels.
      - Never claims work.
      - Returns only when concrete evidence exists; otherwise falls through.
      - Caps inspection at ACTIVE_FOLLOWUP_CAP PRs per run.
    """
    # Require gh CLI
    if not shutil.which(_gh_binary()):
        return None

    # Fetch all open issues from Dispatch (has labels, lane, agent info)
    all_issues = dispatch_request("/api/issues?limit=300", timeout=20)
    if not isinstance(all_issues, list):
        return None

    # Fetch AgentWork records for prUrl cross-reference
    aw_data = dispatch_request(f"/api/agent-work?agent={agent_name}&include_stale=false", timeout=20)
    agent_work_by_issue: dict[str, str] = {}
    if isinstance(aw_data, dict):
        for item in aw_data.get("activeWork", []):
            if not isinstance(item, dict):
                continue
            repo = str(item.get("repoFullName") or "")
            num = item.get("issueNumber")
            pr_url = item.get("prUrl")
            if repo and num and pr_url:
                agent_work_by_issue[f"{repo}#{num}"] = pr_url

    # Filter to this agent's in-progress/in-review issues in the requested lane
    candidates: list[dict[str, Any]] = []
    agent_label = f"agent/{agent_name}".lower()
    for issue in all_issues:
        if not isinstance(issue, dict):
            continue
        if is_renovate(issue):
            continue

        # Must have this agent's label
        if agent_label not in labels_of(issue):
            continue

        # Must be in-progress or in-review
        dispatch_status = status_of(issue)
        if dispatch_status not in {"status/in-progress", "status/in-review"}:
            continue

        # Must match the requested lane (if lane is set)
        item_lane = lane_of(issue)
        if item_lane is not None and item_lane != lane:
            continue

        candidates.append(issue)

    if not candidates:
        return None

    # Check up to ACTIVE_FOLLOWUP_CAP candidates
    checked = 0
    for issue in candidates:
        if checked >= ACTIVE_FOLLOWUP_CAP:
            break

        repo = _repo_of(issue)
        issue_number = issue.get("number")
        if not repo or issue_number is None:
            continue

        try:
            issue_number = int(issue_number)
        except (TypeError, ValueError):
            continue

        # Resolve PR URL — prefer AgentWork, fall back to gh issue view
        pr_url, pr_number = _resolve_pr_url_for_issue(repo, issue_number, agent_work_by_issue)
        if not pr_url or pr_number is None:
            # No linked PR for this issue; can't check PR evidence
            continue

        checked += 1

        # Check PR for actionable evidence
        evidence = active_followup_check_pr_gh(repo, pr_number)
        if not evidence:
            continue

        return {
            "action": "active-follow-up",
            "terminal": None,
            "lane": lane,
            "agentName": agent_name,
            "evidence": evidence,
            "prUrl": pr_url,
            "prNumber": pr_number,
            "issue": issue,
        }

    return None


def queue_items(lane: str, agent_name: str) -> list[dict[str, Any]]:
    data = dispatch_request(f"/api/agents/{agent_name}/queue?lane={lane}&includeClaimed=true", timeout=20)
    return data if isinstance(data, list) else []


def item_agent_match(item: dict[str, Any], agent_name: str) -> bool:
    if item.get("agentMatch") is True:
        return True
    return f"agent/{agent_name}".lower() in labels_of(item)


def select_queue_item(items: list[dict[str, Any]], lane: str, agent_name: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or is_renovate(item):
            continue
        status = status_of(item)
        if status == "status/backlog" or status in {"status/done", "status/in-review", "status/in-progress"}:
            continue
        item_lane = lane_of(item) or lane
        if item_lane != lane:
            continue
        if item_agent_match(item, agent_name) or (item.get("claimable") is True and status == "status/ready"):
            candidates.append(item)

    claimed = [item for item in candidates if item_agent_match(item, agent_name)]
    return (claimed or candidates)[0] if candidates else None


def claim_issue(item: dict[str, Any], agent_name: str) -> dict[str, Any]:
    issue_id = item.get("issueId") or item.get("id")
    repo = item.get("repoFullName")
    number = item.get("number") or item.get("issueNumber")
    if not issue_id or not repo or number is None:
        raise RuntimeError("selected queue item lacks issueId/repoFullName/number")
    payload = {
        "issueId": issue_id,
        "repoFullName": repo,
        "issueNumber": int(number),
        "agentName": agent_name,
    }
    result = dispatch_request("/api/issues/claim", method="POST", payload=payload, timeout=30)
    return result if isinstance(result, dict) else {"result": result}


def build_packet(lane: str, agent_name: str, *, claim: bool) -> dict[str, Any]:
    ok, missing = required_tools_ok()
    if not ok:
        return {
            "action": "stuck",
            "terminal": f"Stuck: required tools unavailable: {', '.join(missing)}.",
            "reason": "required tools unavailable",
            "missingTools": missing,
        }

    # 1. PR-fix queue — highest priority
    pr_fix = pr_fix_next(lane, agent_name)
    if pr_fix:
        return {
            "action": "pr-fix",
            "terminal": None,
            "lane": lane,
            "agentName": agent_name,
            "item": pr_fix,
        }

    # 2. Resume active work — resumable checkpoint existing on this agent's issue
    active = active_work_packet(lane, agent_name)
    if active:
        return active

    # 3. Evidence-based active follow-up — in-progress/in-review items needing PR attention
    followup = active_followup_pass(lane, agent_name)
    if followup:
        return followup

    # 4. Fresh queue — new ready work
    items = queue_items(lane, agent_name)
    selected = select_queue_item(items, lane, agent_name)
    if not selected:
        return {
            "action": "clear",
            "terminal": terminal_clear(lane),
            "lane": lane,
            "agentName": agent_name,
            "queueCount": len(items),
        }

    repo = str(selected.get("repoFullName") or "")
    workspace_ok, workspace_detail = repo_workspace_ok(repo)
    if not workspace_ok:
        return {
            "action": "stuck",
            "terminal": f"Stuck: {workspace_detail}.",
            "reason": workspace_detail,
            "lane": lane,
            "agentName": agent_name,
            "item": selected,
        }

    packet = {
        "action": "claim-ready-issue" if claim else "ready-issue",
        "terminal": None,
        "lane": lane,
        "agentName": agent_name,
        "item": selected,
        "repoWorkspace": workspace_detail,
        "issueKey": issue_key(str(selected.get("repoFullName")), selected.get("number") or selected.get("issueNumber")),
    }
    if claim:
        packet["claim"] = claim_issue(selected, agent_name)
    return packet


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic Dispatch worker preflight")
    parser.add_argument("--lane", choices=sorted(VALID_LANES), default="normal")
    parser.add_argument("--agent-name", help="Dispatch worker agent name; defaults from lane")
    parser.add_argument("--claim", action="store_true", help="Claim selected ready issue before printing packet")
    parser.add_argument("--json", action="store_true", help="Always print JSON instead of terminal text for clear/stuck")
    args = parser.parse_args()

    agent_name = args.agent_name or os.environ.get("DISPATCH_AGENT_NAME") or LANE_AGENT_DEFAULTS[args.lane]

    try:
        packet = build_packet(args.lane, agent_name, claim=args.claim)
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        packet = {
            "action": "stuck",
            "terminal": f"Stuck: worker preflight failed: {e}.",
            "reason": f"worker preflight failed: {e}",
            "lane": args.lane,
            "agentName": agent_name,
        }

    if packet.get("terminal") and not args.json:
        print(packet["terminal"])
    else:
        print(json.dumps(packet, sort_keys=True))
    return 2 if packet.get("action") == "stuck" else 0


if __name__ == "__main__":
    raise SystemExit(main())