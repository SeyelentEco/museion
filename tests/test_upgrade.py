"""Upgrading one copy of Museion from another, with stand-in copies: what is
replaced, what is never touched, what becomes of a person's changes to the
code, going back, and a switch that is refused or cut short."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from museion import appmerge, launchers, package, switch, upgrade
from museion.upgrade import UpgradeError
from upgrade_copies import code, make_copy, tree, write

NEW_ASK = "def ask():\n    return 'new'\n\n\ndef cite():\n    return 1\n"
MINE = ("library", "models", "config.json", "tuning.toml", "agents", "museion-portable")


def passes(app: Path, log: Path) -> bool:
    return True


class UpgradeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.old = make_copy(base / "old", "0.1.0")
        self.new = make_copy(base / "new", "0.2.0", code("0.2.0", {"museion/ask.py": NEW_ASK,
                                                                  "museion/maps.py": "new in 0.2\n"}))
        self.said: list[str] = []

    def install(self, **options) -> upgrade.Outcome:
        options.setdefault("tests", passes)
        return upgrade.install(upgrade.source_of(self.new), self.old, report=self.said.append, **options)

    def app(self, path: str) -> str:
        return (self.old / "app" / path).read_text()

    def mine(self) -> dict[str, bytes]:
        return {p: d for p, d in tree(self.old).items() if p.startswith(MINE)}


class Install(UpgradeCase):
    def test_the_code_is_replaced_and_the_persons_things_are_not_touched(self):
        before = self.mine()
        outcome = self.install()
        self.assertEqual((outcome.old, outcome.new, outcome.parts), ("0.1.0", "0.2.0", ["app"]))
        self.assertEqual(self.app("museion/ask.py"), NEW_ASK)
        self.assertEqual(self.app("museion/maps.py"), "new in 0.2\n")
        self.assertEqual(package.code_version(self.old / "app"), "0.2.0")
        self.assertEqual(package.read(self.old).version, "0.2.0")
        self.assertEqual(self.mine(), before)
        self.assertGreater(len(before), 5)
        # The record of what was shipped is the new version's.
        self.assertEqual(appmerge.read_original(self.old / upgrade.ORIGINAL)["museion/ask.py"], NEW_ASK.encode())
        self.assertEqual(upgrade.local_changes(self.old), {})
        # Nothing is left of the work, and the launchers are there.
        self.assertFalse((self.old / "upgrade" / "new").exists())
        self.assertFalse((self.old / "upgrade" / "staged.json").exists())
        for name in launchers.launchers():
            self.assertTrue((self.old / name).is_file(), name)
        self.assertEqual([(e["from"], e["to"]) for e in switch.history(self.old)], [("0.1.0", "0.2.0")])

    def test_the_source_is_left_as_it_was(self):
        before = tree(self.new)
        self.install()
        self.assertEqual(tree(self.new), before)

    def test_what_was_replaced_is_kept(self):
        self.install()
        kept = self.old / "previous" / "0.1.0"
        self.assertEqual((kept / "app" / "museion" / "ask.py").read_text(), code("0.1.0")["museion/ask.py"])
        self.assertEqual(package.read(kept).version, "0.1.0")
        self.assertFalse((kept / "python").exists(), "the Python did not change, so it was not moved")
        self.assertEqual(upgrade.previous_version(self.old), "0.1.0")

    def test_python_and_helpers_are_replaced_only_when_their_builds_differ(self):
        self.assertEqual(upgrade.parts_to_replace(upgrade.source_of(self.new), self.old), ["app"])
        newer = make_copy(self.new.parent / "newer", "0.3.0", python="py-2",
                          helpers={"llama.cpp/cpu": "llama-2", "kiwix-tools": "kiwix-1"})
        write(self.old / "bin" / "llama" / "vulkan" / "llama-program", "a build only the old copy has")
        outcome = upgrade.install(upgrade.source_of(newer), self.old, tests=passes)
        self.assertEqual(outcome.parts, ["app", "python", "bin"])
        self.assertEqual((self.old / "python" / "bin" / "python3").read_text(), "the interpreter py-2")
        self.assertEqual((self.old / "bin" / "llama" / "cpu" / "llama.cpp-program").read_text(), "the program llama-2")
        self.assertTrue((self.old / "bin" / "llama" / "vulkan" / "llama-program").is_file())
        self.assertEqual(package.read(self.old).helpers,
                         {"python": "py-2", "llama.cpp/cpu": "llama-2", "kiwix-tools": "kiwix-1"})
        kept = self.old / "previous" / "0.1.0"
        self.assertEqual((kept / "python" / "bin" / "python3").read_text(), "the interpreter py-1")
        self.assertTrue((kept / "bin" / "llama" / "cpu" / "llama.cpp-program").is_file())

    def test_a_source_unpacked_only_for_this_gives_up_its_helpers(self):
        newer = make_copy(self.new.parent / "newer", "0.3.0", helpers={"llama.cpp/cpu": "llama-2"})
        upgrade.install(upgrade.source_of(newer), self.old, tests=passes, consume=True)
        self.assertFalse((newer / "bin").exists(), "moved, not copied")
        self.assertEqual((self.old / "bin" / "llama" / "cpu" / "llama.cpp-program").read_text(), "the program llama-2")

    def test_a_checkout_of_the_code_replaces_only_the_code(self):
        checkout = self.new.parent / "checkout"
        for path, text in code("0.4.0").items():
            write(checkout / path, text)
        source = upgrade.source_of(checkout)
        self.assertIsNone(source.root)
        outcome = upgrade.install(source, self.old, tests=passes)
        self.assertEqual((outcome.new, outcome.parts), ("0.4.0", ["app"]))

    def test_only_the_last_previous_version_is_kept(self):
        self.install()
        newer = make_copy(self.new.parent / "newer", "0.3.0")
        upgrade.install(upgrade.source_of(newer), self.old, tests=passes)
        self.assertEqual([p.name for p in (self.old / "previous").iterdir()], ["0.2.0"])


class Refusals(UpgradeCase):
    def unchanged(self, why: str, **options) -> None:
        before = tree(self.old)
        with self.assertRaisesRegex(UpgradeError, why):
            self.install(**options)
        self.assertEqual(tree(self.old, "upgrade/"), before)

    def test_the_same_version_or_an_older_one(self):
        (self.new / "app" / "museion" / "version.py").write_text('VERSION = "0.1.0"\n')
        self.unchanged("already version 0.1.0")
        (self.new / "app" / "museion" / "version.py").write_text('VERSION = "0.0.9"\n')
        self.unchanged("newer than this one")
        self.assertEqual(self.install(force=True).new, "0.0.9")

    def test_a_folder_that_is_not_a_copy(self):
        with self.assertRaisesRegex(UpgradeError, "not a Museion folder"):
            upgrade.install(upgrade.source_of(self.new), self.new.parent / "nowhere", tests=passes)
        with self.assertRaisesRegex(UpgradeError, "not a copy of Museion"):
            upgrade.source_of(self.old / "library")
        with self.assertRaisesRegex(UpgradeError, "this copy of Museion itself"):
            upgrade.install(upgrade.source_of(self.new), self.new, tests=passes, force=True)

    def test_a_copy_for_another_system(self):
        other = "linux" if sys.platform == "win32" else "windows"
        held = package.read(self.old)
        held.platform = other
        package.write(self.old, held)
        self.unchanged(f"a copy for {other}")
        # A copy from before the record is known by its launcher.
        package.path(self.old).unlink()
        write(self.old / ("museion.sh" if other == "linux" else "Museion.cmd"), "")
        self.unchanged(f"a copy for {other}")

    def test_not_enough_room(self):
        with mock.patch.object(upgrade.disk, "free_space", lambda path: 1 << 20):
            self.unchanged("free for the upgrade")

    def test_a_new_version_that_fails_its_tests_is_not_installed(self):
        self.unchanged("did not pass its own tests", tests=lambda app, log: False)

    def test_code_changed_while_the_upgrade_was_prepared(self):
        upgrade.stage(upgrade.source_of(self.new), self.old, tests=passes)
        write(self.old / "app" / "museion" / "ask.py", "def ask():\n    return 'edited meanwhile'\n")
        with self.assertRaisesRegex(UpgradeError, "changed while the upgrade was being prepared"):
            upgrade.switch_staged(self.old)
        self.assertEqual(package.code_version(self.old / "app"), "0.1.0")

    def test_nothing_staged(self):
        with self.assertRaisesRegex(UpgradeError, "nothing is ready"):
            upgrade.switch_staged(self.old)


class BeforeVersions(UpgradeCase):
    def test_a_copy_from_before_versions_is_upgraded_whole(self):
        (self.old / "app" / "museion" / "version.py").unlink()
        package.path(self.old).unlink()
        outcome = self.install()
        self.assertEqual((outcome.old, outcome.parts), ("", ["app", "python", "bin"]))
        self.assertTrue((self.old / "previous" / "before-versions" / "app" / "museion" / "ask.py").is_file())
        self.assertEqual(upgrade.go_back(self.old), ("0.2.0", "before-versions"))

    def test_without_a_record_of_what_was_shipped_the_new_code_goes_in_as_it_is(self):
        (self.old / upgrade.ORIGINAL).unlink()
        write(self.old / "app" / "museion" / "ask.py", "def ask():\n    return 'mine'\n")
        self.assertIsNone(upgrade.local_changes(self.old))
        outcome = self.install()
        self.assertEqual((outcome.carried, outcome.aside), ([], []))
        self.assertEqual(self.app("museion/ask.py"), NEW_ASK)
        self.assertIn("mine", (self.old / "previous" / "0.1.0" / "app" / "museion" / "ask.py").read_text())


class MyChanges(UpgradeCase):
    def test_changes_are_carried_into_the_new_code(self):
        write(self.old / "app" / "museion" / "ask.py", code("0.1.0")["museion/ask.py"].replace("return 1", "return 2"))
        write(self.old / "app" / "museion" / "tuning.toml", "passages = 20\nlength = 400\n")
        write(self.old / "app" / "museion" / "mine.py", "my own module\n")
        (self.old / "app" / "tests" / "test_ask.py").unlink()
        outcome = self.install()
        self.assertEqual(outcome.carried, ["museion/ask.py", "museion/mine.py", "museion/tuning.toml", "tests/test_ask.py"])
        self.assertEqual(outcome.aside, [])
        self.assertEqual(self.app("museion/ask.py"), NEW_ASK.replace("return 1", "return 2"))
        self.assertEqual(self.app("museion/tuning.toml"), "passages = 20\nlength = 400\n")
        self.assertEqual(self.app("museion/mine.py"), "my own module\n")
        self.assertFalse((self.old / "app" / "tests" / "test_ask.py").exists())
        self.assertFalse((self.old / "app" / appmerge.NOTES).exists())
        self.assertFalse((self.old / "my-changes").exists())
        # They are still changes, measured against the new version.
        self.assertEqual(upgrade.local_changes(self.old), {
            "museion/ask.py": "changed", "museion/mine.py": "added",
            "museion/tuning.toml": "changed", "tests/test_ask.py": "removed"})

    def test_a_change_to_the_same_lines_is_set_aside_with_a_note(self):
        mine = code("0.1.0")["museion/ask.py"].replace("'old'", "'mine'")
        write(self.old / "app" / "museion" / "ask.py", mine)
        outcome = self.install()
        self.assertEqual(outcome.aside, ["museion/ask.py"])
        self.assertEqual(outcome.aside_folder, "my-changes/from-0.1.0-to-0.2.0")
        self.assertEqual(self.app("museion/ask.py"), NEW_ASK)
        aside = self.old / "my-changes" / "from-0.1.0-to-0.2.0"
        self.assertEqual((aside / "museion" / "ask.py").read_text(), mine)
        self.assertIn("+    return 'mine'", (aside / "museion" / "ask.py.diff").read_text())
        notes = self.app(appmerge.NOTES)
        self.assertIn("museion/ask.py", notes)
        self.assertIn("my-changes/from-0.1.0-to-0.2.0", notes)
        # The note is not itself a change, and the next upgrade does not carry it.
        self.assertEqual(upgrade.local_changes(self.old), {})
        self.assertEqual(switch.history(self.old)[-1]["aside"], ["museion/ask.py"])

    def test_changes_that_break_the_new_version_are_set_aside(self):
        write(self.old / "app" / "museion" / "mine.py", "breaks the tests\n")
        outcome = self.install(tests=lambda app, log: not (app / "museion" / "mine.py").exists())
        self.assertTrue(outcome.dropped_changes)
        self.assertEqual(outcome.aside, ["museion/mine.py"])
        self.assertFalse((self.old / "app" / "museion" / "mine.py").exists())
        self.assertEqual((self.old / outcome.aside_folder / "museion" / "mine.py").read_text(), "breaks the tests\n")
        self.assertIn("did not pass its tests with your changes", self.app(appmerge.NOTES))

    def test_or_kept_if_asked(self):
        write(self.old / "app" / "museion" / "mine.py", "breaks the tests\n")
        outcome = self.install(tests=lambda app, log: not (app / "museion" / "mine.py").exists(), keep_changes=True)
        self.assertFalse(outcome.dropped_changes)
        self.assertTrue((self.old / "app" / "museion" / "mine.py").exists())


class GoingBack(UpgradeCase):
    def test_back_and_forward_again(self):
        write(self.old / "app" / "museion" / "mine.py", "my own module\n")
        before = tree(self.old)
        newer = make_copy(self.new.parent / "newer", "0.3.0", python="py-2", helpers={"llama.cpp/cpu": "llama-2"})
        upgrade.install(upgrade.source_of(newer), self.old, tests=passes)
        self.assertEqual(upgrade.go_back(self.old, self.said.append), ("0.3.0", "0.1.0"))
        self.assertEqual(self.said[-1], "Going back from 0.3.0 to 0.1.0")
        skip = ("previous/", "support/installed.json", *launchers.launchers())
        self.assertEqual(tree(self.old, *skip), before)
        self.assertEqual(upgrade.previous_version(self.old), "0.3.0")
        # Going back can itself be undone.
        self.assertEqual(upgrade.go_back(self.old), ("0.1.0", "0.3.0"))
        self.assertEqual((self.old / "python" / "bin" / "python3").read_text(), "the interpreter py-2")
        self.assertEqual(self.app("museion/mine.py"), "my own module\n")
        self.assertEqual([p.name for p in (self.old / "previous").iterdir()], ["0.1.0"])

    def test_nothing_to_go_back_to(self):
        with self.assertRaisesRegex(UpgradeError, "no previous version"):
            upgrade.go_back(self.old)


class TheSwitch(UpgradeCase):
    def test_a_refused_rename_changes_nothing(self):
        real = switch.move

        def move(src: Path, dst: Path, attempts: int = 60) -> None:
            if src.as_posix().endswith("upgrade/new/app/museion"):
                raise PermissionError(13, "Access is denied", str(src))
            real(src, dst, attempts)

        before = tree(self.old)
        with mock.patch.object(switch, "move", move), self.assertRaisesRegex(UpgradeError, "Is Museion still running"):
            self.install()
        self.assertEqual(tree(self.old, "upgrade/"), before)
        self.assertFalse((self.old / "upgrade" / "journal.json").exists())
        # What was staged is still there, so the switch can be tried again.
        self.assertEqual(upgrade.switch_staged(self.old).new, "0.2.0")
        self.assertEqual(package.code_version(self.old / "app"), "0.2.0")

    def test_a_switch_cut_short_is_finished(self):
        real = switch.move
        count = 0

        def move(src: Path, dst: Path, attempts: int = 60) -> None:
            nonlocal count
            count += 1
            if count == 4:
                raise KeyboardInterrupt  # the power went
            real(src, dst, attempts)

        with mock.patch.object(switch, "move", move), self.assertRaises(KeyboardInterrupt):
            self.install()
        self.assertTrue((self.old / "upgrade" / "journal.json").exists())
        self.assertTrue(switch.recover(self.old, self.said.append))
        self.assertEqual(self.said[-1], "Finishing the switch to 0.2.0 that was cut short")
        self.assertEqual(package.code_version(self.old / "app"), "0.2.0")
        self.assertEqual(self.app("museion/ask.py"), NEW_ASK)
        self.assertEqual(package.read(self.old).version, "0.2.0")
        self.assertFalse((self.old / "upgrade" / "journal.json").exists())
        self.assertFalse(switch.recover(self.old))
        self.assertEqual(len(switch.history(self.old)), 1)

    def test_the_running_python_is_copied_not_moved(self):
        newer = make_copy(self.new.parent / "newer", "0.3.0", python="py-2")
        running = newer / "python" / "bin" / "python3"
        with mock.patch.object(sys, "executable", str(running)):
            upgrade.install(upgrade.source_of(newer), self.old, tests=passes, consume=True)
            self.assertTrue(running.is_file())
            self.assertEqual((self.old / "python" / "bin" / "python3").read_text(), "the interpreter py-2")
        # And an upgrade run by the copy's own Python cannot replace it.
        newest = make_copy(self.new.parent / "newest", "0.4.0", python="py-3")
        with mock.patch.object(sys, "executable", str(self.old / "python" / "bin" / "python3")), \
                self.assertRaisesRegex(UpgradeError, "replaces the Python that is running it"):
            upgrade.install(upgrade.source_of(newest), self.old, tests=passes)

    def test_tidy_removes_what_is_left_over(self):
        write(self.old / "upgrade" / "incoming" / "app" / "x.py", "")
        write(self.old / "previous" / "0.0.5" / "python" / "bin" / "python3", "left by going back")
        switch.tidy(self.old)
        self.assertFalse((self.old / "upgrade" / "incoming").exists())
        self.assertFalse((self.old / "previous" / "0.0.5").exists())
        # Not while a switch is unfinished.
        write(self.old / "upgrade" / "incoming" / "app" / "x.py", "")
        write(self.old / "upgrade" / "journal.json", json.dumps({"moves": []}))
        switch.tidy(self.old)
        self.assertTrue((self.old / "upgrade" / "incoming").exists())


if __name__ == "__main__":
    unittest.main()
