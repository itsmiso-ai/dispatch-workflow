#!/usr/bin/env python3
"""Dispatch-first grooming for Saffron work queues.

GitHub issues/PRs are the source of truth for issue state, labels, and merged PRs.
Dispatch owns work discovery, claims, lane assignment, and cron enablement.

This script intentionally does not read or mutate GitHub Projects.
"""

from __future__ import annotations

import argparse
import datetime as dt
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
NORMAL_WORKER_CRON_ID = "6b09bed4-cfbe-4c35-bbee-2b66c5ef17aa"
ESCALATED_WORKER_CRON_ID = "1723278d-2eaa-435b-9fda-0efe8febb30b"
LANE_JUDGE = str(Path(__file__).with_name("issue_lane_judge.py"))
NORMAL_WORKER_AGENT = os.environ.get("DISPATCH_NORMAL_AGENT", "saffron-normal")
ESCALATED_WORKER_AGENT = os.environ.get("DISPATCH_ESCALATED_AGENT", "saffron-escalated")

DEFAULT_TRACKED_REPOS = [
    "misospace/miso-chat",
    "misospace/miso-gallery",
    "misospace/dispatch",
    "misospace/pr-reviewer-action",
    "misospace/windowstead",
]

# Populated at runtime from Dispatch when available. The default list is a
# safety fallback only; Dispatch is the canonical repo inventory.
TRACKED_REPOS = DEFAULT_TRACKED_REPOS.copy()

GPT_AUDIT_LABELS = {"audit", "needs-gpt", "needs-escalation"}
GPT_AUDIT_TITLE_PREFIXES = ("weekly tech debt audit:", "tech debt audit:")
LANE_ALIASES = {"gpt": "escalated"}
VALID_LANES = {"normal", "escalated", "backlog"}

STATE_DIR = Path(os.environ.get("SAFFRON_STATE_DIR", Path.home() / ".openclaw" / "workspace-saffron" / ".state"))
BACKLOG_GROOMING_STATE = STATE_DIR / "backlog_grooming.json"
BACKLOG_GROOMING_REPORTS = STATE_DIR / "backlog_grooming_reports"
BACKLOG_GROOMING_MARKER = "<!-- saffron-backlog-grooming -->"
BACKLOG_GROOMING_MODEL = os.environ.get("BACKLOG_GROOMING_MODEL", "litellm/self-hosted")
BACKLOG_GROOMING_COMMAND = os.environ.get("BACKLOG_GROOMING_COMMAND", "")

BACKLOG_RECOMMENDATIONS = {
    "ready",
    "escalated",
    "needs-human",
    "needs-info",
    "decompose",
    "keep-backlog",
}
HUMAN_ATTENTION_RECOMMENDATIONS = {"needs-human", "needs-info"}

RENOVATE_TITLE_RE = re.compile(r"(?:dependency dashboard|^update (?:dependency|image|deps?)|renovate)", re.I)
GPT_MODEL_RE = re.compile(r"(?:^|/)(?:gpt|chatgpt)[-/]", re.I)


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


def dispatch_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
    require_token: bool = True,
) -> Any:
    token = dispatch_token()
    if require_token and not token:
        raise RuntimeError("DISPATCH_AGENT_TOKEN not set")

    data = None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
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


def get_tracked_repos() -> list[str]:
    """Return Dispatch's enabled tracked repos, with local defaults as fallback."""
    try:
        data = dispatch_request(
            "/api/automation/repos/tracked",
            require_token=False,
            timeout=10,
        )
    except Exception as e:
        print(f"  [!] Dispatch tracked repo lookup failed; using fallback list: {e}", file=sys.stderr)
        return DEFAULT_TRACKED_REPOS.copy()

    if not isinstance(data, list):
        print("  [!] Dispatch tracked repo response was not a list; using fallback list", file=sys.stderr)
        return DEFAULT_TRACKED_REPOS.copy()

    repos: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        full_name = item.get("fullName")
        if isinstance(full_name, str) and "/" in full_name:
            repos.append(full_name)

    if not repos:
        print("  [!] Dispatch tracked repo response was empty; using fallback list", file=sys.stderr)
        return DEFAULT_TRACKED_REPOS.copy()

    return sorted(dict.fromkeys(repos))


def dispatch_sync() -> bool:
    try:
        result = dispatch_request("/api/sync/scheduled", method="POST", payload={}, timeout=60)
    except urllib.error.HTTPError as e:
        print(f"  [!] Dispatch scheduled sync failed: HTTP {e.code} {e.reason}")
        return False
    except Exception as e:
        print(f"  [!] Dispatch scheduled sync failed: {e}")
        return False

    if isinstance(result, dict) and result.get("success"):
        issues = result.get("issues") or result
        print(f"  Dispatch scheduled sync: syncedCount={issues.get('syncedCount', '?')} repos={issues.get('repos', '?')}")
        return True

    print(f"  [!] Unexpected Dispatch scheduled sync response: {str(result)[:300]}")
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
    agent = ESCALATED_WORKER_AGENT if normalize_lane(lane) == "escalated" else NORMAL_WORKER_AGENT
    try:
        data = dispatch_request(f"/api/agents/{agent}/queue?lane={lane}")
    except Exception as e:
        print(f"  [!] Dispatch {lane} queue check failed for {agent}: {e}")
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


def set_dispatch_status(issue: dict[str, Any], status: str, reason: str) -> bool:
    """Set issue lifecycle status through Dispatch, not direct GitHub label edits."""
    issue_id = issue.get("id")
    repo = repo_full_name(issue)
    number = issue_number(issue)
    if not issue_id or not repo or number is None:
        return False

    payload = {
        "issueId": issue_id,
        "repoFullName": repo,
        "issueNumber": number,
        "status": status,
        "agentName": "saffron",
        "actor": "saffron-groom",
    }
    try:
        result = dispatch_request("/api/issues/status", method="POST", payload=payload, timeout=30)
    except Exception as e:
        print(f"      [!] Dispatch status update failed: {e}")
        return False

    if isinstance(result, dict) and result.get("success"):
        print(f"      -> status/{status} ({reason})")
        return True

    print(f"      [!] Unexpected status response: {str(result)[:300]}")
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


def issue_status(issue: dict[str, Any]) -> str | None:
    for label in issue.get("labels") or []:
        label_s = str(label)
        if label_s.startswith("status/"):
            return label_s.lower()
    return None


def issue_url(issue: dict[str, Any]) -> str:
    return str(issue.get("url") or "")


def issue_updated_at(issue: dict[str, Any]) -> str:
    return str(issue.get("updatedAt") or issue.get("updated_at") or "")


def is_renovate_issue(issue: dict[str, Any]) -> bool:
    labels = issue_labels(issue)
    title = issue_title(issue)
    return bool(RENOVATE_TITLE_RE.search(title)) or bool(labels & {"renovate", "dependencies", "automated"})


def load_backlog_grooming_state() -> dict[str, Any]:
    try:
        return json.loads(BACKLOG_GROOMING_STATE.read_text())
    except FileNotFoundError:
        return {"issues": {}}
    except Exception as e:
        print(f"  [!] Could not read backlog grooming state: {e}")
        return {"issues": {}}


def save_backlog_grooming_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKLOG_GROOMING_STATE.write_text(json.dumps(state, indent=2, sort_keys=True))


def backlog_grooming_already_handled(previous: Any) -> bool:
    if not isinstance(previous, dict):
        return False
    return any(previous.get(key) for key in ("appliedAt", "dryRunAt", "skippedAt"))


def github_issue_comments(repo: str, number: int, limit: int = 8) -> list[dict[str, Any]]:
    r = gh([
        "api",
        f"repos/{repo}/issues/{number}/comments",
        "--paginate",
        "--jq",
        ".[] | {author:.user.login, createdAt:.created_at, body:.body}",
    ], timeout=60)
    if r.returncode != 0:
        return []

    comments: list[dict[str, Any]] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            comments.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return comments[-limit:]


def existing_grooming_comment(repo: str, number: int) -> bool:
    comments = github_issue_comments(repo, number, limit=30)
    return any(BACKLOG_GROOMING_MARKER in str(comment.get("body") or "") for comment in comments)


def github_issue_snapshot(repo: str, number: int) -> dict[str, Any] | None:
    r = gh([
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "body,labels,number,state,title,updatedAt,url",
    ], timeout=60)
    if r.returncode != 0:
        print(f"      [!] failed to fetch live issue state: {(r.stderr or r.stdout).strip()[:300]}")
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        print("      [!] failed to parse live issue state")
        return None
    return data if isinstance(data, dict) else None


def live_issue_status(snapshot: dict[str, Any]) -> str | None:
    for label in snapshot.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        name_s = str(name or "").lower()
        if name_s.startswith("status/"):
            return name_s
    return None


def issue_with_live_github_state(issue: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    live = dict(issue)
    if snapshot.get("title"):
        live["title"] = snapshot["title"]
    if snapshot.get("body") is not None:
        live["body"] = snapshot["body"]
    if snapshot.get("state"):
        live["state"] = str(snapshot["state"]).lower()
    if snapshot.get("updatedAt"):
        live["updatedAt"] = snapshot["updatedAt"]
    if snapshot.get("url"):
        live["url"] = snapshot["url"]
    if snapshot.get("labels") is not None:
        live["labels"] = [
            str(label.get("name") if isinstance(label, dict) else label)
            for label in snapshot.get("labels") or []
        ]
    return live


def issue_has_actionable_detail(issue: dict[str, Any]) -> bool:
    body = issue_body(issue).lower()
    if len(body.strip()) < 900:
        return False
    has_scope = any(
        marker in body
        for marker in [
            "## goal",
            "## problem",
            "## desired behavior",
            "## root cause",
            "## suggested implementation",
        ]
    )
    has_acceptance = "acceptance criteria" in body
    has_validation = "## validation" in body or "## tests" in body or "\n## test" in body
    return has_scope and has_acceptance and has_validation


def should_comment_on_grooming(issue: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str]:
    recommendation = result.get("recommendation")
    if recommendation in {"ready", "escalated"} and issue_has_actionable_detail(issue):
        return False, "issue already has actionable scope, acceptance criteria, and validation"
    return True, "grooming comment adds missing detail or surfaces a non-ready reason"


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def build_backlog_grooming_prompt(issue: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    repo = repo_full_name(issue)
    number = issue_number(issue)
    labels = sorted(issue_labels(issue))
    comments_text = "\n\n".join(
        f"[{c.get('createdAt', '?')}] {c.get('author', '?')}:\n{truncate_text(str(c.get('body') or ''), 1200)}"
        for c in comments
    ) or "(no recent comments)"

    return f"""You are grooming a GitHub issue for an autonomous engineering work queue.

Return ONLY compact JSON with this exact schema:
{{
  "recommendation": "ready" | "escalated" | "needs-human" | "needs-info" | "decompose" | "keep-backlog",
  "lane": "normal" | "escalated" | "backlog",
  "confidence": "high" | "medium" | "low",
  "reason": "short reason",
  "summary": "operator-facing grooming summary",
  "likelyFiles": ["path or area"],
  "acceptanceCriteria": ["testable criterion"],
  "validation": ["command/check to run"],
  "nextAction": "specific next action if not ready, otherwise implementation start guidance"
}}

Rules:
- "ready" means concrete, scoped, and safe for a normal worker to implement.
- "escalated" means actionable but needs higher-judgment implementation/design/security/API review.
- "decompose" means this is an umbrella/audit parent that should stay backlog until the deterministic audit-decomposer workflow creates child issues.
- "needs-info" means a human/repo owner must clarify missing requirements.
- "needs-human" means policy/security/product judgment is required before agent work.
- "keep-backlog" means intentionally parked; provide a concrete nextAction and why.
- Do not choose ready if key acceptance criteria or scope are missing.
- Do not choose keep-backlog silently; every non-ready answer needs a clear nextAction.
- Prefer normal ready for bounded code/docs/test/CI fixes with enough details.

Issue:
repo: {repo}
number: {number}
url: {issue_url(issue)}
title: {issue_title(issue)}
state: {issue.get('state')}
currentLane: {normalize_lane(issue.get('currentLane')) or 'normal'}
status: {issue_status(issue) or 'no-status'}
labels: {', '.join(labels) or '(none)'}
updatedAt: {issue_updated_at(issue) or '(unknown)'}

Body:
{truncate_text(issue_body(issue), 7000) or '(no body)'}

Recent comments:
{truncate_text(comments_text, 5000)}
"""


def _run_llm(prompt: str, model: str | None, command: str | None, timeout: int) -> str:
    """Single LLM attempt; returns stdout or raises RuntimeError."""
    if command:
        if GPT_MODEL_RE.search(command):
            raise RuntimeError("Refusing to use a GPT command for backlog grooming.")
        proc = subprocess.run(
            command, input=prompt, capture_output=True, text=True, shell=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "LLM command failed").strip()[:1000])
        return proc.stdout.strip()
    else:
        if GPT_MODEL_RE.search(model or ""):
            raise RuntimeError("Refusing to use a GPT model for backlog grooming.")
        # Call litellm proxy directly via HTTP instead of openclaw capability model run
        import requests as _http_lib
        litellm_url = os.environ.get("LITELLM_PROXY_URL", "http://litellm.llm:4000/v1/chat/completions")
        litellm_key = os.environ.get("LITELLM_API_KEY", "")
        # Translate litellm/ prefix for proxy
        # Translate model name for litellm proxy
        litellm_model = (model or "self-hosted").replace("litellm/", "")
        try:
            resp = _http_lib.post(
                litellm_url,
                headers={"Authorization": f"Bearer {litellm_key}", "Content-Type": "application/json"},
                json={"model": litellm_model, "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
            resp.raise_for_status()
            result = resp.json()
            msg = result["choices"][0]["message"]
            text = msg.get("content") or msg.get("reasoning_content") or ""
            return text.strip()
        except _http_lib.exceptions.Timeout:
            raise RuntimeError(f"LLM request timed out after {timeout}s")
        except _http_lib.exceptions.HTTPError as e:
            body = e.response.text[:500] if e.response is not None else str(e)
            raise RuntimeError(f"LLM HTTP {e.response.status_code}: {body}")
        except Exception as e:
            raise RuntimeError(f"LLM HTTP failed: {e}")


def run_backlog_grooming_llm(prompt: str, timeout: int = 900) -> str:
    primary_model = BACKLOG_GROOMING_MODEL
    fallback_model = "litellm/self-hosted"
    try:
        return _run_llm(prompt, primary_model, BACKLOG_GROOMING_COMMAND or None, timeout)
    except RuntimeError as e:
        err_text = str(e).lower()
        is_network = any(k in err_text for k in ("network", "connection", "timeout", "failover", "gatewayclientrequesterror"))
        if not is_network:
            raise
        print(f"      [!] primary LLM failed ({e}); trying fallback ({fallback_model})")
        try:
            return _run_llm(prompt, fallback_model, None, timeout)
        except RuntimeError as e2:
            raise RuntimeError(f"Primary ({primary_model}) failed: {e}; Fallback ({fallback_model}) failed: {e2}") from e2


def parse_backlog_grooming_result(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        text = match.group(0)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM result was not a JSON object")

    recommendation = str(data.get("recommendation") or "").strip().lower()
    lane = normalize_lane(str(data.get("lane") or "")) or "backlog"
    confidence = str(data.get("confidence") or "medium").strip().lower()
    if recommendation not in BACKLOG_RECOMMENDATIONS:
        raise ValueError(f"invalid recommendation: {recommendation!r}")
    if lane not in VALID_LANES:
        raise ValueError(f"invalid lane: {lane!r}")
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"

    data["recommendation"] = recommendation
    data["lane"] = lane
    data["confidence"] = confidence
    for key in ["likelyFiles", "acceptanceCriteria", "validation"]:
        value = data.get(key)
        if not isinstance(value, list):
            data[key] = []
        else:
            data[key] = [str(item) for item in value[:8]]
    for key in ["reason", "summary", "nextAction"]:
        data[key] = str(data.get(key) or "").strip()
    return data


def grooming_comment_body(result: dict[str, Any], *, applied: bool) -> str:
    def section(title: str, body: str | list[str]) -> str:
        if isinstance(body, list):
            if not body:
                return ""
            body = "\n".join(f"- {item}" for item in body)
        if not body or body == "(none)":
            return ""
        return f"\n### {title}\n{body}"

    applied_line = "Applied by Saffron groomer." if applied else "Dry-run/generated by Saffron groomer; no status changes applied."
    parts = [
        f"{BACKLOG_GROOMING_MARKER}",
        "## Saffron backlog grooming note",
        "",
        applied_line,
        "",
        f"**Recommendation:** `{result['recommendation']}`",
        f"**Lane:** `{result['lane']}`",
        f"**Confidence:** `{result['confidence']}`",
        "",
        f"**Reason:** {result.get('reason') or '(none)'}" + section("Summary", result.get("summary") or "") + section("Likely files / areas", result.get("likelyFiles") or []) + section("Acceptance criteria", result.get("acceptanceCriteria") or []) + section("Validation", result.get("validation") or []) + section("Next action", result.get("nextAction") or ""),
    ]
    return "\n".join(parts)


def comment_on_issue(repo: str, number: int, body: str) -> bool:
    r = gh(["issue", "comment", "--repo", repo, str(number), "--body", body], timeout=60)
    if r.returncode != 0:
        print(f"      [!] failed to comment: {(r.stderr or r.stdout).strip()[:300]}")
        return False
    return True


def backlog_grooming_candidates(issues: list[dict[str, Any]], *, include_no_status: bool) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for issue in issues:
        if not is_open(issue):
            continue
        repo = repo_full_name(issue)
        number = issue_number(issue)
        if repo not in TRACKED_REPOS or number is None:
            continue
        if is_renovate_issue(issue):
            continue
        labels = issue_labels(issue)
        if any(label.startswith("agent/") for label in labels):
            continue
        if issue_status(issue) in {"status/in-progress", "status/in-review", "status/done", "status/ready"}:
            continue
        status = issue_status(issue)
        if status == "status/backlog" or (include_no_status and status is None):
            candidates.append(issue)

    def sort_key(issue: dict[str, Any]) -> tuple[int, int, str]:
        labels = issue_labels(issue)
        priority_rank = 9
        for idx, priority in enumerate(["priority/p0", "priority/p1", "priority/p2", "priority/p3"]):
            if priority in labels:
                priority_rank = idx
                break
        status_rank = 0 if issue_status(issue) == "status/backlog" else 1
        return (priority_rank, status_rank, issue_updated_at(issue))

    return sorted(candidates, key=sort_key)


def apply_backlog_grooming(issue: dict[str, Any], result: dict[str, Any], *, comment: bool) -> bool:
    repo = repo_full_name(issue)
    number = issue_number(issue)
    issue_id = issue.get("id")
    if not repo or number is None or not issue_id:
        return False

    snapshot = github_issue_snapshot(repo, number)
    if snapshot is None:
        return False
    if str(snapshot.get("state") or "").upper() != "OPEN":
        print("      live issue is closed; skipping Dispatch updates and comments")
        return False
    live_status = live_issue_status(snapshot)
    if live_status in {"status/in-progress", "status/in-review", "status/done", "status/ready"}:
        print(f"      live issue is {live_status}; skipping Dispatch updates and comments")
        return False

    ok = True
    desired_lane = result["lane"]
    current_lane = normalize_lane(issue.get("currentLane")) or "normal"
    if desired_lane != current_lane:
        ok = classify_dispatch_issue(
            str(issue_id),
            desired_lane,
            result.get("reason") or "Backlog grooming classification",
            confidence=result.get("confidence") or "medium",
            model="saffron-backlog-groomer",
        ) and ok

    if result["recommendation"] in {"ready", "escalated"}:
        ok = set_dispatch_status(issue, "ready", "LLM-assisted backlog grooming marked issue claimable") and ok

    if comment:
        live_issue = issue_with_live_github_state(issue, snapshot)
        should_comment, reason = should_comment_on_grooming(live_issue, result)
        if should_comment:
            ok = comment_on_issue(repo, number, grooming_comment_body(result, applied=True)) and ok
        else:
            print(f"      grooming comment skipped: {reason}")

    return ok


def write_backlog_grooming_report(records: list[dict[str, Any]], report_path: str | None = None) -> Path:
    BACKLOG_GROOMING_REPORTS.mkdir(parents=True, exist_ok=True)
    if report_path:
        path = Path(report_path)
    else:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = BACKLOG_GROOMING_REPORTS / f"backlog-grooming-{stamp}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def groom_backlog_issues(
    issues: list[dict[str, Any]],
    *,
    apply: bool,
    max_items: int,
    force: bool,
    include_no_status: bool,
    comment: bool,
    report_path: str | None,
) -> tuple[int, int, int, Path | None]:
    state = load_backlog_grooming_state()
    issue_state = state.setdefault("issues", {})
    candidates = backlog_grooming_candidates(issues, include_no_status=include_no_status)

    print(f"  Backlog grooming candidates: {len(candidates)}")
    records: list[dict[str, Any]] = []
    investigated = 0
    applied = 0
    surfaced = 0
    human_attention_records: list[dict[str, Any]] = []

    for issue in candidates:
        repo = repo_full_name(issue)
        number = issue_number(issue)
        if number is None:
            continue
        key = f"{repo}#{number}"
        previous = issue_state.get(key) if isinstance(issue_state, dict) else None

        # Fetch live GitHub state FIRST to compute current fingerprint
        # before checking whether a prior handled result still applies.
        snapshot = github_issue_snapshot(repo, number)
        if snapshot is None:
            print(f"  [{key}] live issue state unavailable; skipping")
            continue
        live_state = str(snapshot.get("state") or "").upper()
        if live_state != "OPEN":
            issue_state[key] = {
                "fingerprint": f"{issue_updated_at(issue)}|{issue_status(issue) or 'no-status'}|{normalize_lane(issue.get('currentLane')) or 'normal'}",
                "recommendation": "skip-closed",
                "lane": normalize_lane(issue.get("currentLane")) or "backlog",
                "skippedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                "skipReason": f"GitHub issue is {live_state.lower()}",
            }
            continue
        live_status = live_issue_status(snapshot)
        if live_status in {"status/in-progress", "status/in-review", "status/done", "status/ready"}:
            print(f"  [{key}] live issue is {live_status}; skipping")
            issue_state[key] = {
                "fingerprint": f"{issue_updated_at(issue)}|{issue_status(issue) or 'no-status'}|{normalize_lane(issue.get('currentLane')) or 'normal'}",
                "recommendation": "skip-live-status",
                "lane": normalize_lane(issue.get("currentLane")) or "backlog",
                "skippedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                "skipReason": f"GitHub issue is {live_status}",
            }
            continue

        # Compute fingerprint from live state — this is what we compare against
        # the stored fingerprint. Previously the snapshot was fetched AFTER the
        # already-handled check, so state changes could be missed.
        issue = issue_with_live_github_state(issue, snapshot)
        fingerprint = f"{issue_updated_at(issue)}|{issue_status(issue) or 'no-status'}|{normalize_lane(issue.get('currentLane')) or 'normal'}"

        # Skip if force is off AND issue was previously handled (applied/dry-run/skipped)
        # WITHOUT a prior fingerprint check. Now fingerprint IS checked first.
        if not force and backlog_grooming_already_handled(previous):
            prior_fp = previous.get("fingerprint") if isinstance(previous, dict) else None
            if prior_fp == fingerprint:
                print(f"  [{key}] already groomed and unchanged; skipping")
                continue
            # Fingerprint changed — re-groom normally (don't skip)

        if max_items >= 0 and investigated >= max_items:
            break

        if not force and isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
            if not apply or previous.get("appliedAt"):
                print(f"  [{key}] unchanged since last grooming; skipping")
                continue

        print(f"  [{key}] investigating: {issue_title(issue)[:80]}")
        comments = github_issue_comments(repo, number)
        prompt = build_backlog_grooming_prompt(issue, comments)
        try:
            raw = run_backlog_grooming_llm(prompt)
            result = parse_backlog_grooming_result(raw)
        except Exception as e:
            print(f"      [!] grooming LLM failed: {e}")
            result = {
                "recommendation": "needs-human",
                "lane": normalize_lane(issue.get("currentLane")) or "backlog",
                "confidence": "low",
                "reason": f"LLM grooming failed: {e}",
                "summary": "",
                "likelyFiles": [],
                "acceptanceCriteria": [],
                "validation": [],
                "nextAction": "Rerun backlog grooming after LLM is healthy.",
            }

        investigated += 1
        if result["recommendation"] in HUMAN_ATTENTION_RECOMMENDATIONS:
            surfaced += 1

        record = {
            "repo": repo,
            "number": number,
            "title": issue_title(issue),
            "url": issue_url(issue),
            "labels": issue.get("labels") or [],
            "status": issue_status(issue),
            "currentLane": normalize_lane(issue.get("currentLane")) or "normal",
            "fingerprint": fingerprint,
            "result": result,
            "applied": False,
        }

        if apply:
            if comment and existing_grooming_comment(repo, number) and not force:
                print("      existing grooming comment found; not adding duplicate comment")
                should_comment = False
            else:
                should_comment = comment
            if should_comment:
                should_comment, reason = should_comment_on_grooming(issue, result)
                if not should_comment:
                    print(f"      grooming comment skipped: {reason}")
            if apply_backlog_grooming(issue, result, comment=should_comment):
                applied += 1
                record["applied"] = True
                issue_state[key] = {
                    "fingerprint": fingerprint,
                    "recommendation": result["recommendation"],
                    "lane": result["lane"],
                    "appliedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
        else:
            issue_state[key] = {
                "fingerprint": fingerprint,
                "recommendation": result["recommendation"],
                "lane": result["lane"],
                "dryRunAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

        print(f"      recommendation={result['recommendation']} lane={result['lane']} confidence={result['confidence']}")
        print(f"      reason={result.get('reason') or '(none)'}")
        if result["recommendation"] in HUMAN_ATTENTION_RECOMMENDATIONS:
            human_attention_records.append(record)
        records.append(record)

    report = write_backlog_grooming_report(records, report_path) if records else None
    save_backlog_grooming_state(state)
    if report:
        print(f"  Backlog grooming report: {report}")
    print(f"  Backlog grooming human attention: {len(human_attention_records)}")
    for record in human_attention_records:
        result = record["result"]
        print(
            "      "
            f"{record['repo']}#{record['number']} "
            f"{result['recommendation']}: {result.get('reason') or '(no reason)'}"
        )
        if result.get("nextAction"):
            print(f"        next: {result['nextAction']}")
    return investigated, applied, surfaced, report


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


def is_audit_child_issue(issue: dict[str, Any]) -> bool:
    """Check if an issue is a decomposed child from an audit umbrella."""
    body = issue_body(issue) or ""
    return body.strip().startswith("<!-- audit-child:v1")


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
    """Set cron enabled state via openclaw CLI -- gateway does not hot-reload jobs.json."""
    import subprocess
    state = "ENABLED" if enabled else "DISABLED"
    print(f"  [*] {display_name} -> {state}")
    flag = "--enable" if enabled else "--disable"
    try:
        r = subprocess.run(
            ["openclaw", "cron", "edit", job_id, flag],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print(f"  [!] openclaw cron edit failed: {r.stderr[:200]}")
    except Exception as e:
        print(f"  [!] openclaw cron edit error: {e}")


def set_normal_worker_cron(enabled: bool) -> None:
    set_cron_enabled(NORMAL_WORKER_CRON_ID, enabled, "(Saffron): MC: Normal")


def reconcile_stale_done_statuses(issues: list[dict[str, Any]]) -> int:
    """Open issues must not be Done. Move stale Done statuses back to Backlog.

    Done is terminal and corresponds to a closed GitHub issue in Dispatch v0.3.
    Use Dispatch's status API so GitHub labels and the Dispatch cache remain in
    sync; do not edit status labels directly with `gh issue edit` here.

    Checks live GitHub state before mutating: if the GitHub issue is closed,
    status/done is correct and the issue is left alone. Only moves to backlog
    when the GitHub issue is confirmed open.
    """
    reconciled = 0
    skipped_closed = 0
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
        # Verify live GitHub state before mutating.
        # Dispatch cache may be stale on closure; trust GitHub over cache.
        snapshot = github_issue_snapshot(repo, number)
        if snapshot is None:
            print(f"  [{repo} #{number}] could not fetch live state; skipping stale done reconciliation")
            continue
        live_state = str(snapshot.get("state") or "").lower()
        if live_state != "open":
            # GitHub says closed — status/done is correct, do not move to backlog.
            print(f"  [{repo} #{number}] GitHub closed with status/done; skipping (done is correct)")
            skipped_closed += 1
            continue
        print(f"  [{repo} #{number}] open issue has stale status/done")
        if set_dispatch_status(issue, "backlog", "open issue cannot be Done"):
            reconciled += 1
        else:
            print("      -> failed to reconcile stale Done status")
    if skipped_closed:
        print(f"  Skipped {skipped_closed} closed issue(s) with status/done (correct state)")
    return reconciled


def reconcile_closed_issue_statuses(issues: list[dict[str, Any]]) -> int:
    """Closed GitHub issues must not remain active/claimable in Dispatch.

    Cached Dispatch issues with active status labels (status/ready,
    status/in-progress, status/in-review) are checked against live GitHub
    state. If GitHub shows the issue as closed, move it to status/done via
    Dispatch and release any active work/lease.
    """
    ACTIVE_STATUS_LABELS = {"status/ready", "status/in-progress", "status/in-review"}
    reconciled = 0
    for issue in issues:
        labels = issue_labels(issue)
        # Only interested in issues that look active in Dispatch
        if not (labels & ACTIVE_STATUS_LABELS):
            continue
        repo = repo_full_name(issue)
        number = issue_number(issue)
        if not repo or number is None:
            continue
        # Skip closed Dispatch issues (already reflected as Done, or closed externally)
        if not is_open(issue):
            continue
        # Fetch live GitHub state
        snapshot = github_issue_snapshot(repo, number)
        if snapshot is None:
            print(f"  [!] Could not fetch live state for {repo} #{number}; skipping")
            continue
        if str(snapshot.get("state") or "").lower() != "closed":
            continue
        print(f"  [{repo} #{number}] GitHub closed but Dispatch has active status; reconciling to done")
        if set_dispatch_status(issue, "done", "GitHub closed, was active in Dispatch"):
            reconciled += 1
        else:
            print("      -> failed to set status/done")
        # Attempt to release any active work/lease for this issue.
        # Try all worker identities — Dispatch will ignore unknown agentName values.
        issue_id = issue.get("id")
        for agent in ("saffron", "saffron-normal", "saffron-escalated"):
            try:
                dispatch_request(
                    "/api/agent-work",
                    method="POST",
                    payload={
                        "action": "release",
                        "agentName": agent,
                        "issueId": issue_id,
                        "reason": "GitHub issue is closed; releasing stale work",
                    },
                    timeout=15,
                )
            except Exception as e:
                print(f"      -> work release for {agent} failed (non-fatal): {e}")
    return reconciled

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
    if is_audit_child_issue(issue):
        return "normal", "high", "Concrete audit child issue — actionable follow-up from decomposed parent"
    if has_large_audit_findings(issue):
        return "backlog", "high", "Audit umbrella — awaiting audit-decomposer workflow (do not assign to escalated worker)"
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

        status = issue_status(issue)
        if status in {"status/in-progress", "status/in-review", "status/done"}:
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
            if desired_lane == "normal" and is_audit_child_issue(issue):
                set_dispatch_status(issue, "ready", f"Audit child — {reason}")
    return changed


def manage_crons() -> tuple[int, int]:
    normal_queue = get_dispatch_queue("normal")
    escalated_queue = get_dispatch_queue("escalated")
    queued_normal_pr_fixes = queued_pr_fixes("normal")
    queued_escalated_pr_fixes = queued_pr_fixes("escalated")
    queued_human_pr_fixes = queued_pr_fixes("needs-human", include_blocked=True)

    print(f"  Dispatch normal queue: {len(normal_queue)}")
    print(f"  Dispatch escalated queue: {len(escalated_queue)}")
    print(f"  Queued normal PR fixes: {len(queued_normal_pr_fixes)}")
    print(f"  Queued escalated PR fixes: {len(queued_escalated_pr_fixes)}")
    if queued_human_pr_fixes:
        print(f"  Blocked PR fixes needing human review: {len(queued_human_pr_fixes)}")

    if normal_queue or queued_normal_pr_fixes:
        print("  -> Keeping normal worker cron enabled")
        set_normal_worker_cron(True)
    else:
        print("  -> No normal Dispatch work — disabling normal worker cron")
        set_normal_worker_cron(False)

    escalated_ready = [i for i in escalated_queue if i.get("status") == "status/ready"]
    escalated_paused = (
        Path("/home/node/.openclaw/workspace-saffron/.state/escalated_paused").exists()
        or Path("/home/node/.openclaw/workspace-saffron/.state/varka_paused").exists()
    )
    if escalated_paused:
        print("  -> Escalated worker manually paused (flag file present) — keeping disabled")
        set_cron_enabled(ESCALATED_WORKER_CRON_ID, False, "(Saffron): MC: Escalated")
    elif escalated_ready or queued_escalated_pr_fixes:
        print("  Escalated queue items:")
        for item in escalated_ready[:10]:
            print(f"      {item.get('repoFullName', '?')} #{item.get('number', '?')}: {str(item.get('title') or '')[:70]}")
        print("  -> Keeping escalated worker cron enabled")
        set_cron_enabled(ESCALATED_WORKER_CRON_ID, True, "(Saffron): MC: Escalated")
    else:
        print("  -> No escalated Dispatch work — disabling escalated worker cron")
        set_cron_enabled(ESCALATED_WORKER_CRON_ID, False, "(Saffron): MC: Escalated")

    return len(normal_queue), len(escalated_queue)


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch-first grooming for Saffron queues")
    parser.add_argument("--no-sync", action="store_true", help="Skip Dispatch issue sync before grooming")
    parser.add_argument("--list-tracked-repos", action="store_true", help="Print enabled tracked repos from Dispatch and exit")
    parser.add_argument("--groom-backlog", action="store_true", help="Run LLM-assisted backlog investigation/enrichment pass")
    parser.add_argument("--groom-backlog-use-llm", action="store_true", help="Explicitly allow the LLM-backed backlog grooming path")
    parser.add_argument("--groom-backlog-only", action="store_true", help="Only run backlog grooming; skip normal closure/lane/cron mutations")
    parser.add_argument("--groom-backlog-apply", action="store_true", help="Apply backlog grooming recommendations through Dispatch/GitHub")
    parser.add_argument("--groom-backlog-max", type=int, default=5, help="Maximum backlog items to investigate (-1 for all)")
    parser.add_argument("--groom-backlog-force", action="store_true", help="Re-groom issues even if unchanged since last pass")
    parser.add_argument("--groom-backlog-include-no-status", action="store_true", help="Also investigate no-status non-Renovate issues")
    parser.add_argument("--groom-backlog-no-comment", action="store_true", help="Do not add GitHub enrichment comments when applying")
    parser.add_argument("--groom-backlog-report", help="Write backlog grooming JSONL report to this path")
    args = parser.parse_args()

    if args.groom_backlog and not args.groom_backlog_use_llm:
        print(
            "  [!] --groom-backlog is an intelligence workflow. "
            "Pass --groom-backlog-use-llm explicitly to run it."
        )
        return 2

    global TRACKED_REPOS
    TRACKED_REPOS = get_tracked_repos()

    if args.list_tracked_repos:
        print("\n".join(TRACKED_REPOS))
        return 0

    print("[*] Grooming Dispatch queues...")
    print(f"[*] Tracked repos from Dispatch: {len(TRACKED_REPOS)}")

    if not args.no_sync:
        print("[*] Requesting Dispatch scheduled sync...")
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

    if args.groom_backlog_only:
        if not args.groom_backlog:
            print("  [!] --groom-backlog-only requires --groom-backlog")
            return 2
        print("\n[*] LLM-assisted backlog grooming only...")
        print("  Mode: APPLY (Dispatch/GitHub mutations enabled)" if args.groom_backlog_apply else "  Mode: DRY-RUN (no Dispatch/GitHub mutations)")
        backlog_groomed, backlog_promoted, backlog_surfaced, _report = groom_backlog_issues(
            issues,
            apply=args.groom_backlog_apply,
            max_items=args.groom_backlog_max,
            force=args.groom_backlog_force,
            include_no_status=args.groom_backlog_include_no_status,
            comment=not args.groom_backlog_no_comment,
            report_path=args.groom_backlog_report,
        )
        print("\nSummary:")
        print(
            f"  dispatch:{len(issues)} cached,{open_count} open,{backlog_groomed} "
            f"backlog_investigated,{backlog_promoted} backlog_applied,{backlog_surfaced} "
            "backlog_surfaced_not_ready"
        )
        return 0

    print("\n[*] Checking merged PR closures from GitHub...")
    merged_prs = {}
    for repo in TRACKED_REPOS:
        merged_prs[repo] = get_merged_prs_for_repo(repo)
        print(f"      {repo}: {len(merged_prs[repo])} merged PRs referencing issues")
    closed = close_resolved_issues(issues, merged_prs)
    print(f"  Closed issues: {closed}")

    print("\n[*] Reconciling stale Done statuses on open issues...")
    reconciled_statuses = reconcile_stale_done_statuses(issues)
    print(f"  Reconciled stale Done statuses: {reconciled_statuses}")

    closed_active_reconciled = reconcile_closed_issue_statuses(issues)
    print(f"  Reconciled closed-active statuses: {closed_active_reconciled}")

    if closed or reconciled_statuses or closed_active_reconciled:
        print("\n[*] Re-syncing Dispatch after GitHub/status mutations...")
        dispatch_sync()
        issues = get_all_dispatch_issues()

    print("\n[*] Reconciling Dispatch lane assignments...")
    changed_lanes = reconcile_lanes(issues)
    print(f"  Lane updates: {changed_lanes}")

    backlog_groomed = 0
    backlog_promoted = 0
    backlog_surfaced = 0
    if args.groom_backlog:
        print("\n[*] LLM-assisted backlog grooming...")
        if args.groom_backlog_apply:
            print("  Mode: APPLY (Dispatch/GitHub mutations enabled)")
        else:
            print("  Mode: DRY-RUN (no Dispatch/GitHub mutations)")
        backlog_groomed, backlog_promoted, backlog_surfaced, _report = groom_backlog_issues(
            issues,
            apply=args.groom_backlog_apply,
            max_items=args.groom_backlog_max,
            force=args.groom_backlog_force,
            include_no_status=args.groom_backlog_include_no_status,
            comment=not args.groom_backlog_no_comment,
            report_path=args.groom_backlog_report,
        )
        print(
            f"  Backlog grooming: investigated={backlog_groomed} "
            f"applied={backlog_promoted} surfaced_not_ready={backlog_surfaced}"
        )

    print("\n[*] Managing crons from Dispatch queues...")
    normal_count, escalated_count = manage_crons()

    print("\nSummary:")
    print(
        f"  dispatch:{len(issues)} cached,{open_count} open,{normal_count} normal_queue,"
        f"{escalated_count} escalated_queue,{closed} closed,{reconciled_statuses} "
        f"stale_done_reconciled,{closed_active_reconciled} closed_active_reconciled,"
        f"{changed_lanes} lane_updates,{backlog_groomed} "
        f"backlog_investigated,{backlog_promoted} backlog_applied,{backlog_surfaced} "
        "backlog_surfaced_not_ready"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
