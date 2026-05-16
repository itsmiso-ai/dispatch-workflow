#!/usr/bin/env python3
"""
Add enhancement/bug/priority issues from tracked repos to the Vibe Coding Backlog project.

Usage: python3 project_backlog_sync.py [--dry-run]
"""

import os
import json
import argparse
import subprocess
import sys

GH = os.environ.get("GH", "/home/node/.local/bin/gh")
PROJECT_ID = "PVT_kwHOAsG-YM4BTyY3"
REPOS = [
    "misospace/miso-chat",
    "misospace/miso-gallery",
    "misospace/mission-control",
    "misospace/pr-reviewer-action",
    "misospace/windowstead",
]

# Labels that qualify for the kanban
QUALIFYING_LABELS = {
    "enhancement",
    "bug",
    "priority/p0",
    "priority/p1",
    "priority/p2",
    # Weekly audit issues are created by Saffron's GPT audit crons. They must
    # enter the backlog so heartbeat grooming can route them to the GPT worker
    # instead of the normal self-hosted wishlist cron.
    "audit",
    "needs-gpt",
}

# Labels to skip entirely (bots, meta, docs)
SKIP_LABELS = {"dependencies", "skip-changelog", "internal"}


def gh_graphql(query: str) -> dict:
    """Execute a GraphQL query via gh api."""
    result = subprocess.run(
        [GH, "api", "graphql", "--field", "query=" + query],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"  [!] GraphQL error: {result.stderr[:300]}", file=sys.stderr)
        return {}
    return json.loads(result.stdout)


def repo_exists(repo: str) -> bool:
    """Return True if the repo is reachable by gh."""
    result = subprocess.run(
        [GH, "repo", "view", repo, "--json", "nameWithOwner"],
        capture_output=True, text=True, timeout=15
    )
    return result.returncode == 0


def get_field_id(project_id: str, field_name: str):
    """Get the field ID and options for a given field name."""
    query = """
    {
      node(id: "%s") {
        ... on ProjectV2 {
          fields(first: 20) {
            nodes {
              ... on ProjectV2Field { id name }
              ... on ProjectV2SingleSelectField {
                id
                name
                options { id name }
              }
            }
          }
        }
      }
    }
    """ % project_id

    result = gh_graphql(query)
    if not result.get("data", {}).get("node", {}):
        return None, {}

    fields = result["data"]["node"]["fields"]["nodes"]
    status_options = {}

    for field in fields:
        if field["name"] == field_name:
            if "options" in field:
                for opt in field["options"]:
                    status_options[opt["name"]] = opt["id"]
            return field["id"], status_options

    return None, {}


def get_project_items(project_id: str) -> set:
    """Get set of issue IDs already in the project."""
    query = """
    {
      node(id: "%s") {
        ... on ProjectV2 {
          items(first: 100) {
            nodes {
              content { ... on Issue { id } }
            }
          }
        }
      }
    }
    """ % project_id

    result = gh_graphql(query)
    items = result.get("data", {}).get("node", {}).get("items", {}).get("nodes", [])
    return {item["content"]["id"] for item in items if item.get("content")}


def qualifies(issue_labels: list) -> bool:
    """Check if issue has a qualifying label."""
    label_names = {l["name"].lower() for l in issue_labels}
    if label_names & SKIP_LABELS:
        return False
    for label in label_names:
        if label in QUALIFYING_LABELS:
            return True
        if label.startswith("priority/"):
            return True
    return False


def add_issue_to_project(project_id: str, issue_id: str, status_field_id: str, status_option_id: str = None):
    """Add an issue to the project and set status to Triage."""
    # Note: contentId is the issue's node ID; itemId is for existing project items
    add_query = """
    mutation {
      addProjectV2ItemById(input: {projectId: "%s", contentId: "%s"}) {
        item { id }
      }
    }
    """ % (project_id, issue_id)

    result = gh_graphql(add_query)
    if result.get("errors"):
        print(f"    [!] Add item error: {result['errors']}")
        return None

    item_id = result.get("data", {}).get("addProjectV2ItemById", {}).get("item", {}).get("id")
    if not item_id:
        print(f"    [!] No item ID returned")
        return None

    print(f"    -> Added item")

    # Set status to Triage
    if status_field_id and status_option_id:
        status_query = """
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
        """ % (project_id, item_id, status_field_id, status_option_id)

        status_result = gh_graphql(status_query)
        if status_result.get("errors"):
            print(f"    [!] Set status error: {status_result['errors']}")
        else:
            print(f"    -> Set status to Triage")

    return item_id


def main():
    parser = argparse.ArgumentParser(description="Sync enhancement/bug/priority issues to Vibe Coding Backlog project")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be added without adding")
    parser.add_argument("--repo", action="append", help="Specific repo to scan (default: all tracked repos)")
    args = parser.parse_args()

    repos_to_scan = args.repo if args.repo else REPOS

    # Get Status field ID
    print("Fetching project field info...")
    status_field_id, status_options = get_field_id(PROJECT_ID, "Status")
    if not status_field_id:
        print("[!] Could not find Status field in project", file=sys.stderr)
        sys.exit(1)

    triage_option_id = status_options.get("Triage")
    if not triage_option_id:
        print(f"[!] Could not find 'Triage' option. Available: {list(status_options.keys())}", file=sys.stderr)
        sys.exit(1)

    print(f"  Status field ID: {status_field_id}")
    print(f"  Triage option ID: {triage_option_id[:20]}...")

    # Get existing items in project
    existing_items = get_project_items(PROJECT_ID)
    print(f"\nExisting items in project: {len(existing_items)}")

    total_added = 0
    total_skipped = 0
    total_qualified = 0

    for repo in repos_to_scan:
        if not repo_exists(repo):
            print(f"\n[*] Scanning {repo}...")
            print("  -> repo not reachable, skipping")
            continue

        owner, name = repo.split("/")
        print(f"\n[*] Scanning {repo}...")

        query = """
        {
          repository(owner: "%s", name: "%s") {
            issues(first: 50, states: OPEN) {
              nodes {
                id
                number
                title
                repository { nameWithOwner }
                labels(first: 10) { nodes { name } }
              }
            }
          }
        }
        """ % (owner, name)

        result = gh_graphql(query)
        repo_data = result.get("data", {}).get("repository") or {}
        issues = repo_data.get("issues", {}).get("nodes", [])

        repo_qualified = 0
        for issue in issues:
            issue_labels = issue.get("labels", {}).get("nodes", [])
            if not qualifies(issue_labels):
                continue

            repo_qualified += 1
            total_qualified += 1

            issue_id = issue["id"]
            issue_num = issue["number"]
            label_list = [l["name"] for l in issue_labels]
            issue_title = issue["title"][:70]

            if issue_id in existing_items:
                print(f"  [#{issue_num}] {issue_title}... already in project, skipping")
                total_skipped += 1
                continue

            print(f"  [#{issue_num}] {issue_title}...")
            print(f"      labels: {label_list}")

            if args.dry_run:
                print(f"    -> [DRY RUN] Would add to project")
                total_added += 1
            else:
                item_id = add_issue_to_project(PROJECT_ID, issue_id, status_field_id, triage_option_id)
                if item_id:
                    existing_items.add(issue_id)
                    total_added += 1
                else:
                    total_skipped += 1

        print(f"  -> {repo_qualified} qualifying issue(s)")

    print(f"\n{'='*50}")
    print(f"Total qualifying: {total_qualified}, Added: {total_added}, Skipped (already exists): {total_skipped}")
    if args.dry_run:
        print("(dry-run mode - no changes made)")


if __name__ == "__main__":
    main()
