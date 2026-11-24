"""The upgrade commands and the page's side of upgrades, over stand-in
copies: finding the newer copy in a download, refusing a copy that is
running, and what each command prints and leaves behind."""

import contextlib
import io
import json
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from museion import cli, package, switch, upgrade, upgrade_cli, upgrade_commands
from museion.appstate import UiError
from museion.upgrade import UpgradeError
from upgrade_copies import code, make_copy, write


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.old = make_copy(self.base / "old", "0.1.0")
        self.new = make_copy(self.base / "new", "0.2.0", code("0.2.0", {"museion/ask.py": "new\n"}))
        # This program is the new copy; no Museion is running; the stand-in
        # code has no tests to run.
        for target, name, value in ((upgrade, "this_museion", lambda: upgrade.source_of(self.new)),
                                    (upgrade_cli, "museion_at", lambda port: None),
                                    (upgrade_cli, "own_root", lambda: self.old),
                                    (upgrade, "run_tests", lambda app, log: True)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv: str) -> tuple[str, int]:
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                cli.main(list(argv))
            except SystemExit as e:
                code = int(e.code or 0)
        return out.getvalue(), code


class Commands(Case):
    def test_into_puts_this_version_into_that_copy(self):
        write(self.old / "app" / "museion" / "mine.py", "mine\n")
        out, code = self.run_cli("upgrade", "--into", str(self.old))
        self.assertEqual(code, 0, out)
        self.assertIn("is now version 0.2.0 (was 0.1.0)", out)
        self.assertIn("1 change you had made to the code was carried over: museion/mine.py", out)
        self.assertIn("upgrade --back", out)
        self.assertEqual(package.code_version(self.old / "app"), "0.2.0")

    def test_in_two_steps_as_the_page_does_it(self):
        out, code = self.run_cli("upgrade", "--into", str(self.old), "--stage-only")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[-1], upgrade_cli.READY)
        self.assertEqual(package.code_version(self.old / "app"), "0.1.0")
        with mock.patch.object(upgrade_cli, "relaunch") as relaunch:
            out, code = self.run_cli("upgrade", "--into", str(self.old), "--switch-only", "--relaunch", "8070")
        self.assertEqual(code, 0, out)
        relaunch.assert_called_once_with(self.old.resolve(), 8070)
        self.assertEqual(package.code_version(self.old / "app"), "0.2.0")

    def test_a_switch_that_cannot_happen_tells_the_page_why(self):
        with mock.patch.object(upgrade_cli, "relaunch") as relaunch:
            out, code = self.run_cli("upgrade", "--into", str(self.old), "--switch-only", "--relaunch", "8070")
        self.assertEqual(code, 1)
        self.assertIn("Not upgraded: nothing is ready", out)
        relaunch.assert_called_once()  # the old version comes back up
        state = upgrade_commands.upgrade_state(types.SimpleNamespace(drive=types.SimpleNamespace(root=self.old)))
        self.assertIn("nothing is ready", state["failed"])
        # Said once.
        state = upgrade_commands.upgrade_state(types.SimpleNamespace(drive=types.SimpleNamespace(root=self.old)))
        self.assertIsNone(state["failed"])

    def test_a_copy_museion_is_running_from_is_refused(self):
        for hello, refused in (({"app": "museion", "root": str(self.old)}, True),
                               ({"app": "museion", "version": "0.1.0"}, True),  # 0.1.0 does not say where
                               ({"app": "museion", "root": str(self.new)}, False),
                               ({"app": "museion", "root": ""}, False)):  # a git checkout
            with mock.patch.object(upgrade_cli, "museion_at", lambda port: hello):
                if refused:
                    with self.assertRaisesRegex(UpgradeError, "Museion is running"):
                        upgrade_cli.closed(self.old)
                else:
                    upgrade_cli.closed(self.old)

    def test_back(self):
        self.run_cli("upgrade", "--into", str(self.old))
        out, code = self.run_cli("upgrade", "--back")
        self.assertEqual(code, 0, out)
        self.assertIn("is version 0.1.0 again", out)
        self.assertEqual(package.code_version(self.old / "app"), "0.1.0")
        out, code = self.run_cli("upgrade", "--back")
        self.assertIn("is version 0.2.0 again", out)

    def test_back_is_handed_to_the_kept_python_when_the_upgrade_replaced_it(self):
        newer = make_copy(self.base / "newer", "0.3.0", python="py-2")
        upgrade.install(upgrade.source_of(newer), self.old, tests=None)
        command, folder = upgrade_cli.back_command(self.old)
        kept = self.old / "previous" / "0.1.0" / "python"
        self.assertTrue(Path(command[0]).resolve().is_relative_to(kept.resolve()), command)
        self.assertEqual((command[1:], folder), (["-m", "museion", "upgrade", "--back"], self.old / "app"))

    def test_nothing_named(self):
        out, code = self.run_cli("upgrade")
        self.assertEqual(code, 1)
        self.assertIn("say what to upgrade from", out)

    def test_changes(self):
        out, _ = self.run_cli("changes")
        self.assertIn("as it was shipped", out)
        write(self.old / "app" / "museion" / "ask.py", "def ask():\n    return 'mine'\n")
        out, _ = self.run_cli("changes")
        self.assertIn("1 file differs from the code as it was shipped. An upgrade carries it over.", out)
        write(self.old / "app" / "museion" / "mine.py", "mine\n")
        out, _ = self.run_cli("changes", "--diff")
        self.assertIn("2 files differ", out)
        self.assertRegex(out, r"changed\s+museion/ask.py")
        self.assertRegex(out, r"added\s+museion/mine.py")
        self.assertIn("+    return 'mine'", out)


class TheNewerCopy(Case):
    def test_a_folder_is_used_where_it_is(self):
        self.assertEqual(upgrade_cli.newer_copy(self.new, self.old), (self.new, False))

    def test_a_download_is_unpacked_inside_the_copy(self):
        flat = self.base / "museion-0.2.0-windows.zip"
        with zipfile.ZipFile(flat, "w") as z:
            for f in self.new.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(self.new).as_posix())
        nested = self.base / "museion-0.2.0-linux.tar.gz"
        with tarfile.open(nested, "w:gz") as tf:
            tf.add(self.new, "Museion")
        said: list[str] = []
        for archive, where in ((flat, "upgrade/incoming"), (nested, "upgrade/incoming/Museion")):
            folder, unpacked = upgrade_cli.newer_copy(archive, self.old, said.append)
            self.assertEqual((folder, unpacked), (self.old / where, True))
            self.assertEqual(upgrade.source_of(folder).version, "0.2.0")
        self.assertEqual(said[0], "Unpacking museion-0.2.0-windows.zip")

    def test_what_is_not_a_download(self):
        junk = self.base / "holiday.zip"
        with zipfile.ZipFile(junk, "w") as z:
            z.writestr("photo.jpg", "x")
        write(self.base / "notes.txt", "x")
        for path, why in ((junk, "does not hold a copy of Museion"), (self.base / "notes.txt", "not a Museion download"),
                          (self.base / "nowhere.zip", "no file or folder")):
            with self.assertRaisesRegex(UpgradeError, why):
                upgrade_cli.newer_copy(path, self.old)


class ThePage(Case):
    def state(self, root: Path | None, events: list | None = None):
        emit = (lambda name, payload: events.append(payload["line"])) if events is not None else (lambda *a: None)
        return types.SimpleNamespace(drive=types.SimpleNamespace(root=root) if root else None, emit=emit)

    def test_state(self):
        self.assertEqual(upgrade_commands.upgrade_state(self.state(None))["root"], None)
        write(self.old / "app" / "museion" / "mine.py", "mine\n")
        st = upgrade_commands.upgrade_state(self.state(self.old))
        self.assertEqual((st["root"], st["previous"], st["changes"], st["last"]),
                         (str(self.old), None, {"museion/mine.py": "added"}, None))
        upgrade.install(upgrade.source_of(self.new), self.old, tests=None)
        st = upgrade_commands.upgrade_state(self.state(self.old))
        self.assertEqual((st["previous"], st["last"]["to"], st["last"]["carried"]), ("0.1.0", "0.2.0", ["museion/mine.py"]))

    def test_another_copy_is_brought_up_to_this_version(self):
        events: list[str] = []
        said = upgrade_commands.upgrade_other(self.state(self.new, events), str(self.old))
        self.assertIn("is now version 0.2.0", said)
        self.assertIn("Switching to the new version", events)
        with self.assertRaisesRegex(UiError, "already version 0.2.0"):
            upgrade_commands.upgrade_other(self.state(self.new), str(self.old))
        with self.assertRaisesRegex(UiError, "choose the folder"):
            upgrade_commands.upgrade_other(self.state(self.new), "")

    def test_upgrading_this_copy_stages_then_leaves_the_switch_waiting(self):
        newer = make_copy(self.base / "newer", "9.0.0")
        write(newer / "app" / "museion" / "upgrade_cli.py", "")
        ran: list[list[str]] = []

        def watch(command, cwd, report):
            ran.append(command)
            report("Testing the new version")

        events: list[str] = []
        # (The running code's version is this copy's; these tests also run
        # inside a copy standing in for a far later version.)
        with (mock.patch.object(upgrade_commands, "_watch", watch), mock.patch.object(upgrade_cli, "spawn") as spawn,
              mock.patch.object(upgrade_commands, "VERSION", "0.1.0")):
            result = upgrade_commands.upgrade_from(self.state(self.old, events), str(newer), 8123)
        self.assertEqual(result, {"to": "9.0.0"})
        self.assertEqual(ran[0][1:], ["-m", "museion", "upgrade", "--into", str(self.old), "--stage-only"])
        # (resolved: a temporary folder is reached through a link on macOS)
        self.assertTrue(Path(ran[0][0]).is_relative_to(newer.resolve() / "python"))
        switched = spawn.call_args.args[0]
        self.assertEqual(switched[5:], [str(self.old), "--switch-only", "--wait-port", "8123", "--relaunch", "8123"])
        self.assertEqual(events, ["Testing the new version"])

    def test_upgrading_this_copy_refuses_what_cannot_install_itself(self):
        with self.assertRaisesRegex(UiError, "cannot|does not hold a whole copy"):
            upgrade_commands.upgrade_from(self.state(self.old), str(self.new), 8123)  # no upgrade_cli.py in it
        older = make_copy(self.base / "older", "0.0.1")
        write(older / "app" / "museion" / "upgrade_cli.py", "")
        with self.assertRaisesRegex(UiError, "this copy is already"):
            upgrade_commands.upgrade_from(self.state(self.old), str(older), 8123)
        with self.assertRaisesRegex(UiError, "git checkout"):
            upgrade_commands.upgrade_from(self.state(None), str(self.new), 8123)

    def test_going_back_leaves_a_program_waiting(self):
        with self.assertRaisesRegex(UiError, "No previous version"):
            upgrade_commands.upgrade_back(self.state(self.old), 8123)
        upgrade.install(upgrade.source_of(self.new), self.old, tests=None)
        with mock.patch.object(upgrade_cli, "spawn") as spawn:
            self.assertEqual(upgrade_commands.upgrade_back(self.state(self.old), 8123), {"to": "0.1.0"})
        self.assertEqual(spawn.call_args.args[0][1:],
                         ["-m", "museion", "upgrade", "--back", "--wait-port", "8123", "--relaunch", "8123"])
        self.assertEqual(spawn.call_args.args[0][0], sys.executable)


if __name__ == "__main__":
    unittest.main()
