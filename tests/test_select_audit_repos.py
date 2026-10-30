import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import select_audit_repos


class SelectAuditReposTests(unittest.TestCase):
    def test_umbrella_audit_is_not_used_as_recency(self):
        issues = [
            {"title": "Weekly tech debt audit: org/repo", "createdAt": "2026-09-01T00:00:00Z", "labels": [{"name": "audit"}]},
            {"title": "[P2] Real finding", "createdAt": "2026-08-01T00:00:00Z", "labels": [{"name": "audit"}]},
        ]
        with patch.object(select_audit_repos, "gh_issue_list", return_value=issues):
            self.assertEqual(select_audit_repos.latest_audit_issue_at("org/repo"), "2026-08-01T00:00:00Z")

    def test_selects_never_audited_repos_first_then_oldest_success(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "audit-rotation.json"
            state_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repos": {
                            "org/new": {"lastSuccessfulAuditAt": None},
                            "org/old": {"lastSuccessfulAuditAt": "2026-08-01T00:00:00Z"},
                            "org/recent": {"lastSuccessfulAuditAt": "2026-09-01T00:00:00Z"},
                            "org/removed": {"lastSuccessfulAuditAt": "2020-01-01T00:00:00Z"},
                        },
                    }
                )
            )

            with patch.object(select_audit_repos, "fetch_tracked_repos", return_value=["org/recent", "org/old", "org/new"]):
                result = select_audit_repos.select_repos(state_file, 3)

            self.assertEqual([repo["fullName"] for repo in result["repos"]], ["org/new", "org/old", "org/recent"])
            saved = json.loads(state_file.read_text())
            self.assertNotIn("org/removed", saved["repos"])

    def test_completed_record_advances_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "audit-rotation.json"
            state_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repos": {"org/repo": {"lastSuccessfulAuditAt": "2026-08-01T00:00:00Z"}},
                    }
                )
            )

            select_audit_repos.record_audit(
                state_file,
                "org/repo",
                "completed",
                None,
                "2026-09-02T20:00:00Z",
            )

            saved = json.loads(state_file.read_text())
            self.assertEqual(saved["repos"]["org/repo"]["lastSuccessfulAuditAt"], "2026-09-02T20:00:00Z")
            self.assertEqual(saved["repos"]["org/repo"]["lastStatus"], "completed")

    def test_failed_record_does_not_advance_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "audit-rotation.json"
            state_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repos": {"org/repo": {"lastSuccessfulAuditAt": "2026-08-01T00:00:00Z"}},
                    }
                )
            )

            select_audit_repos.record_audit(
                state_file,
                "org/repo",
                "partial",
                "provider rate limit",
                "2026-09-02T20:00:00Z",
            )

            saved = json.loads(state_file.read_text())
            self.assertEqual(saved["repos"]["org/repo"]["lastSuccessfulAuditAt"], "2026-08-01T00:00:00Z")
            self.assertEqual(saved["repos"]["org/repo"]["lastStatus"], "partial")



    def test_fresh_failure_ranks_before_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "audit-rotation.json"
            state_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repos": {
                            "org/fail": {"lastSuccessfulAuditAt": "2026-08-01T00:00:00Z", "lastStatus": "partial", "consecutiveFailures": 1},
                            "org/old": {"lastSuccessfulAuditAt": "2026-07-01T00:00:00Z", "lastStatus": "completed"},
                        },
                    }
                )
            )
            with patch.object(select_audit_repos, "fetch_tracked_repos", return_value=["org/fail", "org/old"]):
                result = select_audit_repos.select_repos(state_file, 2)
            names = [r["fullName"] for r in result["repos"]]
            self.assertEqual(names[0], "org/fail")
            self.assertEqual(names[1], "org/old")

    def test_chronic_failure_demoted_below_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "audit-rotation.json"
            state_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repos": {
                            "org/chronic": {"lastSuccessfulAuditAt": "2026-06-01T00:00:00Z", "lastStatus": "partial", "consecutiveFailures": 2},
                            "org/old": {"lastSuccessfulAuditAt": "2026-07-01T00:00:00Z", "lastStatus": "completed"},
                            "org/new": {"lastSuccessfulAuditAt": "2026-08-01T00:00:00Z", "lastStatus": "completed"},
                        },
                    }
                )
            )
            with patch.object(select_audit_repos, "fetch_tracked_repos", return_value=["org/chronic", "org/old", "org/new"]):
                result = select_audit_repos.select_repos(state_file, 2)
            names = [r["fullName"] for r in result["repos"]]
            # chronic should be last among the two selected (old before chronic)
            self.assertEqual(names, ["org/old", "org/new"])

    def test_consecutive_failures_increment_on_partial_and_reset_on_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "audit-rotation.json"
            state_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repos": {"org/repo": {"lastSuccessfulAuditAt": "2026-08-01T00:00:00Z", "lastStatus": "completed", "consecutiveFailures": 0}},
                    }
                )
            )
            # first partial
            select_audit_repos.record_audit(state_file, "org/repo", "partial", "err", "2026-09-01T00:00:00Z")
            saved = json.loads(state_file.read_text())
            self.assertEqual(saved["repos"]["org/repo"]["consecutiveFailures"], 1)
            # second partial
            select_audit_repos.record_audit(state_file, "org/repo", "partial", "err", "2026-09-02T00:00:00Z")
            saved = json.loads(state_file.read_text())
            self.assertEqual(saved["repos"]["org/repo"]["consecutiveFailures"], 2)
            # completed resets
            select_audit_repos.record_audit(state_file, "org/repo", "completed", None, "2026-09-03T00:00:00Z")
            saved = json.loads(state_file.read_text())
            self.assertEqual(saved["repos"]["org/repo"]["consecutiveFailures"], 0)
            self.assertEqual(saved["repos"]["org/repo"]["lastSuccessfulAuditAt"], "2026-09-03T00:00:00Z")

    def test_selection_avoids_stuck_repo_after_cap(self):
        # Simulate the real pinchflat scenario: one chronic failing repo among others
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "audit-rotation.json"
            state_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repos": {
                            "misospace/pinchflat": {"lastSuccessfulAuditAt": "2026-10-20T11:39:49Z", "lastStatus": "partial", "consecutiveFailures": 3},
                            "misospace/musebridge": {"lastSuccessfulAuditAt": "2026-10-20T12:13:03Z", "lastStatus": "completed"},
                            "misospace/courier": {"lastSuccessfulAuditAt": "2026-10-20T23:41:16Z", "lastStatus": "completed"},
                        },
                    }
                )
            )
            with patch.object(select_audit_repos, "fetch_tracked_repos", return_value=["misospace/pinchflat", "misospace/musebridge", "misospace/courier"]):
                result = select_audit_repos.select_repos(state_file, 1)
            self.assertEqual(result["repos"][0]["fullName"], "misospace/musebridge")


if __name__ == "__main__":
    unittest.main()
