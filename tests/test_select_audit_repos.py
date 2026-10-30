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



    def test_oldest_audit_wins_regardless_of_status(self):
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
            # Oldest successful audit wins; status no longer demotes
            self.assertEqual(names[0], "org/old")
            self.assertEqual(names[1], "org/fail")

    def test_chronic_failure_not_demoted_oldest_wins(self):
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
            # Oldest audit wins; chronic is oldest and should be selected
            self.assertEqual(names, ["org/chronic", "org/old"])

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

    def test_partial_repo_retries_after_each_full_rotation(self):
        repos = ["org/a", "org/b", "org/chronic"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 1, "repos": {
                repo: {"lastAttemptAt": "2020-01-01T00:00:00Z",
                       "lastSuccessfulAuditAt": "2019-01-01T00:00:00Z",
                       "lastStatus": "partial" if repo.endswith("chronic") else "completed"}
                for repo in repos}}))
            selected = []
            with patch.object(select_audit_repos, "fetch_tracked_repos", return_value=repos):
                for turn in range(9):
                    result = select_audit_repos.select_repos(path, 1)
                    repo = result["repos"][0]["fullName"]
                    selected.append(repo)
                    select_audit_repos.record_audit(
                        path, repo, "partial" if repo.endswith("chronic") else "completed",
                        "blocked" if repo.endswith("chronic") else None,
                        f"2099-01-{turn + 1:02d}T00:00:00Z")
            self.assertEqual(selected, repos * 3)
            chronic = json.loads(path.read_text())["repos"]["org/chronic"]
            self.assertEqual(chronic["lastSuccessfulAuditAt"], "2019-01-01T00:00:00Z")
            self.assertEqual(chronic["lastStatus"], "partial")

    def test_chronic_partial_repo_is_selected_not_starved(self):
        # Simulate the real pinchflat scenario: one chronic failing repo among others
        # With fair oldest-attempt ordering, pinchflat (oldest) should be selected
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
            self.assertEqual(result["repos"][0]["fullName"], "misospace/pinchflat")


if __name__ == "__main__":
    unittest.main()
