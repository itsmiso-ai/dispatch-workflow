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


if __name__ == "__main__":
    unittest.main()
