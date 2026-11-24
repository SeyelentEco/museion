"""The version number, comparing versions, and the change list."""

import contextlib
import io
import unittest
from pathlib import Path

from museion import cli, version

ROOT = Path(__file__).resolve().parent.parent


class VersionTest(unittest.TestCase):
    def test_versions_compare_as_numbers(self):
        self.assertTrue(version.newer("0.10.0", "0.9.3"))
        self.assertTrue(version.newer("1.0", "0.99.99"))
        self.assertTrue(version.newer("v0.2.0", "0.1.0"))
        self.assertTrue(version.newer("0.1.1", "0.1"))
        self.assertFalse(version.newer("0.1.0", "0.1.0"))
        self.assertFalse(version.newer("0.1.0-rc1", "0.1.0"))
        # Text that is not a version is older than any version.
        self.assertTrue(version.newer("0.0.1", "unknown"))
        self.assertFalse(version.newer("", "0.1.0"))

    def test_this_version_is_a_version(self):
        self.assertGreaterEqual(len(version.parse(version.VERSION)), 2)
        self.assertEqual(".".join(map(str, version.parse(version.VERSION))), version.VERSION)

    def test_the_change_list_says_what_this_version_changed(self):
        # The release's text on GitHub is this section; a release cannot go
        # out with nothing to say.
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(version.notes(changelog))
        first = next(line for line in changelog.splitlines() if line.startswith("## "))
        self.assertEqual(first, f"## {version.VERSION}", "the newest version comes first")

    def test_notes_are_one_section(self):
        text = "# Changes\n\n## 0.2.0\n\n- two\n\n## 0.1.0\n\n- one\n"
        self.assertEqual(version.notes(text, "0.2.0"), "- two")
        self.assertEqual(version.notes(text, "0.1.0"), "- one")
        self.assertEqual(version.notes(text, "0.3.0"), "")

    def test_the_command_prints_it(self):
        for argv, want in ((["version"], f"Museion {version.VERSION}\n"), (["--version"], f"Museion {version.VERSION}\n"),
                           (["version", "--number"], version.VERSION + "\n")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(argv)
            self.assertEqual(out.getvalue(), want)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["version", "--notes"])
        self.assertIn("first version", out.getvalue())
