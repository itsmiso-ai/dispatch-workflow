import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import create_audit_issue


class CreateAuditIssueTests(unittest.TestCase):
    def test_normalized_priority_title_is_same_finding(self):
        self.assertEqual(
            create_audit_issue.normalize_title("[P1] Fix the thing"),
            create_audit_issue.normalize_title("[P2]   fix the thing"),
        )

    def test_existing_closed_issue_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.json"
            args = create_audit_issue.build_parser().parse_args(
                [
                    "--repo",
                    "org/repo",
                    "--title",
                    "[P2] Fix the thing",
                    "--body",
                    "Ask: Fix the thing.\nExpected files: src/thing.py\n\n**Problem:** bad\n\n**Evidence:** test\n\n**Acceptance:** fixed",
                    "--ledger",
                    str(ledger),
                ]
            )
            with patch.object(create_audit_issue, "gh_issue_list", return_value=[{"number": 12, "title": "[P2] Fix the thing", "state": "CLOSED", "url": "u"}]):
                result = create_audit_issue.create_issue(args, args.body)
            self.assertFalse(result["created"])
            self.assertTrue(result["duplicate"])
            self.assertTrue(ledger.exists())

    def test_failed_create_recovers_existing_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.json"
            body_file = Path(directory) / "body.md"
            body_file.write_text("Ask: Fix the thing.\nExpected files: src/thing.py\n\n**Problem:** bad\n\n**Evidence:** test\n\n**Acceptance:** fixed")
            args = create_audit_issue.build_parser().parse_args(
                ["--repo", "org/repo", "--title", "[P2] Fix the thing", "--body-file", str(body_file), "--ledger", str(ledger)]
            )
            matches = [[], [{"number": 12, "title": "[P2] Fix the thing", "state": "OPEN", "url": "u"}]]
            with patch.object(create_audit_issue, "gh_issue_list", side_effect=matches), patch.object(
                create_audit_issue.subprocess, "run", side_effect=create_audit_issue.subprocess.SubprocessError("timed out")
            ):
                result = create_audit_issue.create_issue(args, body_file.read_text())
            self.assertTrue(result["recovered"])
            self.assertEqual(result["issue"]["number"], 12)


if __name__ == "__main__":
    unittest.main()
