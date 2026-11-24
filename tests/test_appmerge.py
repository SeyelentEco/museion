"""Carrying a person's code changes into a new version's app/."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from museion import appmerge
from museion.appmerge import ADDED, KEPT, MERGED, REMOVED, SET_ASIDE

BASE = {
    "museion/ask.py": b"def ask():\n    return 1\n\n\ndef rank():\n    return 2\n",
    "museion/calc.py": b"def calc():\n    return 1\n",
    "museion/old.py": b"old = True\n",
    "agents/librarian/agent.toml": b'name = "Librarian"\n',
    "catalog/a.toml": b'id = "a"\n',
}


class MergeTreeTest(unittest.TestCase):
    def test_an_unchanged_copy_becomes_the_new_version(self):
        new = {**BASE, "museion/calc.py": b"def calc():\n    return 2\n", "museion/new.py": b"new = True\n"}
        del new["museion/old.py"]
        result = appmerge.merge(BASE, dict(BASE), new)
        self.assertEqual(result.files, new)
        self.assertEqual(result.changes, {})
        self.assertEqual(appmerge.notes(result, "0.1.0", "0.2.0", "my-changes/from-0.1.0"), "")

    def test_each_kind_of_change_is_carried(self):
        mine = dict(BASE)
        mine["museion/ask.py"] = b"def ask():\n    return 100\n\n\ndef rank():\n    return 2\n"   # both change it, apart
        mine["museion/calc.py"] = b"def calc():\n    return 5\n"                                  # only I change it
        mine["agents/mine/agent.toml"] = b'name = "Mine"\n'                                       # I add it
        del mine["catalog/a.toml"]                                                                # I delete it
        new = dict(BASE)
        new["museion/ask.py"] = b"def ask():\n    return 1\n\n\ndef rank():\n    return 20\n"
        new["museion/new.py"] = b"new = True\n"
        result = appmerge.merge(BASE, mine, new)
        self.assertEqual(result.files["museion/ask.py"], b"def ask():\n    return 100\n\n\ndef rank():\n    return 20\n")
        self.assertEqual(result.files["museion/calc.py"], mine["museion/calc.py"])
        self.assertEqual(result.files["agents/mine/agent.toml"], b'name = "Mine"\n')
        self.assertNotIn("catalog/a.toml", result.files)
        self.assertEqual(result.files["museion/new.py"], b"new = True\n")
        self.assertEqual(result.changes, {"museion/ask.py": MERGED, "museion/calc.py": KEPT,
                                          "agents/mine/agent.toml": ADDED, "catalog/a.toml": REMOVED})
        self.assertEqual(result.set_aside(), [])
        self.assertEqual(len(result.carried()), 4)

    def test_collisions_are_set_aside_with_what_was_changed(self):
        mine = dict(BASE)
        mine["museion/calc.py"] = b"def calc():\n    return 5\n"       # same line as the new version
        mine["museion/old.py"] = b"old = False\n"                      # the new version drops this file
        mine["museion/new.py"] = b"mine = True\n"                      # the new version brings its own
        del mine["agents/librarian/agent.toml"]                        # I deleted what the new version changed
        new = dict(BASE)
        new["museion/calc.py"] = b"def calc():\n    return 9\n"
        del new["museion/old.py"]
        new["museion/new.py"] = b"new = True\n"
        new["agents/librarian/agent.toml"] = b'name = "The Librarian"\n'
        result = appmerge.merge(BASE, mine, new)
        self.assertEqual(result.files, new)
        self.assertEqual(result.set_aside(), ["agents/librarian/agent.toml", "museion/calc.py", "museion/new.py", "museion/old.py"])
        self.assertTrue(all(how == SET_ASIDE for how in result.changes.values()))
        kept, text, why = result.aside["museion/calc.py"]
        self.assertEqual(kept, mine["museion/calc.py"])
        self.assertIn("-    return 1", text)
        self.assertIn("+    return 5", text)
        self.assertIn("same lines", why)
        self.assertIsNone(result.aside["agents/librarian/agent.toml"][0])

        notes = appmerge.notes(result, "0.1.0", "0.2.0", "my-changes/from-0.1.0")
        for path in result.set_aside():
            self.assertIn(f"`{path}`", notes)
        self.assertIn("my-changes/from-0.1.0/museion/calc.py.diff", notes)
        with tempfile.TemporaryDirectory() as tmp:
            appmerge.write_aside(result, Path(tmp))
            self.assertEqual(Path(tmp, "museion/calc.py").read_bytes(), mine["museion/calc.py"])
            self.assertIn("+    return 5", Path(tmp, "museion/calc.py.diff").read_text(encoding="utf-8"))
            self.assertFalse(Path(tmp, "agents/librarian/agent.toml").exists())
            self.assertTrue(Path(tmp, "agents/librarian/agent.toml.diff").exists())

    def test_the_same_change_on_both_sides_is_no_change(self):
        both = {**BASE, "museion/calc.py": b"def calc():\n    return 7\n"}
        result = appmerge.merge(BASE, dict(both), dict(both))
        self.assertEqual((result.files, result.changes), (both, {}))

    def test_line_endings_alone_are_not_a_change(self):
        mine = {path: data.replace(b"\n", b"\r\n") for path, data in BASE.items()}
        new = {**BASE, "museion/calc.py": b"def calc():\n    return 2\n"}
        result = appmerge.merge(BASE, mine, new)
        self.assertEqual((result.files, result.changes), (new, {}))
        self.assertEqual(appmerge.local_changes(BASE, mine), {})

    def test_without_a_record_of_what_was_shipped_the_new_version_goes_in_whole(self):
        new = {"museion/ask.py": b"new\n"}
        result = appmerge.merge(None, {"museion/ask.py": b"mine\n"}, new)
        self.assertEqual((result.files, result.changes, result.aside), (new, {}, {}))

    def test_setting_every_change_aside(self):
        mine = {**BASE, "museion/calc.py": b"def calc():\n    return 5\n", "extra.py": b"x = 1\n"}
        del mine["museion/old.py"]
        new = {**BASE, "museion/new.py": b"new = True\n"}
        result = appmerge.without_changes(BASE, mine, new)
        self.assertEqual(result.files, new)
        self.assertEqual(result.set_aside(), ["extra.py", "museion/calc.py", "museion/old.py"])
        self.assertEqual(result.carried(), [])


class ReadingTest(unittest.TestCase):
    def test_trees_and_the_shipped_zip_read_alike(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp, "app")
            for path, data in BASE.items():
                (app / path).parent.mkdir(parents=True, exist_ok=True)
                (app / path).write_bytes(data)
            (app / "museion" / "__pycache__").mkdir()
            (app / "museion" / "__pycache__" / "ask.cpython-312.pyc").write_bytes(b"\0")
            (app / appmerge.NOTES).write_text("notes")
            self.assertEqual(appmerge.read_tree(app), BASE)
            with zipfile.ZipFile(Path(tmp, "original.zip"), "w") as z:
                for path, data in BASE.items():
                    z.writestr(f"app/{path}", data)
            self.assertEqual(appmerge.read_original(Path(tmp, "original.zip")), BASE)
            self.assertIsNone(appmerge.read_original(Path(tmp, "missing.zip")))
            Path(tmp, "bad.zip").write_bytes(b"not a zip")
            self.assertIsNone(appmerge.read_original(Path(tmp, "bad.zip")))

    def test_local_changes(self):
        mine = {**BASE, "museion/calc.py": b"changed\n", "mine.py": b"x\n"}
        del mine["museion/old.py"]
        self.assertEqual(appmerge.local_changes(BASE, mine),
                         {"mine.py": "added", "museion/calc.py": "changed", "museion/old.py": "removed"})
