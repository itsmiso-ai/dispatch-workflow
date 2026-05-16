#!/usr/bin/env python3
"""
research_before_task.py — Research phase helper for wishlist items.

Run BEFORE writing any code. Takes a GitHub issue and produces a research brief
covering: related commits, similar past fixes, related PRs, and external references.

Usage:
    python3 research_before_task.py <owner/repo> <issue-number>
    python3 research_before_task.py joryirving/miso-chat 411
"""

import json
import subprocess
import sys
import textwrap
import urllib.request
import urllib.parse
from pathlib import Path

GH = Path.home() / ".local/bin/gh"
GRAPHQL = "https://api.github.com/graphql"


def gh_graphql(query: str, variables: dict = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    req = urllib.request.Request(
        GRAPHQL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"bearer {subprocess.check_output([str(GH), 'auth', 'token'], text=True).strip()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def gh_rest(path: str) -> dict:
    result = subprocess.check_output([str(GH), "api", path, "--jq", "."], text=True)
    return json.loads(result) if result.strip() else {}


def get_issue(repo: str, issue_num: int) -> dict:
    data = gh_rest(f"repos/{repo}/issues/{issue_num}")
    return data


def get_issue_comments(repo: str, issue_num: int) -> list:
    data = gh_rest(f"repos/{repo}/issues/{issue_num}/comments")
    return data if isinstance(data, list) else []


def get_related_commits(repo: str, issue_num: int) -> list:
    """Find commits that touched the same files as this issue."""
    query = """
    query($repo: String!, $issue: Int!) {
      repository(owner: $repo, name: $repo.split("/")[1]) {
        issue(number: $issue) {
          title
          body
          labels(first: 10) { nodes { name } }
        }
      }
    }
    """.replace("$repo.split", "$repo .split")  # hack for eval

    owner, repo_name = repo.split("/", 1)
    variables = {"repo": owner, "repoName": repo_name, "issue": issue_num}

    # Search for commits referencing this issue or related files
    results = []
    try:
        # Get commits that mention this issue
        search = gh_rest(f"search/commits?q=is%3Apr+{issue_num}+repo%3A{repo}+in%3Atitle&per_page=5")
        if isinstance(search, dict) and "items" in search:
            for item in search["items"][:3]:
                results.append({
                    "type": "related_pr",
                    "sha": item.get("sha", "")[:8],
                    "title": item.get("title", ""),
                    "url": item.get("html_url", ""),
                })
    except Exception:
        pass

    return results


def get_recent_fixes_in_area(repo: str, issue_labels: list, issue_title: str) -> list:
    """Find recent merged PRs that fixed similar issues."""
    owner, repo_name = repo.split("/", 1)
    results = []
    try:
        label_names = [l["name"] for l in issue_labels]
        keywords = [w for w in issue_title.split() if len(w) > 4][:3]

        search_query = " ".join(keywords) + " " + " ".join(label_names[:2])
        search = gh_rest(f"search/issues?q={urllib.parse.quote(search_query)}+is%3Apr+is%3Amerged+repo%3A{repo}&sort=updated&per_page=5")
        if isinstance(search, dict) and "items" in search:
            for item in search["items"][:3]:
                results.append({
                    "type": "similar_fix",
                    "number": item.get("number", ""),
                    "title": item.get("title", ""),
                    "url": item.get("html_url", ""),
                    "labels": [l["name"] for l in item.get("labels", [])],
                })
    except Exception:
        pass

    return results


def get_issue_pr_context(repo: str, issue_num: int) -> list:
    """Find PRs that reference this issue."""
    try:
        search = gh_rest(f"search/issues?q={issue_num}+repo%3A{repo}+is%3Apr&per_page=5")
        if isinstance(search, dict) and "items" in search:
            return [{
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "url": item.get("html_url"),
                "merged": item.get("pull_request", {}).get("merged_at") is not None,
            } for item in search["items"] if "pull_request" in item]
    except Exception:
        pass
    return []


def get_code_context(repo: str, issue_body: str, issue_title: str) -> list:
    """Extract file paths and code patterns mentioned in the issue."""
    import re
    files = re.findall(r'`([^`]+\.(py|ts|js|go|rs|yaml|yml|md))`', issue_body)
    # Also look for common file paths
    paths = re.findall(r'(?:src/|lib/|pkg/|cmd/|internal/|app/)[^\s`#]+', issue_body)
    return list(set(files))[:5], list(set(paths))[:5]


def build_research_brief(repo: str, issue_num: int) -> str:
    issue = get_issue(repo, issue_num)
    if not issue or "title" not in issue:
        return f"⚠ Could not fetch issue {repo}#{issue_num}"

    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    labels = issue.get("labels", [])
    label_names = [l["name"] for l in labels] if isinstance(labels, list) else []

    lines = []
    lines.append(f"# Research Brief: {repo}#{issue_num}")
    lines.append(f"**Title:** {title}")
    if label_names:
        lines.append(f"**Labels:** {', '.join(label_names)}")
    lines.append("")

    # --- Issue context
    lines.append("## Issue Summary")
    summary = body[:500] + ("..." if len(body) > 500 else "")
    lines.append(textwrap.dedent(summary).strip())
    lines.append("")

    # --- Existing PRs referencing this issue
    prs = get_issue_pr_context(repo, issue_num)
    if prs:
        lines.append("## Existing PRs for This Issue")
        for pr in prs:
            status = "✅ MERGED" if pr.get("merged") else pr.get("state", "").upper()
            lines.append(f"  [{status}] #{pr['number']} — {pr['title']} ({pr['url']})")
        lines.append("")

    # --- Recent similar fixes
    similar = get_recent_fixes_in_area(repo, labels, title)
    if similar:
        lines.append("## Recent Similar Fixes")
        for s in similar[:3]:
            lbls = ", ".join(s.get("labels", [])[:3])
            lines.append(f"  #{s['number']} — {s['title']} [{lbls}]")
            lines.append(f"    {s['url']}")
        lines.append("")

    # --- Related commits / PRs
    related = get_related_commits(repo, issue_num)
    if related:
        lines.append("## Related Commits / PRs")
        for r in related[:3]:
            lines.append(f"  {r['sha']} — {r['title']} ({r['url']})")
        lines.append("")

    # --- Code areas mentioned
    code_files, code_paths = get_code_context(repo, body, title)
    if code_files or code_paths:
        lines.append("## Code Areas Mentioned in Issue")
        for f, p in zip(code_files, code_paths):
            lines.append(f"  `{p or f}`")
        lines.append("")

    # --- Research notes
    lines.append("## Research Notes")
    lines.append("_Fill in: what did you find? what approaches have been tried before? what does the codebase already do in this area?_")
    lines.append("")

    lines.append("## Recommended Approach")
    lines.append("_Fill in: what will you implement this run? be specific — which files, what behavior._")

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <owner/repo> <issue-number>")
        sys.exit(1)

    repo = sys.argv[1]
    try:
        issue_num = int(sys.argv[2])
    except ValueError:
        print(f"Error: issue-number must be an integer, got {sys.argv[2]}")
        sys.exit(1)

    brief = build_research_brief(repo, issue_num)
    print(brief)
