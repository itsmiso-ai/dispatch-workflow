#!/usr/bin/env python3
"""Read Ready/In Progress GPT-lane items from the Vibe Coding Backlog project.

Heartbeat/groom owns lane assignment. This selector consumes audit parents and
issues labeled needs-gpt after groom's model-backed judgment.
"""
import os
import json
import subprocess

GH = os.environ.get("GH", "/home/node/.local/bin/gh")
PROJECT_ID = "PVT_kwHOAsG-YM4BTyY3"


def gh(args: list) -> subprocess.CompletedProcess:
    cmd = [GH] + args
    return subprocess.run(cmd, capture_output=True, text=True)


def is_weekly_audit_parent(item: dict) -> bool:
    title = (item.get("title") or "").lower()
    labels = {str(label).lower() for label in item.get("labels", [])}
    return (
        title.startswith("weekly tech debt audit:")
        or title.startswith("tech debt audit:")
        or "weekly tech debt audit:" in title
        or "[audit]" in title
        or "audit" in labels
        or "umbrella" in labels
    )



def is_decomposed_audit_parent(item: dict) -> bool:
    """Return True when an audit parent has already been split into follow-ups.

    These parent issues are routing records, not implementation work. Keeping them
    in the GPT audit lane causes the cron to repeatedly spend a GPT run deciding
    not to duplicate already-created follow-up issues.
    """
    labels = {str(label).lower() for label in item.get("labels", [])}
    if "umbrella" in labels:
        return True

    comments = "\n".join(str(comment).lower() for comment in item.get("comments", []))
    decomposition_signals = [
        "follow-up issues created",
        "created follow-up issues",
        "decomposed remaining",
        "focused follow-up issues",
        "already decomposed into follow-up issues",
        "decomposed into follow-up issues",
    ]
    return any(signal in comments for signal in decomposition_signals)


def main():
    query = """
{
  node(id: "%s") {
    ... on ProjectV2 {
      items(first: 100) {
        nodes {
          id
          fieldValues(first: 5) {
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
              labels(first: 20) { nodes { name } }
              body
              comments(last: 20) { nodes { body } }
            }
          }
        }
      }
    }
  }
}
""" % PROJECT_ID

    result = gh(["api", "graphql", "--field", f"query={query}"])
    if result.returncode != 0:
        print("ERROR: GraphQL query failed")
        print(result.stderr)
        return

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("ERROR: Could not parse GraphQL response")
        print(result.stdout[:500])
        return

    items = data.get("data", {}).get("node", {}).get("items", {}).get("nodes", [])
    selected = []

    for item in items:
        content = item.get("content")
        if not content or content.get("state") != "OPEN":
            continue

        statuses = [
            fv.get("name")
            for fv in item.get("fieldValues", {}).get("nodes", [])
            if fv.get("__typename") == "ProjectV2ItemFieldSingleSelectValue"
        ]
        status = next((s for s in statuses if s in ("Ready", "In Progress")), None)
        if not status:
            continue

        labels = [lbl.get("name", "") for lbl in content.get("labels", {}).get("nodes", [])]
        comments = [c.get("body", "") for c in content.get("comments", {}).get("nodes", [])]
        item_data = {
            "item_id": item["id"],
            "issue_id": content.get("id"),
            "number": content.get("number"),
            "title": content.get("title"),
            "repo": content.get("repository", {}).get("nameWithOwner"),
            "labels": labels,
            "body": content.get("body") or "",
            "comments": comments,
            "status": status,
        }
        if is_decomposed_audit_parent(item_data):
            continue
        if is_weekly_audit_parent(item_data) or ("needs-gpt" in {str(label).lower() for label in labels} or "needs-escalation" in {str(label).lower() for label in labels}):
            selected.append(item_data)

    def priority_key(item):
        labels = {str(label).lower() for label in item["labels"]}
        if "priority/p0" in labels or "p0" in labels:
            prio = 0
        elif "priority/p1" in labels or "p1" in labels:
            prio = 1
        elif "priority/p2" in labels or "p2" in labels:
            prio = 2
        else:
            prio = 3
        status_prio = 0 if item["status"] == "Ready" else 1
        return (status_prio, prio, item["number"] or 0)

    selected.sort(key=priority_key)
    for item in selected:
        print(json.dumps(item))

    if not selected:
        print("No GPT audit issues ready.")


if __name__ == "__main__":
    main()
