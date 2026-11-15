"""The release inventory must never include private runtime files or unreviewed art."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.audit_release import history_private_paths, scan


class ReleaseInventoryTests(unittest.TestCase):
    def test_only_allowlisted_source_gets_a_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = {
                "matchlog/example.py": b"pass\n",
                "ui/assets/ranks/badge.png": b"unreviewed art",
                "tests/fixtures/example.json": b"{}",
                "ui/app.js": b"// pending review of existing profile compatibility",
                "data/real-account.json": b"private runtime data",
                ".env.local": b"private configuration",
                "output/private-report.md": b"private output",
                "misc/export.zip": b"unknown archive",
            }
            for relative, content in paths.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            report = scan(root)

        self.assertFalse(report["ready_for_publication"])
        self.assertEqual(
            report["candidate_source"],
            [
                {
                    "path": "matchlog/example.py",
                    "bytes": 5,
                    "sha256": hashlib.sha256(b"pass\n").hexdigest(),
                }
            ],
        )
        self.assertEqual(
            {row["reason"] for row in report["withheld"]},
            {
                "bundled_asset",
                "test_fixture",
                "account_compatibility_review",
                "unknown_file",
            },
        )
        result = json.dumps(report)
        for name in ("real-account.json", ".env.local", "private-report.md"):
            self.assertNotIn(name, result)

    def test_report_withholds_and_redacts_source_with_private_patterns(self):
        home = "C:" + "/Users/" + "LocalPlayer"
        credential = "gh" + "p_" + "z" * 36
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "app.py"
            path.write_text(
                f"path = {home!r}\nsecret = {credential!r}\n", encoding="utf-8"
            )
            report = scan(root)

        self.assertEqual(report["candidate_source"], [])
        self.assertEqual(
            report["withheld"],
            [
                {
                    "path": "app.py",
                    "reason": "privacy_review",
                    "findings": [
                        {"line": 1, "category": "local_home_path"},
                        {"line": 2, "category": "credential_like"},
                    ],
                }
            ],
        )
        result = json.dumps(report)
        self.assertNotIn(home, result)
        self.assertNotIn(credential, result)

    def test_history_lists_counts_without_private_filenames(self):
        # The same historical file can appear in several revisions.
        result = Mock(stdout=b"app.py\0\noutput/private.db\0app.py\0")
        with patch("tools.audit_release.subprocess.run", return_value=result) as run:
            report = history_private_paths(Path.cwd())

        self.assertEqual(
            report,
            {
                "paths_examined": 2,
                "private_paths_found": 1,
                "requires_content_review": True,
            },
        )
        self.assertEqual(run.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
