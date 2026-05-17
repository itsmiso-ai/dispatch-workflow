#!/usr/bin/env python3
"""
Groom the Vibe Coding Backlog project: close resolved issues, validate and promote items,
and auto-disable wishlist cron when pipeline is empty.

Run as part of heartbeat.

Logic:
  - For items in any column: check if their linked issue has a merged PR. If yes -> move to Done + close issue.
  - For Triage items: read issue, validate it's ready to work on. If yes -> Ready. If not -> Backlog.
  - For Backlog items: read issue, validate it's ready to work on. If yes -> Ready. If not -> keep in Backlog.
  - Phase 4: Re-fetch board state; if Ready column has no open items from tracked repos -> disable wishlist cron.
              If Ready column has open items -> ensure wishlist cron is enabled.
"""

import os
import json
import subprocess
import re
import sys
from pathlib import Path

from pr_fix_queue import queued_items as queued_pr_fixes

GH = os.environ.get("GH", "/home/node/.local/bin/gh")
PROJECT_ID = "PVT_kwHOAsG-YM4BTyY3"
CRON_JOBS_FILE = "/home/node/.openclaw/cron/jobs.json"
WISHLIST_CRON_ID = "6b09bed4-cfbe-4c35-bbee-2b66c5ef17aa"

STATUS_FIELD_ID = "PVTSSF_lAHOAsG-YM4BTyY3zhA-4y0"
COLUMNS = {
    "Triage":      "f75ad846",
    "Backlog":     "94b12736",
    "Ready":       "1060d5eb",
    "In Progress": "47fc9ee4",
    "Done":        "98236657",
}
COLUMN_NAMES = {v: k for k, v in COLUMNS.items()}

TRACKED_REPOS = [
    "misospace/miso-chat",
    "misospace/miso-gallery",
    "misospace/mission-control",
    "misospace/pr-reviewer-action",
    "misospace/windowstead",
]

GPT_AUDIT_LABELS = {"audit", "needs-gpt", "needs-escalation"}
GPT_AUDIT_TITLE_PREFIXES = ("weekly tech debt audit:", "tech debt audit:")
GPT_AUDIT_CRON_ID = "1723278d-2eaa-435b-9fda-0efe8febb30b"
LANE_JUDGE = str(Path(__file__).with_name("issue_lane_judge.py"))

# Lane compatibility: "escalated" is canonical; "gpt" is legacy alias.
LANE_ALIASES = {"gpt": "escalated"}


def normalize_lane(lane: str) -> str:
    """Map legacy lane aliases to canonical names."""
    return LANE_ALIASES.get(lane, lane)


def gh(args: list, capture=True) -> subprocess.CompletedProcess:
    cmd = [GH] + args
    r = subprocess.run(cmd, capture_output=capture, text=True, timeout=30)
    return r


def gh_graphql(query: str) -> dict:
    result = gh(["api", "graphql", "--field", "query=" + query])
    if result.returncode != 0:
        return {"errors": [result.stderr]}
    return json.loads(result.stdout)


def get_merged_prs_for_repo(repo: str) -> dict:
    """Return {pr_number: pr_data} for all merged PRs referencing issues."""
    r = gh(["pr", "list", "--repo", repo, "--state", "merged", "--json", "number,title,body,mergedAt"])
    if r.returncode != 0:
        return {}
    prs = json.loads(r.stdout)
    merged = {}
    for pr in prs:
        body = pr.get("body", "")
        numbers = set(int(n) for n in re.findall(r'(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#(\d+)', body, re.I))
        for num in numbers:
            if num not in merged:
                merged[num] = []
            merged[num].append({"number": pr["number"], "title": pr["title"], "mergedAt": pr["mergedAt"]})
    return merged


def get_open_prs_for_repo(repo: str) -> list[dict]:
    """Return open PRs with enough metadata for board reconciliation."""
    r = gh([
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--json",
        "number,title,body,headRefName,url,isDraft,reviewDecision,mergeStateStatus,statusCheckRollup",
    ])
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def pr_mentions_issue(pr: dict, issue_number: int) -> bool:
    """Return True if a PR appears to cover a GitHub issue.

    Prefer explicit #number references in title/body. Also support the branch
    naming convention used by wishlist workers: fix/issue-{number}-slug.
    """
    number = str(issue_number)
    title_body = "\n".join([str(pr.get("title") or ""), str(pr.get("body") or "")])
    if re.search(rf"(?<!\d)#{re.escape(number)}(?!\d)", title_body):
        return True

    branch = str(pr.get("headRefName") or "").lower()
    branch_patterns = [
        rf"(?:^|[-_/])issue[-_/]?{re.escape(number)}(?:[-_/]|$)",
        rf"(?:^|[-_/]){re.escape(number)}(?:[-_/]|$)",
    ]
    return any(re.search(pattern, branch) for pattern in branch_patterns)


def pr_needs_more_work(pr: dict) -> tuple[bool, str]:
    """Classify whether an open PR needs another worker pass."""
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        return True, "review changes requested"

    merge_state = pr.get("mergeStateStatus") or ""
    if merge_state in {"DIRTY", "BEHIND", "BLOCKED", "UNKNOWN"}:
        return True, f"merge state {merge_state}"

    failing = []
    for check in pr.get("statusCheckRollup") or []:
        conclusion = check.get("conclusion")
        if conclusion in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
            failing.append(check.get("name") or "unknown")
    if failing:
        return True, "failing checks: " + ", ".join(failing[:3])

    return False, "open PR is pending/healthy"


def close_issue(repo: str, issue_number: int, comment: str):
    """Close a GitHub issue with a comment."""
    r = gh(["issue", "close", "--repo", repo, str(issue_number), "--comment", comment])
    return r.returncode == 0


def get_issue_body(repo: str, issue_number: int) -> str:
    """Fetch the body of an issue for validation."""
    r = gh(["api", "repos/" + repo + "/issues/" + str(issue_number), "--jq", ".body"])
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def issue_has_label(labels: set, label: str) -> bool:
    return label.lower() in {str(item).lower() for item in labels}


def add_label(repo: str, issue_number: int, label: str) -> bool:
    r = gh(["issue", "edit", str(issue_number), "--repo", repo, "--add-label", label])
    return r.returncode == 0


def remove_label(repo: str, issue_number: int, label: str) -> bool:
    r = gh(["issue", "edit", str(issue_number), "--repo", repo, "--remove-label", label])
    return r.returncode == 0


def judge_lane(repo: str, issue_number: int) -> dict | None:
    """Use the model-backed lane judge. Fail soft; do not guess on errors."""
    r = subprocess.run(
        [sys.executable, LANE_JUDGE, repo, str(issue_number)],
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


def apply_lane_judgment(item: dict) -> str | None:
    """Judge and apply lane labels/movement for a concrete issue.

    Returns lane when judged, or None when the judge failed.
    """
    judgment = judge_lane(item["repo"], item["issue_number"])
    if not judgment:
        return None

    lane = judgment["lane"]
    confidence = judgment["confidence"]
    reason = judgment.get("reason", "")
    print(f"      lane judge -> {lane} ({confidence}): {reason}")

    canonical = normalize_lane(lane)
    if canonical == "escalated":
        if not (issue_has_label(item["labels"], "needs-gpt") or issue_has_label(item["labels"], "needs-escalation")):
            if add_label(item["repo"], item["issue_number"], "needs-escalation"):
                print("      -> added needs-escalation label")
            else:
                print("      -> failed to add needs-escalation label")
        if item["status"] != "Ready":
            if move_item(item["item_id"], "Ready"):
                print("      -> Ready (Escalated lane)")
        return lane

    elif canonical == "normal":
        if (issue_has_label(item["labels"], "needs-gpt") or issue_has_label(item["labels"], "needs-escalation")):
            if (remove_label(item["repo"], item["issue_number"], "needs-gpt") or remove_label(item["repo"], item["issue_number"], "needs-escalation")):
                print("      -> removed stale needs-gpt/needs-escalation labels")
            else:
                print("      -> failed to remove stale needs-gpt/needs-escalation labels")
        if item["status"] != "Ready":
            if move_item(item["item_id"], "Ready"):
                print("      -> Ready (normal lane)")
        return lane

    elif canonical == "backlog":
        if item["status"] != "Backlog":
            if move_item(item["item_id"], "Backlog"):
                print("      -> Backlog (judge says not actionable)")
        return lane

    return lane


def is_gpt_audit_issue(item_or_title, labels: set = None) -> bool:
    """Return True for audit parents, not every issue with needs-gpt."""
    if isinstance(item_or_title, dict):
        title = item_or_title.get("issue_title", "")
        labels = item_or_title.get("labels", set())
    else:
        title = item_or_title or ""
        labels = labels or set()

    label_names = {str(label).lower() for label in labels}
    title_l = title.lower().strip()
    return (
        "audit" in label_names
        or "umbrella" in label_names
        or title_l.startswith(GPT_AUDIT_TITLE_PREFIXES) or "weekly tech debt audit:" in title_l or "[audit]" in title_l
    )


def has_large_audit_findings(repo: str, issue_number: int) -> bool:
    """Heuristic for routing audit issues to GPT instead of local wishlist.

    Weekly audit issues are expected to collect systemic findings. If the issue
    explicitly records P0/P1/P2 findings or follow-up breakdowns, keep it on the
    GPT lane. If it is just the initial placeholder with no findings yet, it can
    sit in Backlog until the next groom pass sees substance.
    """
    body = get_issue_body(repo, issue_number)
    if not body:
        return False

    body_l = body.lower()
    large_signals = [
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
    return len(body) >= 500 and any(signal in body_l for signal in large_signals)


def audit_already_decomposed(repo: str, issue_number: int, labels: set = None) -> bool:
    """Return True when an audit parent has already been split into follow-ups.

    Decomposed/umbrella audit parents should stay out of Ready/In Progress. The
    actionable work lives on the child issues; otherwise the GPT audit cron burns
    a run rediscovering that there is nothing left to decompose.
    """
    label_names = {str(label).lower() for label in (labels or set())}
    if "umbrella" in label_names:
        return True

    r = gh(["issue", "view", str(issue_number), "--repo", repo, "--json", "comments", "--jq", ".comments[].body"])
    if r.returncode != 0:
        return False

    comments = (r.stdout or "").lower()
    decomposition_signals = [
        "follow-up issues created",
        "created follow-up issues",
        "decomposed remaining",
        "focused follow-up issues",
        "already decomposed into follow-up issues",
        "decomposed into follow-up issues",
    ]
    return any(signal in comments for signal in decomposition_signals)


def is_sufficient_for_ready(repo: str, issue_number: int, labels: set) -> bool:
    """
    Validate that an issue has enough information to be moved to Ready.
    Returns True if the issue is actionable, False if it needs more info.
    """
    body = get_issue_body(repo, issue_number)

    # Must have some body text
    if not body:
        return False

    # Check for signals that this is a real, actionable issue:
    # - Has reasonable length (not just a title bump)
    # - Contains some description of what's needed
    # - Mentions acceptance criteria, scope, or expected behavior

    # Minimum useful content: ~100 chars of meaningful text
    if len(body) < 50:
        return False

    # Check for common "needs more info" patterns
    needs_more = [
        "tbd",
        "to be determined",
        "todo",
        "fill in",
        "placeholder",
        "more details needed",
        "needs more info",
        "???",
        "discuss",
        "not sure yet",
    ]
    body_lower = body.lower()
    if any(phrase in body_lower for phrase in needs_more):
        # Allow if there's still substantial content beyond the placeholder phrase
        if len(body) < 300:
            return False

    # Positive signals: contains descriptions of what/why/how
    actionable_signals = [
        "implement",
        "add",
        "fix",
        "create",
        "build",
        "update",
        "when",
        "should",
        "if",
        "user can",
        "allows",
        "displays",
        "shows",
        "tracks",
        "enables",
        "prevents",
        "##",
        "- ",
        "* ",
        "acceptance",
        "criteria",
        "scope",
        "detail",
        "the ",
    ]
    signal_count = sum(1 for s in actionable_signals if s in body_lower)
    if signal_count < 2:
        return False

    return True


def get_project_items() -> list:
    query = """
    {
      node(id: "%s") {
        ... on ProjectV2 {
          items(first: 100) {
            nodes {
              id
              fieldValues(first: 10) {
                nodes {
                  __typename
                  ... on ProjectV2ItemFieldSingleSelectValue { name }
                }
              }
              content {
                ... on Issue {
                  id
                  number
                  title
                  state
                  repository { nameWithOwner }
                  labels(first: 10) { nodes { name } }
                }
              }
            }
          }
        }
      }
    }
    """ % PROJECT_ID

    result = gh_graphql(query)
    if result.get("errors"):
        print(f"  [!] Error fetching items: {result['errors']}")
        return []

    items = result["data"]["node"]["items"]["nodes"]
    processed = []

    for item in items:
        content = item.get("content", {})
        if not content:
            continue

        status_name = None
        for fv in item.get("fieldValues", {}).get("nodes", []):
            if fv.get("__typename") == "ProjectV2ItemFieldSingleSelectValue":
                status_name = fv.get("name")
                break

        repo = content.get("repository", {}).get("nameWithOwner", "unknown")
        labels = {l["name"].lower() for l in content.get("labels", {}).get("nodes", [])}

        processed.append({
            "item_id": item["id"],
            "issue_id": content["id"],
            "issue_number": content["number"],
            "issue_title": content["title"],
            "issue_state": content["state"],
            "repo": repo,
            "labels": labels,
            "status": status_name or "Triage",
        })

    return processed


def move_item(item_id: str, target_column: str) -> bool:
    option_id = COLUMNS.get(target_column)
    if not option_id:
        print(f"  [!] Unknown column: {target_column}")
        return False

    mutation = """
    mutation {
      updateProjectV2ItemFieldValue(input: {
        projectId: "%s"
        itemId: "%s"
        fieldId: "%s"
        value: { singleSelectOptionId: "%s" }
      }) {
        projectV2Item { id }
      }
    }
    """ % (PROJECT_ID, item_id, STATUS_FIELD_ID, option_id)

    result = gh_graphql(mutation)
    if result.get("errors"):
        print(f"  [!] Move error: {result['errors']}")
        return False
    return True


def set_wishlist_cron(enabled: bool):
    """Enable or disable the wishlist chip cron in jobs.json."""
    try:
        with open(CRON_JOBS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  [!] Could not read {CRON_JOBS_FILE}: {e}")
        return

    found = False
    for job in data.get("jobs", []):
        if job.get("id") == WISHLIST_CRON_ID:
            job["enabled"] = enabled
            found = True
            state = "ENABLED" if enabled else "DISABLED"
            print(f"  [*] (Saffron): 35B Wishlist Chip -> {state}")

    if not found:
        print(f"  [!] Could not find wishlist cron id {WISHLIST_CRON_ID}")
        return

    try:
        with open(CRON_JOBS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  [!] Could not write {CRON_JOBS_FILE}: {e}")


def set_cron_enabled(job_id: str, enabled: bool, display_name: str):
    """Enable or disable a cron job in jobs.json."""
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


def main():
    print("[*] Grooming Vibe Coding Backlog...")

    print("[*] Fetching merged PRs for tracked repos...")
    merged_prs = {}
    for repo in TRACKED_REPOS:
        merged_prs[repo] = get_merged_prs_for_repo(repo)
        count = len(merged_prs[repo])
        print(f"      {repo}: {count} merged PRs referencing issues")

    items = get_project_items()
    by_status = {}
    for item in items:
        s = item["status"]
        by_status[s] = by_status.get(s, 0) + 1
    print(f"  Total items: {len(items)}")
    print(f"  Current columns: {by_status}")

    # Phase 1: Close resolved items
    print("\n[*] Phase 1: Checking for resolved issues...")
    closed_count = 0
    for item in items:
        repo = item["repo"]
        if repo not in TRACKED_REPOS:
            continue
        if item["status"] == "Done":
            continue
        if item["issue_state"] == "CLOSED":
            continue

        issue_num = item["issue_number"]
        if issue_num in merged_prs.get(repo, {}):
            pr = merged_prs[repo][issue_num][0]
            comment = f"Closed — fixed by PR #{pr['number']} (merged {pr['mergedAt'][:10]})."
            print(f"  [{repo} #{issue_num}] {item['issue_title'][:50]}...")
            print(f"      -> Done (PR #{pr['number']} merged)")
            move_item(item["item_id"], "Done")
            close_issue(repo, issue_num, comment)
            closed_count += 1

    print(f"  Phase 1: {closed_count} issues closed -> Done")

    # Phase 2: Validate and promote Triage items
    print("\n[*] Phase 2: Validating Triage items...")
    triage_items = [i for i in items if i["status"] == "Triage"]
    if triage_items:
        for item in triage_items:
            print(f"  [{item['repo']} #{item['issue_number']}] {item['issue_title']}")
            print(f"      labels: {sorted(item['labels'])}")

            if is_gpt_audit_issue(item):
                if audit_already_decomposed(item["repo"], item["issue_number"], item["labels"]):
                    if move_item(item["item_id"], "Backlog"):
                        print("      -> Backlog (audit already decomposed into follow-up issues)")
                    else:
                        print("      -> failed to move decomposed audit issue to Backlog")
                    continue
                if has_large_audit_findings(item["repo"], item["issue_number"]):
                    if move_item(item["item_id"], "Ready"):
                        print("      -> Ready (GPT audit findings present)")
                    else:
                        print("      -> failed to move GPT audit issue to Ready")
                else:
                    if move_item(item["item_id"], "Backlog"):
                        print("      -> Backlog (audit placeholder / no findings yet)")
                    else:
                        print("      -> failed to move GPT audit issue to Backlog")
                continue

            lane = apply_lane_judgment(item)
            if lane:
                continue

            if is_sufficient_for_ready(item["repo"], item["issue_number"], item["labels"]):
                if move_item(item["item_id"], "Ready"):
                    print(f"      -> Ready (sufficient info; judge unavailable)")
                else:
                    print(f"      -> failed to move to Ready")
            else:
                if move_item(item["item_id"], "Backlog"):
                    print(f"      -> Backlog (needs more info / underspecified)")
                else:
                    print(f"      -> failed to move to Backlog")
    else:
        print("  -> No Triage items")

    # Phase 3: Validate Backlog items and promote if ready
    print("\n[*] Phase 3: Validating Backlog items...")
    backlog_items = [i for i in items if i["status"] == "Backlog"]
    promoted_count = 0
    if backlog_items:
        for item in backlog_items:
            if is_gpt_audit_issue(item):
                if audit_already_decomposed(item["repo"], item["issue_number"], item["labels"]):
                    continue
                if has_large_audit_findings(item["repo"], item["issue_number"]):
                    print(f"  [{item['repo']} #{item['issue_number']}] {item['issue_title']}")
                    print(f"      labels: {sorted(item['labels'])}")
                    print("      -> promoting to Ready (GPT audit findings present)")
                    if move_item(item["item_id"], "Ready"):
                        promoted_count += 1
                    else:
                        print(f"      -> failed to promote")
                continue

            if is_sufficient_for_ready(item["repo"], item["issue_number"], item["labels"]):
                print(f"  [{item['repo']} #{item['issue_number']}] {item['issue_title']}")
                print(f"      labels: {sorted(item['labels'])}")
                lane = apply_lane_judgment(item)
                canonical_lane = normalize_lane(lane)
                if canonical_lane == "normal" or canonical_lane == "escalated":
                    promoted_count += 1
                elif lane is None:
                    print(f"      -> keeping in Backlog (lane judge unavailable)")
            # else: stays in Backlog, no output
    else:
        print("  -> No Backlog items")

    if promoted_count:
        print(f"  Phase 3: promoted {promoted_count} Backlog items to Ready")

    # Phase 4: Wishlist cron management
    print("\n[*] Phase 4: Re-fetching board state for wishlist cron check...")
    items = get_project_items()

    decomposed_audit_items = [
        i for i in items
        if i["status"] in ("Ready", "In Progress")
        and i["repo"] in TRACKED_REPOS
        and i["issue_state"] == "OPEN"
        and is_gpt_audit_issue(i)
        and audit_already_decomposed(i["repo"], i["issue_number"], i["labels"])
    ]
    if decomposed_audit_items:
        print("[*] Phase 4a: Moving decomposed audit parents out of active lanes...")
        changed = False
        for item in decomposed_audit_items:
            print(f"  [{item['repo']} #{item['issue_number']}] {item['issue_title']}")
            if move_item(item["item_id"], "Backlog"):
                print("      -> Backlog (audit already decomposed into follow-up issues)")
                changed = True
            else:
                print("      -> failed to move decomposed audit issue to Backlog")
        if changed:
            items = get_project_items()

    # Reconcile already-Ready/In Progress concrete items that carry stale
    # routing labels. Heartbeat/groom owns lane assignment; crons consume lanes.
    routing_candidates = [
        i for i in items
        if i["status"] in ("Ready", "In Progress")
        and i["repo"] in TRACKED_REPOS
        and i["issue_state"] == "OPEN"
        and (issue_has_label(i["labels"], "needs-gpt") or issue_has_label(i["labels"], "needs-escalation"))
        and not is_gpt_audit_issue(i)
    ]
    if routing_candidates:
        print("[*] Phase 4a: Reconciling model-judged lane labels...")
        changed = False
        for item in routing_candidates:
            print(f"  [{item['repo']} #{item['issue_number']}] {item['issue_title']}")
            print(f"      labels: {sorted(item['labels'])}")
            before_status = item["status"]
            lane = apply_lane_judgment(item)
            canonical_lane = normalize_lane(lane)
            if canonical_lane in ("normal", "escalated", "backlog"):
                changed = True
        if changed:
            items = get_project_items()

    # Reconcile normal active items against open PR state. The cron consumes
    # only Ready items; groom owns whether an issue is Ready for another pass or
    # In Progress waiting on review/CI/merge.
    normal_active_items = [
        i for i in items
        if i["status"] in ("Ready", "In Progress")
        and i["repo"] in TRACKED_REPOS
        and i["issue_state"] == "OPEN"
        and not is_gpt_audit_issue(i)
        and not (issue_has_label(i["labels"], "needs-gpt") or issue_has_label(i["labels"], "needs-escalation"))
    ]
    if normal_active_items:
        print("[*] Phase 4b: Reconciling normal items with open PR state...")
        open_prs = {repo: get_open_prs_for_repo(repo) for repo in sorted({i["repo"] for i in normal_active_items})}
        changed = False
        for item in normal_active_items:
            prs = [pr for pr in open_prs.get(item["repo"], []) if pr_mentions_issue(pr, item["issue_number"])]
            if not prs:
                if item["status"] == "In Progress":
                    print(f"  [{item['repo']} #{item['issue_number']}] no open PR -> Ready")
                    if move_item(item["item_id"], "Ready"):
                        changed = True
                    else:
                        print("      -> failed to move back to Ready")
                continue

            # If any matching PR needs more work, put/keep the issue in Ready
            # so the cron updates the existing PR instead of starting new work.
            actionable = []
            for pr in prs:
                needs_work, reason = pr_needs_more_work(pr)
                if needs_work:
                    actionable.append((pr, reason))

            if actionable:
                pr, reason = actionable[0]
                if item["status"] != "Ready":
                    print(f"  [{item['repo']} #{item['issue_number']}] PR #{pr['number']} needs work ({reason}) -> Ready")
                    if move_item(item["item_id"], "Ready"):
                        changed = True
                    else:
                        print("      -> failed to move back to Ready")
                else:
                    print(f"  [{item['repo']} #{item['issue_number']}] PR #{pr['number']} needs work ({reason}); stays Ready")
                continue

            # Open PR exists and does not currently require another worker pass.
            # Keep it out of Ready so cron can move to the next item.
            pr = prs[0]
            if item["status"] == "Ready":
                print(f"  [{item['repo']} #{item['issue_number']}] PR #{pr['number']} open and healthy/pending -> In Progress")
                if move_item(item["item_id"], "In Progress"):
                    changed = True
                else:
                    print("      -> failed to move to In Progress")
        if changed:
            items = get_project_items()

    queued_normal_pr_fixes = queued_pr_fixes("normal")
    queued_gpt_pr_fixes = queued_pr_fixes("escalated")
    queued_human_pr_fixes = queued_pr_fixes("needs-human", include_blocked=True)

    ready_items = [i for i in items if i["status"] == "Ready" and i["repo"] in TRACKED_REPOS and i["issue_state"] == "OPEN"]
    in_progress_items = [i for i in items if i["status"] == "In Progress" and i["repo"] in TRACKED_REPOS and i["issue_state"] == "OPEN"]
    gpt_audit_items = [
        i for i in ready_items + in_progress_items
        if (
            (is_gpt_audit_issue(i) or issue_has_label(i["labels"], "needs-gpt") or issue_has_label(i["labels"], "needs-escalation"))
            and not audit_already_decomposed(i["repo"], i["issue_number"], i["labels"])
        )
    ]
    gpt_item_ids = {i["item_id"] for i in gpt_audit_items}
    normal_ready_items = [i for i in ready_items if i["item_id"] not in gpt_item_ids]
    normal_in_progress_items = [i for i in in_progress_items if i["item_id"] not in gpt_item_ids]
    print(f"  Ready column (open tracked items): {len(ready_items)}")
    print(f"  In Progress column (open tracked items): {len(in_progress_items)}")
    print(f"  GPT audit lane items: {len(gpt_audit_items)}")
    print(f"  Queued normal PR fixes: {len(queued_normal_pr_fixes)}")
    print(f"  Queued GPT PR fixes: {len(queued_gpt_pr_fixes)}")
    if queued_human_pr_fixes:
        print(f"  Blocked PR fixes needing human review: {len(queued_human_pr_fixes)}")

    if normal_ready_items or normal_in_progress_items or queued_normal_pr_fixes:
        for item in normal_ready_items:
            print(f"      [Ready] {item['repo']} #{item['issue_number']}: {item['issue_title'][:50]}")
        for item in normal_in_progress_items:
            print(f"      [In Progress] {item['repo']} #{item['issue_number']}: {item['issue_title'][:50]}")
        for item in queued_normal_pr_fixes:
            print(f"      [PR fix] {item['repo']} #{item['pr']}: {str(item.get('title') or '')[:50]}")
        print("  -> Keeping (Saffron): 35B Wishlist Chip ENABLED (normal lane work or PR fixes exist)")
        set_wishlist_cron(True)
    else:
        print("  -> No non-audit Ready/In Progress work — DISABLING self-hosted wishlist cron")
        set_wishlist_cron(False)

    if gpt_audit_items or queued_gpt_pr_fixes:
        for item in gpt_audit_items:
            print(f"      [GPT audit] {item['status']} {item['repo']} #{item['issue_number']}: {item['issue_title'][:50]}")
        for item in queued_gpt_pr_fixes:
            print(f"      [GPT PR fix] {item['repo']} #{item['pr']}: {str(item.get('title') or '')[:50]}")
        print("  -> Keeping (Saffron): GPT-5.5 Wishlist Chip ENABLED (GPT lane work or PR fixes exist)")
        set_cron_enabled(GPT_AUDIT_CRON_ID, True, "(Saffron): GPT-5.5 Wishlist Chip")
    else:
        print("  -> No GPT audit work — DISABLING GPT audit chipping cron")
        set_cron_enabled(GPT_AUDIT_CRON_ID, False, "(Saffron): GPT-5.5 Wishlist Chip")


if __name__ == "__main__":
    main()
