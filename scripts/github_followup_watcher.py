#!/usr/bin/env python3
"""Watch open GitHub issues/PRs authored by itsmiso-ai across all reachable repos.

Outputs:
- HEARTBEAT_OK when nothing new needs follow-up
- Otherwise prints a concise activity report for new non-self comments/reviews/state changes

State is stored locally so repeated heartbeat runs only surface newly-seen activity.
Open PRs that need changes are also written to the PR review-fix queue so the
wishlist workers can update the existing branch instead of creating duplicate PRs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from pr_fix_queue import enqueue as enqueue_pr_fix, load_state as load_pr_fix_queue

AUTHOR = "itsmiso-ai"
STATE_PATH = Path("/home/node/.openclaw/workspace-saffron/.state/github_followup_watcher.json")
SELF_LOGINS = {AUTHOR, "github-actions[bot]"}
FIX_AUTHOR_ALLOWLIST = {AUTHOR}
FIX_BRANCH_OWNER_ALLOWLIST = {"misospace", "joryirving"}
FAILING_CHECK_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "CANCELLED"}

ACTIONABLE_COMMENT_PATTERNS = [
    r"\bplease\s+(?:fix|add|restore|update|change|remove|confirm)\b",
    r"\bcan\s+you\s+(?:fix|add|restore|update|change|remove|confirm)\b",
    r"\bconfirm\s+(?:the\s+)?(?:removal|deletion|change|behavior)\b",
    r"\bneeds?\s+(?:changes?|fix|work|update|verification)\b",
    r"\bunknowns?\s+or\s+needs?\s+verification\b",
    r"\bmissing\b",
    r"\bregression\b",
    r"\bfailing\b",
    r"\bbroken\b",
    r"\brequest(?:ed)?\s+changes?\b",
    r"\bnot\s+merge\b",
    r"\bdo\s+not\s+merge\b",
    r"\bshould\s+(?:restore|keep|not|also|include)\b",
]
AMBIGUOUS_COMMENT_PATTERNS = [
    r"\barchitecture\b",
    r"\bdesign\b",
    r"\bsecurity\b",
    r"\bauth(?:entication|orization)?\b",
    r"\bpolicy\b",
    r"\bscope\b",
    r"\bproduct decision\b",
    r"\bnot sure\b",
]


def gh_json(args: list[str]) -> Any:
    cmd = ["gh", *args]
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"items": {}}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"items": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def ts(value: str | None) -> str:
    return value or ""


def summarize_comment(comment: dict[str, Any]) -> dict[str, str]:
    author = ((comment.get("author") or {}).get("login")) or "unknown"
    body = (comment.get("body") or "").strip().replace("\n", " ")
    return {
        "id": str(comment.get("id") or comment.get("databaseId") or ""),
        "at": ts(comment.get("updatedAt") or comment.get("createdAt")),
        "author": author,
        "body": body[:240],
        "fullBody": body,
    }


def summarize_review(review: dict[str, Any]) -> dict[str, str]:
    author = ((review.get("author") or {}).get("login")) or "unknown"
    body = (review.get("body") or "").strip().replace("\n", " ")
    return {
        "id": str(review.get("id") or review.get("databaseId") or ""),
        "at": ts(review.get("submittedAt") or review.get("updatedAt") or review.get("createdAt")),
        "author": author,
        "state": review.get("state") or "UNKNOWN",
        "body": body[:240],
        "fullBody": body,
    }


def item_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def text_matches(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def infer_issue_number(data: dict[str, Any]) -> int | None:
    refs = data.get("closingIssuesReferences") or []
    if refs:
        number = refs[0].get("number")
        if number:
            return int(number)

    text = "\n".join(str(data.get(field) or "") for field in ("title", "body"))
    patterns = [
        r"(?:fix(?:es)?|close[sd]?|resolve[sd]?|refs?)\s+#(\d+)",
        r"\[#(\d+)\]",
        r"\bissue\s+#(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return int(match.group(1))
    return None


def infer_lane(repo: str, issue_number: int | None, title: str, labels: list[str]) -> str:
    label_set = {label.lower() for label in labels}
    title_l = title.lower()
    if (
        ("needs-gpt" in label_set or "needs-escalation" in label_set)
        or "audit" in label_set
        or "umbrella" in label_set
        or title_l.startswith(("weekly tech debt audit:", "tech debt audit:"))
        or "weekly tech debt audit:" in title_l
        or "[audit]" in title_l
    ):
        return "escalated"

    if repo and issue_number:
        try:
            issue = gh_json(["issue", "view", str(issue_number), "--repo", repo, "--json", "title,labels"])
            issue_labels = [str((label or {}).get("name", "")) for label in issue.get("labels", [])]
            return infer_lane(repo, None, issue.get("title") or title, issue_labels)
        except Exception:
            pass

    return "normal"


def check_summary(checks: list[dict[str, Any]]) -> tuple[list[str], str]:
    failed = []
    parts = []
    for check in checks:
        name = check.get("name") or check.get("workflowName") or "unknown"
        conclusion = str(check.get("conclusion") or "").upper()
        status = str(check.get("status") or "").upper()
        parts.append(f"{name}:{status}:{conclusion}")
        if conclusion in FAILING_CHECK_CONCLUSIONS:
            failed.append(name)
    return failed, "|".join(sorted(parts))


def evidence_already_recorded(repo: str, pr_number: int, evidence_key: str) -> bool:
    item = load_pr_fix_queue().get("items", {}).get(item_key(repo, pr_number), {})
    return evidence_key in set(item.get("evidenceKeys", []))


def enqueue_if_safe(
    *,
    data: dict[str, Any],
    repo: str,
    pr_number: int,
    lane: str,
    reason: str,
    feedback: str,
    evidence_key: str,
    events: list[str],
) -> None:
    if evidence_already_recorded(repo, pr_number, evidence_key):
        return

    author = ((data.get("author") or {}).get("login")) or ""
    branch_owner = ((data.get("headRepositoryOwner") or {}).get("login")) or ""
    if author not in FIX_AUTHOR_ALLOWLIST:
        events.append(f"PR {repo}#{pr_number}: not queued ({reason}); author @{author} is not in fix allowlist")
        return
    if branch_owner not in FIX_BRANCH_OWNER_ALLOWLIST:
        events.append(f"PR {repo}#{pr_number}: not queued ({reason}); branch owner {branch_owner} is not allowed")
        return

    issue_number = infer_issue_number(data)
    item = enqueue_pr_fix(
        repo=repo,
        pr=pr_number,
        lane=lane,
        reason=reason,
        feedback=feedback[:1000],
        evidence_key=evidence_key,
        issue=issue_number,
        branch=data.get("headRefName"),
        url=data.get("url"),
        title=data.get("title"),
        head_sha=((data.get("headRefOid") or "") or None),
        author=author,
    )
    status_word = "blocked for human" if item.get("status") == "blocked" else f"queued for {item.get('lane')}"
    events.append(f"PR {repo}#{pr_number}: {status_word} review-fix ({reason})")


def main() -> int:
    issues = gh_json([
        "search",
        "issues",
        "--author",
        AUTHOR,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,updatedAt,url,repository",
    ])
    prs = gh_json([
        "search",
        "prs",
        "--author",
        AUTHOR,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,updatedAt,url,repository",
    ])

    prior = load_state().get("items", {})
    next_state: dict[str, Any] = {}
    events: list[str] = []

    for item in issues:
        repo = item["repository"]["nameWithOwner"]
        number = item["number"]
        key = item_key(repo, number)
        prev = prior.get(key, {})

        data = gh_json([
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "title,url,updatedAt,state,comments",
        ])
        comments = [summarize_comment(c) for c in data.get("comments", []) if ((c.get("author") or {}).get("login") not in SELF_LOGINS)]
        latest_comment = max((c["at"] for c in comments), default="")
        snapshot = {
            "kind": "issue",
            "title": data["title"],
            "url": data["url"],
            "updatedAt": ts(data.get("updatedAt")),
            "state": data.get("state"),
            "latestNonSelfCommentAt": latest_comment,
        }
        next_state[key] = snapshot
        if prev:
            if latest_comment and latest_comment > prev.get("latestNonSelfCommentAt", ""):
                newest = max((c for c in comments if c["at"] == latest_comment), key=lambda c: c["at"])
                events.append(f"Issue {key}: new comment by @{newest['author']} at {newest['at']} | {newest['body']}")
            if snapshot.get("state") != prev.get("state"):
                events.append(f"Issue {key}: state changed from {prev.get('state')} to {snapshot.get('state')}")

    for item in prs:
        repo = item["repository"]["nameWithOwner"]
        number = item["number"]
        key = item_key(repo, number)
        prev = prior.get(key, {})

        data = gh_json([
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "title,body,url,updatedAt,state,isDraft,reviewDecision,mergeStateStatus,comments,reviews,headRefName,headRefOid,headRepositoryOwner,author,closingIssuesReferences,statusCheckRollup",
        ])
        comments = [summarize_comment(c) for c in data.get("comments", []) if ((c.get("author") or {}).get("login") not in SELF_LOGINS)]
        reviews = [summarize_review(r) for r in data.get("reviews", []) if ((r.get("author") or {}).get("login") not in SELF_LOGINS)]
        latest_comment = max((c["at"] for c in comments), default="")
        latest_review = max((r["at"] for r in reviews), default="")
        failed_checks, check_fingerprint = check_summary(data.get("statusCheckRollup") or [])
        issue_number = infer_issue_number(data)
        lane = infer_lane(repo, issue_number, data.get("title") or "", [])
        snapshot = {
            "kind": "pr",
            "title": data["title"],
            "url": data["url"],
            "updatedAt": ts(data.get("updatedAt")),
            "state": data.get("state"),
            "isDraft": data.get("isDraft"),
            "reviewDecision": data.get("reviewDecision"),
            "mergeStateStatus": data.get("mergeStateStatus"),
            "headRefOid": data.get("headRefOid"),
            "latestNonSelfCommentAt": latest_comment,
            "latestNonSelfReviewAt": latest_review,
            "checkFingerprint": check_fingerprint,
        }
        next_state[key] = snapshot

        if prev:
            if latest_comment and latest_comment > prev.get("latestNonSelfCommentAt", ""):
                newest = max((c for c in comments if c["at"] == latest_comment), key=lambda c: c["at"])
                events.append(f"PR {key}: new comment by @{newest['author']} at {newest['at']} | {newest['body']}")
                body = newest.get("fullBody", "")
                if text_matches(ACTIONABLE_COMMENT_PATTERNS, body):
                    chosen_lane = "needs-human" if text_matches(AMBIGUOUS_COMMENT_PATTERNS, body) else lane
                    enqueue_if_safe(
                        data=data,
                        repo=repo,
                        pr_number=number,
                        lane=chosen_lane,
                        reason="actionable_pr_comment",
                        feedback=f"@{newest['author']} commented: {body}",
                        evidence_key=f"comment:{newest.get('id') or newest['at']}",
                        events=events,
                    )
            if latest_review and latest_review > prev.get("latestNonSelfReviewAt", ""):
                newest = max((r for r in reviews if r["at"] == latest_review), key=lambda r: r["at"])
                events.append(f"PR {key}: new review by @{newest['author']} ({newest['state']}) at {newest['at']} | {newest['body']}")
                if newest.get("state") == "CHANGES_REQUESTED":
                    enqueue_if_safe(
                        data=data,
                        repo=repo,
                        pr_number=number,
                        lane=lane,
                        reason="changes_requested_review",
                        feedback=f"@{newest['author']} requested changes: {newest.get('fullBody', '')}",
                        evidence_key=f"review:{newest.get('id') or newest['at']}",
                        events=events,
                    )
            for field in ("state", "isDraft", "reviewDecision", "mergeStateStatus"):
                if snapshot.get(field) != prev.get(field):
                    events.append(f"PR {key}: {field} changed from {prev.get(field)} to {snapshot.get(field)}")
                    if field == "reviewDecision" and snapshot.get(field) == "CHANGES_REQUESTED":
                        enqueue_if_safe(
                            data=data,
                            repo=repo,
                            pr_number=number,
                            lane=lane,
                            reason="review_decision_changes_requested",
                            feedback="GitHub reviewDecision changed to CHANGES_REQUESTED.",
                            evidence_key=f"reviewDecision:{snapshot.get('headRefOid') or snapshot.get('updatedAt')}",
                            events=events,
                        )
            if failed_checks and check_fingerprint != prev.get("checkFingerprint", ""):
                enqueue_if_safe(
                    data=data,
                    repo=repo,
                    pr_number=number,
                    lane=lane,
                    reason="failing_checks",
                    feedback="Failing PR checks: " + ", ".join(failed_checks),
                    evidence_key=f"checks:{snapshot.get('headRefOid')}:{','.join(sorted(failed_checks))}",
                    events=events,
                )

    # Detect prior items that have disappeared from open search — they may be closed/merged
    for key, prev in prior.items():
        if key in next_state:
            continue
        # Item no longer in current open results — fetch live state directly
        repo_full, _, num_str = key.rpartition("#")
        try:
            number = int(num_str)
        except ValueError:
            continue
        snapshot: dict[str, Any] = {}
        if prev.get("kind") == "pr":
            try:
                info = gh_json(["pr", "view", str(number), "--repo", repo_full,
                                "--json", "title,url,state,mergedAt,mergeStateStatus"])
                snapshot = {
                    "kind": "pr",
                    "title": info.get("title") or "",
                    "url": info.get("url") or "",
                    "updatedAt": ts(info.get("updatedAt")),
                    "state": info.get("state") or "unknown",
                    "merged": bool(info.get("mergedAt")),
                }
                event_lines = [
                    f"PR {key}: merged since last check" if info.get("mergedAt") else f"PR {key}: closed since last check"
                ]
                if not info.get("mergedAt") and info.get("state") == "closed":
                    event_lines = [f"PR {key}: closed since last check"]
                for line in event_lines:
                    events.append(line)
                    next_state[key] = snapshot
            except Exception:
                # PR not found or inaccessible — skip
                continue
        else:
            try:
                info = gh_json(["issue", "view", str(number), "--repo", repo_full,
                                "--json", "title,url,state"])
                snapshot = {
                    "kind": "issue",
                    "title": info.get("title") or "",
                    "url": info.get("url") or "",
                    "updatedAt": ts(info.get("updatedAt")),
                    "state": info.get("state") or "unknown",
                }
                events.append(f"Issue {key}: closed since last check")
                next_state[key] = snapshot
            except Exception:
                # Issue not found or inaccessible — skip
                continue

    save_state({"items": next_state})

    if not events:
        print("HEARTBEAT_OK")
        return 0

    print("GITHUB_FOLLOWUP_EVENTS")
    for event in events:
        print(f"- {event}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
