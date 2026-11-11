"""The agent tools on their own: every tool is well formed, and the file
tools stay inside the agent's folder."""

import unittest

from museion.agent_tools import RISKS, ToolError, all_tools, check_args
from museion.index import Hit
from agent_fakes import FakeRun


def tool(name):
    return all_tools()[name]


class RegistryTest(unittest.TestCase):
    def test_every_tool_is_well_formed(self):
        for name, t in all_tools().items():
            with self.subTest(name):
                self.assertIn(t.risk, RISKS)
                self.assertEqual(t.schema()["function"]["name"], name)

    def test_no_tool_reaches_the_network_or_the_shell(self):
        import inspect
        for name, t in all_tools().items():
            source = inspect.getsource(inspect.getmodule(t.run))
            for banned in ("urllib", "socket", "subprocess", "os.system", "shutil.rmtree", "unlink("):
                self.assertNotIn(banned, source, f"{name} uses {banned}")

    def test_check_args(self):
        calc = tool("calculate")
        self.assertIsNone(check_args(calc, {"expression": "1+1"}))
        self.assertIn("needs expression", check_args(calc, {}))
        self.assertIn("must be a string", check_args(calc, {"expression": 3}))
        self.assertIn("must be a integer", check_args(tool("read_page"), {"number": "2"}))


class FileToolsTest(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun()

    def test_write_read_list_and_history(self):
        write, read = tool("write_file"), tool("read_file")
        write.run({"name": "stores.csv", "text": "item,quantity"}, self.run)
        write.run({"name": "stores.csv", "text": "rice,10", "append": True}, self.run)
        self.assertEqual(read.run({"name": "stores.csv"}, self.run), "item,quantity\nrice,10\n")
        self.assertEqual(self.run.files, ["stores.csv"])
        self.assertEqual(len(list((self.run.workspace / ".history").iterdir())), 1)
        self.assertIn("stores.csv", tool("list_files").run({}, self.run))
        self.assertNotIn(".history", tool("list_files").run({}, self.run))

    def test_two_writes_in_one_second_keep_both_old_versions(self):
        notes = tool("notes")
        for line in ("one", "two", "three"):
            notes.run({"action": "add", "text": line}, self.run)
        kept = sorted(p.read_text() for p in (self.run.workspace / ".history").iterdir())
        self.assertEqual(len(kept), 2)  # the versions before "two" and before "three"
        self.assertNotEqual(kept[0], kept[1])

    def test_stays_inside_the_folder(self):
        write = tool("write_file")
        for name in ("../escape.md", "/etc/passwd", "C:/x.md", "sub/../../x.md", ".hidden.md"):
            with self.subTest(name), self.assertRaises(ToolError):
                write.run({"name": name, "text": "x"}, self.run)
        with self.assertRaises(ToolError):
            write.run({"name": "run.py", "text": "x"}, self.run)
        with self.assertRaises(ToolError):
            tool("read_file").run({"name": "../config.json"}, self.run)

    def test_no_folder(self):
        self.run.workspace = None
        with self.assertRaises(ToolError):
            tool("write_file").run({"name": "a.md", "text": "x"}, self.run)

    def test_notes(self):
        notes = tool("notes")
        self.assertTrue(notes.run({"action": "read"}, self.run).startswith("Your own working notes are empty."))
        notes.run({"action": "add", "text": "We are 4."}, self.run)
        self.assertIn("We are 4.", notes.run({"action": "read"}, self.run))
        notes.run({"action": "replace", "text": "We are 5."}, self.run)
        self.assertEqual(notes.run({"action": "read"}, self.run), "We are 5.\n")
        with self.assertRaises(ToolError):
            notes.run({"action": "shred"}, self.run)

    def test_read_page(self):
        self.run.sources = [Hit("Book", "", "b", "f.pdf", 3, "", 0.0, 1.0, "Whole page text.")]
        self.assertIn("Whole page text.", tool("read_page").run({"number": 1}, self.run))
        with self.assertRaises(ToolError):
            tool("read_page").run({"number": 2}, self.run)

    def test_calculate_records(self):
        out = tool("calculate").run({"expression": "convert(0.25 * 55, tsp, ml)"}, self.run)
        self.assertIn("67.77", out)
        self.assertEqual(self.run.calculations[0].result, "67.77")

    def test_library_files_and_add_source(self):
        lib = self.run.workspace / "library"
        lib.mkdir()
        (lib / "my-archive_en_2026-01.zim").write_bytes(b"zim data")
        self.run.setting.library_dir = lib
        self.assertIn("NOT in the catalogue", tool("library_files").run({}, self.run))
        import museion.catalog_add as ca
        target_dir = self.run.workspace / "sources"
        target_dir.mkdir()
        original = ca.add_source
        ca.add_source = lambda path, title, cat: original(path, title, cat, sources_dir=target_dir)
        try:
            out = tool("add_source").run({"file": "my-archive_en_2026-01.zim", "title": "Mine",
                                          "category": "reference"}, self.run)
        finally:
            ca.add_source = original
        self.assertIn("Added", out)
        with self.assertRaises(ToolError):
            tool("add_source").run({"file": "x.zim", "title": "X", "category": "weapons"}, self.run)


if __name__ == "__main__":
    unittest.main()
