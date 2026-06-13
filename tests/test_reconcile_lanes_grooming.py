#!/usr/bin/env python3
"""Test the deterministic lane-reclassification guard in reconcile_lanes.

The heartbeat script calls project_groom.reconcile_lanes() to normalize lane
assignments. That function has a heuristic for audit/umbrella issues and
issues with an unknown current lane. The heuristic must NOT silently override
an explicit /api/issues/groom decision recorded in the issue's groomedAt
timestamp — otherwise Saffron's promotion of audit issues to escalated
gets reverted on the next heartbeat, producing recurring candidates and
worker churn.

Run with:  python3 -m unittest tests.test_reconcile_lanes_grooming
or:        python3 tests/test_reconcile_lanes_grooming.py
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import project_groom  # noqa: E402


def _make_issue(
    *,
    issue_id: str,
    lane: str,
    groomed_at: str | None,
    title: str = "Weekly tech debt audit: foo - 2026-06-03",
    labels: list[str] | None = None,
    body: str = "",
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "id": issue_id,
        "state": "open",
        "repository": {"owner": "foo", "name": "bar"},
        "number": 42,
        "currentLane": lane,
        "title": title,
        "labels": list(labels or ["audit", "enhancement", "priority/p1"]),
        "updatedAt": "2026-06-12T18:00:00Z",
    }
    if groomed_at is not None:
        issue["groomedAt"] = groomed_at
        issue["groomedBy"] = "agent"
    if body:
        issue["body"] = body
    return issue


class GroomingGuardTests(unittest.TestCase):
    """Pin down the deterministic behaviour of the groomedAt guard."""

    def setUp(self) -> None:
        # The function only acts on tracked repos; whitelist our test repo.
        self._orig_tracked = list(project_groom.TRACKED_REPOS)
        project_groom.TRACKED_REPOS = ["foo/bar"]

    def tearDown(self) -> None:
        project_groom.TRACKED_REPOS = self._orig_tracked

    @staticmethod
    def _capture_classify(monkey_results: list[tuple[str, str]]):
        def fake_classify(issue_id, lane, reason, confidence="high"):
            monkey_results.append((issue_id, lane))
            return True

        return fake_classify

    @staticmethod
    def _no_http(*_args, **_kwargs):
        """Stand-in for set_dispatch_status to suppress HTTP 404 stderr in tests."""
        return True

    def test_groomed_audit_not_reverted_to_backlog(self) -> None:
        """Saffron escalated an audit; the heartbeat must not re-queue it."""
        calls: list[tuple[str, str]] = []
        issue = _make_issue(
            issue_id="iss-1",
            lane="escalated",
            groomed_at="2026-06-12T18:47:30.747Z",
        )
        with patch.object(project_groom, "classify_dispatch_issue", self._capture_classify(calls)), \
               patch.object(project_groom, "set_dispatch_status", self._no_http):
            changed = project_groom.reconcile_lanes([issue])
        self.assertEqual(changed, 0, "groomed issue must not be reclassified")
        self.assertEqual(calls, [], "classify_dispatch_issue must not be called for groomed issue")

    def test_ungroomed_audit_can_be_moved_to_backlog(self) -> None:
        """The heuristic still applies to issues Saffron has not groomed yet."""
        calls: list[tuple[str, str]] = []
        issue = _make_issue(
            issue_id="iss-2",
            lane="escalated",
            groomed_at=None,
        )
        with patch.object(project_groom, "classify_dispatch_issue", self._capture_classify(calls)), \
               patch.object(project_groom, "set_dispatch_status", self._no_http):
            changed = project_groom.reconcile_lanes([issue])
        self.assertEqual(changed, 1, "ungroomed audit should be moved to backlog")
        self.assertEqual(calls, [("iss-2", "backlog")])

    def test_groomed_audit_child_not_promoted_to_normal(self) -> None:
        """Symmetric rule: groomed issues skip the heuristic in BOTH directions.

        The previous one-sided guard only blocked the backlog destination.
        If Saffron explicitly groomed an audit child to backlog, the heuristic
        must not promote it back to normal.
        """
        calls: list[tuple[str, str]] = []
        issue = _make_issue(
            issue_id="iss-3",
            lane="backlog",
            groomed_at="2026-06-12T18:47:30.747Z",
            title="Audit child: foo",
            labels=["audit", "priority/p1"],
            body="<!-- audit-child:v1\nchild body",
        )
        with patch.object(project_groom, "classify_dispatch_issue", self._capture_classify(calls)), \
               patch.object(project_groom, "set_dispatch_status", self._no_http):
            changed = project_groom.reconcile_lanes([issue])
        self.assertEqual(changed, 0, "groomed audit child must not be re-promoted to normal")
        self.assertEqual(calls, [], "classify_dispatch_issue must not be called for groomed audit child")

    def test_ungroomed_audit_child_can_be_promoted_to_normal(self) -> None:
        """Ungroomed audit children should still be promoted to normal."""
        calls: list[tuple[str, str]] = []
        issue = _make_issue(
            issue_id="iss-4",
            lane="backlog",
            groomed_at=None,
            title="Audit child: foo",
            labels=["audit", "priority/p1"],
            body="<!-- audit-child:v1\nchild body",
        )
        with patch.object(project_groom, "classify_dispatch_issue", self._capture_classify(calls)), \
               patch.object(project_groom, "set_dispatch_status", self._no_http):
            changed = project_groom.reconcile_lanes([issue])
        self.assertEqual(changed, 1, "ungroomed audit child should be promoted to normal")
        self.assertEqual(calls, [("iss-4", "normal")])

    def test_non_groomed_skip_when_lane_already_matches(self) -> None:
        """No state change is requested if desired_lane == current_lane."""
        calls: list[tuple[str, str]] = []
        # current_lane=backlog, audit parent already decomposed -> desired backlog
        issue = _make_issue(
            issue_id="iss-5",
            lane="backlog",
            groomed_at=None,
            title="Weekly tech debt audit: foo",
            labels=["audit", "enhancement", "umbrella"],
        )
        with patch.object(project_groom, "classify_dispatch_issue", self._capture_classify(calls)), \
               patch.object(project_groom, "set_dispatch_status", self._no_http):
            changed = project_groom.reconcile_lanes([issue])
        self.assertEqual(changed, 0, "no-op when lane already matches")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
