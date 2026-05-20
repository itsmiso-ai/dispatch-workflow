#!/usr/bin/env python3
"""Dispatch-first grooming for Saffron work queues.

GitHub issues/PRs are the source of truth for issue state, labels, and merged PRs.
Dispatch owns work discovery, claims, lane assignment, and cron enablement.

This script intentionally does not read or mutate GitHub Projects.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pr_fix_queue import queued_items as queued_pr_fixes

GH = os.environ.get("GH", "/home/node/.local/bin/gh")
CRON_JOBS_FILE = "/home/node/.openclaw/cron/jobs.json"
WISHLIST_CRON_ID = "6b09bed4-cfbe-4c35-bbee-2b66c5ef17aa"
GPT_AUDIT_CRON_ID = "1723278d-2eaa-435b-9fda-0efe8febb30b"
LANE_JUDGE = str(Path(__file__).with_name("issue_lane_judge.py"))

TRACKED_REPOS = [
    "misospace/miso-chat",
    "misospace/miso-gallery",
    "misospace/dispatch",
    "misospace/pr-reviewer-action",
    "misospace/windowstead",
]

GPT_AUDIT_LABELS = {"audit", "needs-gpt", "needs-escalation"}
GPT_AUDIT_TITLE_PREFIXES = ("weekly tech debt audit:", "tech debt audit:")
LANE_ALIASES = {"gpt": "escalated"}
VALID_LANES = {"normal", "escalated", "backlog"}


def normalize_lane(lane: str | None) -> str | None:
    if not lane:
        return None
    return LANE_ALIASES.get(str(lane).lower(), str(lane).lower())


def gh(args: list[str], capture: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run([GH] + args, capture_output=capture, text=True, timeout=timeout)


def dispatch_base_url() -> str:
    return os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000").rstrip("/")


def dispatch_token() -> str:
    return os.environ.get("DISPATCH_AGENT_TOKEN", "")


def dispatch_request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 20) -> Any:
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
        if not raw:
            return None
        return json.loads(raw)


def dispatch_sync() -> bool:
    try:
        result = dispatch_request("/api/sync", method="POST", timeout=60)
    except Exception as e:
        print(f"  [!] Dispatch sync failed: {e}")
        return False

    if isinstance(result, dict) and result.get("success"):
        print(f"  Dispatch sync: syncedCount={result.get('syncedCount', '?')} repos={result.get('repos', '?')}")
        return True

    print(f"  [!] Unexpected Dispatch sync response: {str(result)[:300]}")
    return False


def get_dispatch_issues(repo: str, limit: int = 200) -> list[dict[str, Any]]:
    try:
        data = dispatch_request(f"/api/issues?repo={repo}&limit={limit}")
    except Exception as e:
        print(f"  [!] Could not fetch Dispatch issues for {repo}: {e}")
        return []
    return data if isinstance(data, list) else []


def get_all_dispatch_issues() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for repo in TRACKED_REPOS:
        issues.extend(get_dispatch_issues(repo))
    return issues


def get_dispatch_queue(lane: str) -> list[dict[str, Any]]:
    try:
        data = dispatch_request(f"/api/agents/saffron/queue?lane={lane}")
    except Exception as e:
        print(f"  [!] Dispatch {lane} queue check failed: {e}")
        return []
    return data if isinstance(data, list) else []


def classify_dispatch_issue(issue_id: str, lane: str, reason: str, *, confidence: str = "high", model: str = "saffron-groom") -> bool:
    lane = normalize_lane(lane) or "normal"
    if lane not in VALID_LANES:
        print(f"      [!] invalid lane {lane!r}; skipping")
        return False

    payload = {
        "model": model,
        "classification": {
            "lane": lane,
            "confidence": confidence,
            "reason": reason,
        },
    }
    try:
        result = dispatch_request(f"/api/issues/{issue_id}/lane", method="POST", payload=payload)
    except Exception as e:
        print(f"      [!] Dispatch lane update failed: {e}")
        return False

    if isinstance(result, dict) and result.get("success"):
        print(f"      -> Dispatch lane {result.get('lane')} ({result.get('confidence')})")
        return True

    print(f"      [!] Unexpected lane response: {str(result)[:300]}")
    return False


def repo_full_name(issue: dict[str, Any]) -> str:
    repo = issue.get("repository") or {}
    owner = repo.get("owner")
    name = repo.get("name")
    if owner and name:
        return f"{owner}/{name}"
    return str(issue.get("repoFullName") or "")


def issue_number(issue: dict[str, Any]) -> int | None:
    number = issue.get("number") or issue.get("issueNumber")
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def issue_labels(issue: dict[str, Any]) -> set[str]:
    return {str(label).lower() for label in (issue.get("labels") or [])}


def issue_has_label(labels: set[str], label: str) -> bool:
    return label.lower() in labels


def issue_title(issue: dict[str, Any]) -> str:
    return str(issue.get("title") or "")


def issue_body(issue: dict[str, Any]) -> str:
    return str(issue.get("body") or "")


def is_open(issue: dict[str, Any]) -> bool:
    return str(issue.get("state") or "").lower() == "open"


def is_gpt_audit_issue(issue: dict[str, Any]) -> bool:
    labels = issue_labels(issue)
    title_l = issue_title(issue).lower().strip()
    return (
        bool(labels & GPT_AUDIT_LABELS)
        or "umbrella" in labels
        or title_l.startswith(GPT_AUDIT_TITLE_PREFIXES)
        or "weekly tech debt audit:" in title_l
        or "[audit]" in title_l
    )


def has_large_audit_findings(issue: dict[str, Any]) -> bool:
    body = issue_body(issue)
    if not body:
        return False
    body_l = body.lower()
    signals = [
        "p0",
        "p1",
        "p2",
        "top findings",
        "recommended issue breakdown",
        "follow-up issue",
        "systemic",
        "architecture",
        "medium/large",
        "overall risk",
    ]
    return len(body) >= 500 and any(signal in body_l for signal in signals)


def audit_already_decomposed(issue: dict[str, Any]) -> bool:
    if issue.get("decomposed") is True:
        return True
    labels = issue_labels(issue)
    if "umbrella" in labels:
        return True
    note = str(issue.get("decomposedNote") or "").lower()
    if note:
        return True
    return bool(issue.get("followUpUrls") or [])


def get_merged_prs_for_repo(repo: str) -> dict[int, list[dict[str, Any]]]:
    r = gh(["pr", "list", "--repo", repo, "--state", "merged", "--json", "number,title,body,mergedAt"], timeout=60)
    if r.returncode != 0:
        return {}
    try:
        prs = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}

    merged: dict[int, list[dict[str, Any]]] = {}
    for pr in prs:
        body = pr.get("body") or ""
        numbers = set(int(n) for n in re.findall(r"(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#(\d+)", body, re.I))
        for num in numbers:
            merged.setdefault(num, []).append({"number": pr["number"], "title": pr["title"], "mergedAt": pr["mergedAt"]})
    return merged


def close_issue(repo: str, number: int, comment: str) -> bool:
    r = gh(["issue", "close", "--repo", repo, str(number), "--comment", comment], timeout=60)
    return r.returncode == 0


def remove_label(repo: str, number: int, label: str) -> bool:
    r = gh(["issue", "edit", str(number), "--repo", repo, "--remove-label", label], timeout=60)
    return r.returncode == 0


def judge_lane(repo: str, number: int) -> dict[str, Any] | None:
    r = subprocess.run(
        [sys.executable, LANE_JUDGE, repo, str(number)],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if r.returncode != 0:
        print(f"      [!] lane judge failed: {(r.stderr or r.stdout).strip()[:300]}")
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"      [!] lane judge returned invalid JSON: {r.stdout[:300]}")
        return None


def set_cron_enabled(job_id: str, enabled: bool, display_name: str) -> None:
    try:
        with open(CRON_JOBS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  [!] Could not read {CRON_JOBS_FILE}: {e}")
        return

    found = False
    for job in data.get("jobs", []):
        if job.get("id") == job_id:
            job["enabled"] = enabled
            found = True
            state = "ENABLED" if enabled else "DISABLED"
            print(f"  [*] {display_name} -> {state}")

    if not found:
        print(f"  [!] Could not find {display_name} cron id {job_id}")
        return

    try:
        with open(CRON_JOBS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  [!] Could not write {CRON_JOBS_FILE}: {e}")


def set_wishlist_cron(enabled: bool) -> None:
    set_cron_enabled(WISHLIST_CRON_ID, enabled, "(Saffron): 35B Wishlist Chip")


def cleanup_status_done_labels(issues: list[dict[str, Any]]) -> int:
    removed = 0
    for issue in issues:
        if not is_open(issue):
            continue
        labels = issue_labels(issue)
        if "status/done" not in labels:
            continue
        repo = repo_full_name(issue)
        number = issue_number(issue)
        if not repo or number is None:
            continue
        print(f"  [{repo} #{number}] removing stale status/done label (issue is still open)")
        if remove_label(repo, number, "status/done"):
            print("      -> removed status/done label")
            removed += 1
        else:
            print("      -> failed to remove status/done label")
    return removed


def close_resolved_issues(issues: list[dict[str, Any]], merged_prs: dict[str, dict[int, list[dict[str, Any]]]]) -> int:
    closed = 0
    for issue in issues:
        if not is_open(issue):
            continue
        repo = repo_full_name(issue)
        number = issue_number(issue)
        if repo not in TRACKED_REPOS or number is None:
            continue
        prs = merged_prs.get(repo, {}).get(number) or []
        if not prs:
            continue
        pr = prs[0]
        comment = f"Closed — fixed by PR #{pr['number']} (merged {pr['mergedAt'][:10]})."
        print(f"  [{repo} #{number}] {issue_title(issue)[:70]}")
        if close_issue(repo, number, comment):
            print(f"      -> closed on GitHub (PR #{pr['number']} merged)")
            closed += 1
        else:
            print("      -> failed to close issue")
    return closed


def classify_audit_issue(issue: dict[str, Any]) -> tuple[str, str, str]:
    if audit_already_decomposed(issue):
        return "backlog", "high", "Audit parent already decomposed; actionable work lives on follow-up issues"
    if has_large_audit_findings(issue):
        return "escalated", "high", "Weekly audit findings require GPT decomposition into follow-up issues"
    return "backlog", "medium", "Audit placeholder has no substantive findings yet"


def reconcile_lanes(issues: list[dict[str, Any]]) -> int:
    changed = 0
    for issue in issues:
        if not is_open(issue):
            continue
        repo = repo_full_name(issue)
        number = issue_number(issue)
        issue_id = issue.get("id")
        if repo not in TRACKED_REPOS or number is None or not issue_id:
            continue

        current_lane = normalize_lane(issue.get("currentLane")) or "normal"
        desired_lane: str | None = None
        confidence = "medium"
        reason = ""

        if is_gpt_audit_issue(issue):
            desired_lane, confidence, reason = classify_audit_issue(issue)
        elif current_lane not in VALID_LANES:
            judgment = judge_lane(repo, number)
            if judgment:
                desired_lane = normalize_lane(judgment.get("lane"))
                confidence = str(judgment.get("confidence") or "medium")
                reason = str(judgment.get("reason") or "Model lane judgment")
        else:
            continue

        if desired_lane not in VALID_LANES:
            continue
        if desired_lane == current_lane:
            continue

        print(f"  [{repo} #{number}] {issue_title(issue)[:70]}")
        print(f"      lane: {current_lane} -> {desired_lane}; {reason}")
        if classify_dispatch_issue(issue_id, desired_lane, reason, confidence=confidence):
            changed += 1
    return changed


def manage_crons() -> tuple[int, int]:
    normal_queue = get_dispatch_queue("normal")
    escalated_queue = get_dispatch_queue("escalated")
    queued_normal_pr_fixes = queued_pr_fixes("normal")
    queued_gpt_pr_fixes = queued_pr_fixes("escalated")
    queued_human_pr_fixes = queued_pr_fixes("needs-human", include_blocked=True)

    print(f"  Dispatch normal queue: {len(normal_queue)}")
    print(f"  Dispatch escalated queue: {len(escalated_queue)}")
    print(f"  Queued normal PR fixes: {len(queued_normal_pr_fixes)}")
    print(f"  Queued GPT PR fixes: {len(queued_gpt_pr_fixes)}")
    if queued_human_pr_fixes:
        print(f"  Blocked PR fixes needing human review: {len(queued_human_pr_fixes)}")

    if normal_queue or queued_normal_pr_fixes:
        print("  -> Keeping normal wishlist cron enabled")
        set_wishlist_cron(True)
    else:
        print("  -> No normal Dispatch work — disabling normal wishlist cron")
        set_wishlist_cron(False)

    if escalated_queue or queued_gpt_pr_fixes:
        print("  Escalated queue items:")
        for item in escalated_queue[:10]:
            print(f"      {item.get('repoFullName', '?')} #{item.get('number', '?')}: {str(item.get('title') or '')[:70]}")
        print("  -> Keeping GPT wishlist cron enabled")
        set_cron_enabled(GPT_AUDIT_CRON_ID, True, "(Saffron): GPT-5.5 Wishlist Chip")
    else:
        print("  -> No escalated Dispatch work — disabling GPT wishlist cron")
        set_cron_enabled(GPT_AUDIT_CRON_ID, False, "(Saffron): GPT-5.5 Wishlist Chip")

    return len(normal_queue), len(escalated_queue)


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch-first grooming for Saffron queues")
    parser.add_argument("--no-sync", action="store_true", help="Skip Dispatch issue sync before grooming")
    args = parser.parse_args()

    print("[*] Grooming Dispatch queues...")

    if not args.no_sync:
        print("[*] Syncing GitHub issues into Dispatch...")
        dispatch_sync()

    print("[*] Fetching Dispatch issues...")
    issues = get_all_dispatch_issues()
    by_lane: dict[str, int] = {}
    open_count = 0
    for issue in issues:
        lane = normalize_lane(issue.get("currentLane")) or "normal"
        by_lane[lane] = by_lane.get(lane, 0) + 1
        if is_open(issue):
            open_count += 1
    print(f"  Total cached tracked issues: {len(issues)}")
    print(f"  Open cached tracked issues: {open_count}")
    print(f"  Current lanes: {by_lane}")

    print("\n[*] Checking merged PR closures from GitHub...")
    merged_prs = {}
    for repo in TRACKED_REPOS:
        merged_prs[repo] = get_merged_prs_for_repo(repo)
        print(f"      {repo}: {len(merged_prs[repo])} merged PRs referencing issues")
    closed = close_resolved_issues(issues, merged_prs)
    print(f"  Closed issues: {closed}")

    print("\n[*] Cleaning stale status/done labels from open GitHub issues...")
    removed_labels = cleanup_status_done_labels(issues)
    print(f"  Removed stale status/done labels: {removed_labels}")

    if closed or removed_labels:
        print("\n[*] Re-syncing Dispatch after GitHub mutations...")
        dispatch_sync()
        issues = get_all_dispatch_issues()

    print("\n[*] Reconciling Dispatch lane assignments...")
    changed_lanes = reconcile_lanes(issues)
    print(f"  Lane updates: {changed_lanes}")

    print("\n[*] Managing crons from Dispatch queues...")
    normal_count, escalated_count = manage_crons()

    print("\nSummary:")
    print(f"  dispatch:{len(issues)} cached,{open_count} open,{normal_count} normal_queue,{escalated_count} escalated_queue,{closed} closed,{removed_labels} stale_done_removed,{changed_lanes} lane_updates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
