#!/usr/bin/env python3
"""Deterministically decompose weekly audit umbrella issues.

Dry-run is the default:
  scripts/audit_decompose.py --repo misospace/dispatch --issue-number 235

Apply mutations:
  scripts/audit_decompose.py --repo misospace/dispatch --issue-number 235 --apply

Scan tracked repos:
  scripts/audit_decompose.py --scan --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


API_ROOT = "https://api.github.com"
DECOMPOSE_OPEN = "<!-- audit-decompose:v1 -->"
DECOMPOSE_CLOSE = "<!-- /audit-decompose:v1 -->"
TITLE_MAX = 120
PRIORITY_RE = re.compile(r"^\s*(?:\*\*)?\[?P([0-3])\]?\s*(?:[-:\u2013\u2014]|--)\s*", re.I)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# A section runs until the next H2 heading. The '### [Pn] Title' recommendation
# blocks are H3 children of '## Recommended Issue Breakdown', so the boundary must
# NOT stop at H3 or the section would truncate to empty before any block.
SECTION_BOUNDARY_RE = re.compile(r"^##\s+", re.M)
ISSUE_REF_RE = re.compile(r"#\d+")


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    html_url: str
    labels: list[str]


@dataclass(frozen=True)
class ChildCandidate:
    raw: str
    title: str
    priority: int | None
    key: str


@dataclass(frozen=True)
class ChildResult:
    number: int
    title: str
    url: str
    action: str


def fail(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        proc = None
    if proc and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    fail("GITHUB_TOKEN is not set and `gh auth token` did not return a token")


def dispatch_url() -> str:
    return os.environ.get("DISPATCH_URL", "http://dispatch.llm:3000").rstrip("/")


def dispatch_token() -> str:
    token = os.environ.get("DISPATCH_AGENT_TOKEN")
    if not token:
        fail("DISPATCH_AGENT_TOKEN is not set")
    return token


def api_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = dict(headers or {})
    if payload is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        print(f"HTTP {exc.code} {method} {url}", file=sys.stderr)
        print(raw[:2000] or "(empty response body)", file=sys.stderr)
        raise


def gh_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    url = f"{API_ROOT}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
    return api_json(
        url,
        method=method,
        payload=payload,
        headers={
            "Authorization": f"Bearer {github_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def gh_paginated(path: str, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        q = dict(query or {})
        q.update({"per_page": 100, "page": page})
        batch = gh_json(path, query=q)
        if not isinstance(batch, list):
            fail(f"Expected list response from GitHub endpoint {path}")
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def fetch_issue(repo: str, issue_number: int) -> Issue:
    data = gh_json(f"/repos/{repo}/issues/{issue_number}")
    return Issue(
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        state=str(data.get("state") or ""),
        html_url=str(data.get("html_url") or ""),
        labels=[str(label.get("name")) for label in data.get("labels", [])],
    )


def list_labels(repo: str) -> set[str]:
    return {str(label["name"]) for label in gh_paginated(f"/repos/{repo}/labels")}


def strip_markdown(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(text: str) -> str:
    text = strip_markdown(text)
    text = PRIORITY_RE.sub("", text, count=1).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:TITLE_MAX].rstrip(" .:-\u2013\u2014")


def parse_priority(text: str) -> int | None:
    match = PRIORITY_RE.search(strip_markdown(text))
    if not match:
        return None
    return int(match.group(1))


def title_from_item(raw: str) -> tuple[str, int | None]:
    priority = parse_priority(raw)
    bold = BOLD_RE.search(raw)
    if bold:
        title_source = bold.group(1)
    else:
        cleaned = strip_markdown(raw)
        colon = cleaned.find(":")
        if 0 < colon <= 100:
            title_source = cleaned[:colon]
        else:
            title_source = cleaned
    return normalize_title(title_source), priority


def extract_section(body: str, heading: str) -> str | None:
    pattern = re.compile(rf"^#{{2,3}}\s+{re.escape(heading)}\s*$", re.I | re.M)
    match = pattern.search(body)
    if not match:
        return None
    start = match.end()
    next_match = SECTION_BOUNDARY_RE.search(body, start)
    end = next_match.start() if next_match else len(body)
    return body[start:end].strip()


def parse_numbered_items(section: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    for line in section.splitlines():
        match = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if match:
            if current:
                items.append("\n".join(current).strip())
            current = [match.group(1).strip()]
            continue
        if current and (line.startswith("   ") or line.startswith("\t")):
            current.append(line.strip())
        elif current and not line.strip():
            continue
        elif current:
            items.append("\n".join(current).strip())
            current = []
    if current:
        items.append("\n".join(current).strip())
    return [item for item in items if item]


def split_priority_heading(text: str) -> tuple[int | None, str]:
    """Split a leading priority marker off a recommendation heading/title.

    Handles the v2 contract's '[P1] Title' as well as 'P1 - Title',
    'P1 — Title', and a plain 'Title' with no priority. Returns
    (priority, remaining-title)."""
    stripped = strip_markdown(text)
    match = re.match(r"^\[?P([0-3])\]?\s*(?:[-:–—]+\s*)?(.*)$", stripped, re.I)
    if match and match.group(2).strip():
        return int(match.group(1)), match.group(2).strip()
    return None, stripped


def parse_recommendations(section: str) -> list[tuple[str, int | None, str]]:
    """Parse the Recommended-issue-breakdown section into
    (title, priority, body) triples.

    v2 strict contract: one '### [Pn] Title' block per issue, body carrying the
    Problem/Evidence/Acceptance fields, which become the child issue verbatim.
    Legacy fallback: a numbered list of '**Pn — Title**' items (older umbrellas
    predating the contract). No cross-referencing of any other section — a
    recommendation is fully self-contained, so nothing outside the breakdown
    can leak into a child (the bug that stapled whole priority buckets in)."""
    blocks = [b.strip() for b in re.split(r"(?m)^(?=###\s+)", section) if b.strip().startswith("###")]
    out: list[tuple[str, int | None, str]] = []
    for block in blocks:
        lines = block.splitlines()
        heading = re.sub(r"^#{2,3}\s+", "", lines[0]).strip()
        priority, title_source = split_priority_heading(heading)
        # v2 findings are '### [Pn] Title'. A '###' block without a priority marker
        # is prose, not a recommendation — e.g. a sibling '### Not worth doing yet'
        # that the H2-only section boundary swept in. Skip it so it can never become
        # a child, and so it does not suppress the legacy numbered-list fallback.
        if priority is None:
            continue
        title = normalize_title(title_source)
        body = "\n".join(lines[1:]).strip()
        out.append((title, priority, body))
    if out:
        return out
    out = []
    for raw in parse_numbered_items(section):
        title, priority = title_from_item(raw)
        out.append((title, priority, raw))
    return out


def stable_key(repo: str, issue_number: int, title: str) -> str:
    raw = f"{repo}#{issue_number}:{title.lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def parse_candidates(repo: str, parent: Issue) -> tuple[list[ChildCandidate], str]:
    section = extract_section(parent.body, "Recommended issue breakdown")
    if not section:
        return [], "none"

    candidates: list[ChildCandidate] = []
    seen: set[str] = set()
    for title, priority, body in parse_recommendations(section):
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        key = stable_key(repo, parent.number, title)
        candidates.append(
            ChildCandidate(raw=body, title=title, priority=priority, key=key)
        )
    return candidates, "breakdown"


def extract_audit_date(parent: Issue) -> str | None:
    match = re.search(r"Audit date:\s*\*\*([^*]+)\*\*", parent.body, re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"(\d{4}-\d{2}-\d{2})", parent.title)
    if match:
        return match.group(1)
    return None


def desired_labels(candidate: ChildCandidate, repo_labels: set[str]) -> tuple[list[str], list[str]]:
    desired = ["audit", "status/backlog"]
    if candidate.priority is not None:
        desired.append(f"priority/p{candidate.priority}")
    present = [label for label in desired if label in repo_labels]
    missing = [label for label in desired if label not in repo_labels]
    return present, missing


def child_marker(repo: str, parent_number: int, key: str) -> str:
    return f"<!-- audit-child:v1 parent={repo}#{parent_number} key={key} -->"


def child_body(repo: str, parent: Issue, candidate: ChildCandidate) -> str:
    parts = [
        child_marker(repo, parent.number, candidate.key),
        "",
        f"Parent umbrella issue: {parent.html_url}",
        f"Source audit: {parent.title}",
    ]
    audit_date = extract_audit_date(parent)
    if audit_date:
        parts.append(f"Source audit date: {audit_date}")
    parts.extend(
        [
            "",
            "## Recommendation",
            "",
            candidate.raw.strip(),
        ]
    )
    return "\n".join(parts).rstrip() + "\n"


def existing_child(repo: str, marker: str) -> dict[str, Any] | None:
    result = gh_json(
        "/search/issues",
        query={"q": f'repo:{repo} "{marker}" in:body'},
    )
    for issue in result.get("items", []):
        if marker in str(issue.get("body") or ""):
            return issue
    return None


def find_open_issue_by_title(repo: str, title: str) -> dict[str, Any] | None:
    """Find an existing open issue in the repo with an exact title match.
    Used for cross-umbrella dedup so concurrent audit umbrellas don't create
    duplicate child issues for the same finding.
    """
    # GitHub search treats quotes as a phrase query
    q = f'repo:{repo} is:issue is:open in:title "{title}"'
    result = gh_json("/search/issues", query={"q": q})
    for issue in result.get("items", []):
        if str(issue.get("title") or "").strip().lower() == title.strip().lower():
            return issue
    return None

def same_labels(current: list[dict[str, Any]], desired: list[str]) -> bool:
    return {str(label.get("name")) for label in current} == set(desired)


def create_or_update_child(
    repo: str,
    parent: Issue,
    candidate: ChildCandidate,
    labels: list[str],
    apply: bool,
) -> ChildResult:
    marker = child_marker(repo, parent.number, candidate.key)
    body = child_body(repo, parent, candidate)
    found = existing_child(repo, marker)

    if found:
        number = int(found["number"])
        url = str(found["html_url"])
        needs_update = (
            str(found.get("title") or "") != candidate.title
            or str(found.get("body") or "") != body
            or not same_labels(list(found.get("labels") or []), labels)
        )
        if not apply:
            action = "would-update" if needs_update else "unchanged"
            return ChildResult(number=number, title=candidate.title, url=url, action=action)
        if needs_update:
            updated = gh_json(
                f"/repos/{repo}/issues/{number}",
                method="PATCH",
                payload={"title": candidate.title, "body": body, "labels": labels},
            )
            return ChildResult(
                number=int(updated["number"]),
                title=candidate.title,
                url=str(updated["html_url"]),
                action="updated",
            )
        return ChildResult(number=number, title=candidate.title, url=url, action="unchanged")

    if not apply:
        return ChildResult(number=0, title=candidate.title, url="", action="would-create")

    # Cross-umbrella dedup: before creating, check for an existing open issue
    # with the same title in this repo (another umbrella may have already
    # decomposed the same finding). This prevents triplication when multiple
    # audit umbrellas exist for the same week.
    existing = find_open_issue_by_title(repo, candidate.title)
    if existing:
        number = int(existing["number"])
        url = str(existing.get("html_url") or "")
        print(f"  dedup: found existing open issue #{number} with same title, skipping creation")
        return ChildResult(number=number, title=candidate.title, url=url, action="dedup-skipped")

    created = gh_json(
        f"/repos/{repo}/issues",
        method="POST",
        payload={"title": candidate.title, "body": body, "labels": labels},
    )
    return ChildResult(
        number=int(created["number"]),
        title=candidate.title,
        url=str(created["html_url"]),
        action="created",
    )


def build_parent_section(children: list[ChildResult]) -> str:
    lines = ["## Decomposed into", DECOMPOSE_OPEN]
    for child in children:
        if child.number:
            lines.append(f"- #{child.number} — {child.title}")
        else:
            lines.append(f"- [dry-run] — {child.title}")
    lines.append(DECOMPOSE_CLOSE)
    return "\n".join(lines)


def updated_parent_body(body: str, children: list[ChildResult]) -> str:
    section = build_parent_section(children)
    if DECOMPOSE_OPEN in body and DECOMPOSE_CLOSE in body:
        pattern = rf"## Decomposed into\s*\n{re.escape(DECOMPOSE_OPEN)}.*?{re.escape(DECOMPOSE_CLOSE)}"
        return re.sub(pattern, section, body, flags=re.S)
    return body.rstrip() + "\n\n" + section + "\n"


def update_parent(repo: str, parent: Issue, children: list[ChildResult], apply: bool) -> None:
    if not apply:
        print("\n[DRY-RUN] Parent body section would be:")
        print(build_parent_section(children))
        return
    new_body = updated_parent_body(parent.body, children)
    gh_json(f"/repos/{repo}/issues/{parent.number}", method="PATCH", payload={"body": new_body})


def mark_dispatch_decomposed(repo: str, parent: Issue, children: list[ChildResult]) -> None:
    child_urls = [child.url for child in children if child.url]
    endpoint = "/api/issues/actions/decompose"
    payload = {
        "repo": repo,
        "issueNumber": parent.number,
        "decomposed": True,
        "followUpUrls": child_urls,
        "note": f"Deterministic audit decomposition created/updated {len(child_urls)} child issue(s).",
        "agentName": "audit-decomposer",
        "actor": "audit-decomposer",
    }
    url = f"{dispatch_url()}{endpoint}"
    api_json(
        url,
        method="POST",
        payload=payload,
        headers={"Authorization": f"Bearer {dispatch_token()}"},
        timeout=15,
    )


def process_issue(repo: str, issue_number: int, apply: bool) -> int:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"{mode}: {repo}#{issue_number}")
    parent = fetch_issue(repo, issue_number)
    if parent.state != "open":
        print(f"Skipped: parent issue is {parent.state}")
        return 0
    if DECOMPOSE_OPEN in parent.body:
        print("Skipped: parent already contains audit-decompose marker")
        return 0

    candidates, source = parse_candidates(repo, parent)
    if not candidates:
        print("Unparseable: no parseable `## Recommended Issue Breakdown` section found")
        return 1 if apply else 0

    labels = list_labels(repo)
    print(f"Parsed {len(candidates)} child candidate(s) from {source}")
    results: list[ChildResult] = []
    missing_seen: set[str] = set()
    for candidate in candidates:
        child_labels, missing = desired_labels(candidate, labels)
        missing_seen.update(missing)
        result = create_or_update_child(repo, parent, candidate, child_labels, apply)
        results.append(result)
        label_text = ", ".join(child_labels) if child_labels else "(none)"
        print(f"- {result.action}: {candidate.title}")
        print(f"  key={candidate.key} labels={label_text}")
        if result.number:
            print(f"  issue=#{result.number} url={result.url}")

    if missing_seen:
        print(f"Skipped missing labels: {', '.join(sorted(missing_seen))}")

    update_parent(repo, parent, results, apply)
    if apply:
        mark_dispatch_decomposed(repo, parent, results)
        print("Dispatch decomposed state updated")
    return 0


def tracked_repos() -> list[str]:
    script = Path(__file__).with_name("project_groom.py")
    proc = subprocess.run(
        [sys.executable, str(script), "--list-tracked-repos"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        fail("failed to list tracked repos")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def is_candidate_parent(issue: dict[str, Any]) -> bool:
    title = str(issue.get("title") or "").lower()
    body = str(issue.get("body") or "")
    # Skip only if the marker block already lists concrete child issue references.
    # The audit-writing template includes an empty marker block with a placeholder
    # sentence, so a naive "marker present = already decomposed" check skips
    # never-decomposed umbrellas. Check for "#NNN" issue refs inside the block.
    if DECOMPOSE_OPEN in body:
        ms = body.find(DECOMPOSE_OPEN)
        me = body.find(DECOMPOSE_CLOSE, ms)
        if me < 0:
            me = len(body)
        block = body[ms + len(DECOMPOSE_OPEN):me].strip()
        if block and ISSUE_REF_RE.search(block):
            return False
    if "weekly tech debt audit" not in title and "audit" not in title:
        return False
    return bool(extract_section(body, "Recommended issue breakdown"))


def decomposed_child_numbers(body: str) -> list[int]:
    """Child issue numbers listed in the umbrella's `## Decomposed into` block."""
    if DECOMPOSE_OPEN not in body:
        return []
    start = body.find(DECOMPOSE_OPEN) + len(DECOMPOSE_OPEN)
    end = body.find(DECOMPOSE_CLOSE, start)
    if end < 0:
        end = len(body)
    numbers: list[int] = []
    for match in re.finditer(r"#(\d+)", body[start:end]):
        number = int(match.group(1))
        if number not in numbers:
            numbers.append(number)
    return numbers


def close_completed_umbrella(repo: str, issue: dict[str, Any], apply: bool) -> int:
    """Close an already-decomposed audit umbrella once every child it lists is
    closed. Umbrellas are kept open while any child is in flight (Jory tracks the
    decomposed set via the open umbrella). Never closes an umbrella that lists no
    children — that is a failed or not-yet-run decompose, not a completed one.
    Returns 1 if it closed (or, in dry-run, would close) the umbrella, else 0."""
    if "audit" not in str(issue.get("title") or "").lower():
        return 0
    children = decomposed_child_numbers(str(issue.get("body") or ""))
    if not children:
        return 0
    try:
        states = [str(gh_json(f"/repos/{repo}/issues/{n}").get("state") or "") for n in children]
    except Exception as exc:  # noqa: BLE001 — a child lookup failed; can't confirm completion
        print(f"  skip auto-close {repo}#{issue.get('number')}: child lookup failed ({exc})")
        return 0
    if not all(state == "closed" for state in states):
        return 0

    number = int(issue["number"])
    print(f"{'APPLY' if apply else 'DRY-RUN'}: {repo}#{number} — all {len(children)} children closed, closing umbrella")
    if not apply:
        return 1
    gh_json(
        f"/repos/{repo}/issues/{number}/comments",
        method="POST",
        payload={
            "body": (
                f"All {len(children)} decomposed child issues are closed — auto-closing "
                "this audit umbrella. Reopen if follow-up work remains."
            )
        },
    )
    gh_json(
        f"/repos/{repo}/issues/{number}",
        method="PATCH",
        payload={"state": "closed", "state_reason": "completed"},
    )
    return 1


def scan(apply: bool) -> int:
    failures = 0
    checked = 0
    closed = 0
    for repo in tracked_repos():
        issues = gh_paginated(f"/repos/{repo}/issues", {"state": "open"})
        for issue in issues:
            # is_candidate_parent checks title + body structure;
            # label filter removed — audit sub-agents don't
            # always apply the "audit" label, causing valid
            # umbrella issues to be silently skipped.
            if is_candidate_parent(issue):
                checked += 1
                failures += process_issue(repo, int(issue["number"]), apply)
            else:
                # Already-decomposed umbrellas fall here (their marker block lists
                # child refs). Close them once every child is done.
                closed += close_completed_umbrella(repo, issue, apply)
    print(f"Scan complete: checked={checked} closed={closed} failures={failures}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--repo", help="Repository full name, e.g. misospace/dispatch")
    target.add_argument("--scan", action="store_true", help="Scan tracked repos")
    parser.add_argument("--issue-number", type=int, help="Umbrella issue number for --repo")
    parser.add_argument("--apply", action="store_true", help="Create/update issues and Dispatch state")
    args = parser.parse_args()

    if args.repo:
        if args.issue_number is None:
            fail("--issue-number is required with --repo")
        return process_issue(args.repo, args.issue_number, args.apply)
    return scan(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
