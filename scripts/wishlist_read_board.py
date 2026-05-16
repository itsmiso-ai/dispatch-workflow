#!/usr/bin/env python3
"""Read Ready normal-lane items from the Vibe Coding Backlog project.

Used by the wishlist chipping cron to get current work items.
Heartbeat/groom owns state reconciliation; this selector intentionally only
returns Ready items for the cron to consume.

Usage:
    python3 wishlist_read_board.py
"""
import os
import json
import subprocess

GH = os.environ.get("GH", "/home/node/.local/bin/gh")
PROJECT_ID = "PVT_kwHOAsG-YM4BTyY3"


def gh(args: list) -> subprocess.CompletedProcess:
    cmd = [GH] + args
    return subprocess.run(cmd, capture_output=True, text=True)


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
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
              }
            }
          }
          content {
            ... on Issue {
              id
              number
              title
              state
              repository {
                nameWithOwner
              }
              labels(first: 20) {
                nodes {
                  name
                }
              }
              body
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
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("ERROR: Could not parse GraphQL response")
        print(result.stdout[:500])
        return []

    items = data.get("data", {}).get("node", {}).get("items", {}).get("nodes", [])
    ready_items = []

    for item in items:
        content = item.get("content")
        if not content:
            continue

        field_values = item.get("fieldValues", {}).get("nodes", [])
        statuses = [
            fv.get("name")
            for fv in field_values
            if fv.get("__typename") == "ProjectV2ItemFieldSingleSelectValue"
        ]
        status = None
        for s in statuses:
            if s == "Ready":
                status = s
                break
        if not status:
            continue

        labels = [lbl.get("name", "") for lbl in content.get("labels", {}).get("nodes", [])]
        label_set = {label.lower() for label in labels}
        title = content.get("title") or ""
        title_l = title.lower()

        # Heartbeat/groom owns lane assignment. This selector only consumes the
        # already-curated normal Ready lane.
        if (
            title_l.startswith(("weekly tech debt audit:", "tech debt audit:"))
            or "weekly tech debt audit:" in title_l
            or "[audit]" in title_l
            or "audit" in label_set
            or "umbrella" in label_set
            or "needs-gpt" in label_set
        ):
            continue

        ready_items.append({
            "item_id": item["id"],
            "issue_id": content.get("id"),
            "number": content.get("number"),
            "title": content.get("title"),
            "repo": content.get("repository", {}).get("nameWithOwner"),
            "labels": labels,
            "status": status,
        })

    # Sort ready items by priority: bug > p0 > p1 > oldest
    def priority_key(item):
        labels = set(item["labels"])
        if "bug" in labels:
            prio = 0
        elif "p0" in labels:
            prio = 1
        elif "p1" in labels:
            prio = 2
        else:
            prio = 3
        return (prio, item["number"])

    ready_items.sort(key=priority_key)

    for item in ready_items:
        print(json.dumps(item))

    if not ready_items:
        print("Pipeline is clear.")


if __name__ == "__main__":
    main()
