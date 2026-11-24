"""Preparing a drive: what it will hold, the layout, the copy of Museion with
its launchers and config, and the manifest that checks it years later.

Nothing is downloaded: the tool installers are replaced with ones that leave
an empty program where the real one would go, and the plans are skeletons,
which carry no content or models.
"""

import dataclasses
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from museion import catalog, hardware, launchers, manifest, package, portable, prepare, version
from museion.appstate import AppState

CPU = hardware.Hardware(hardware.Accelerator(hardware.CPU), 16000, hardware.MODEST)
SMALL = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"


def exe(stem: str) -> str:
    return f"{stem}.exe" if sys.platform == "win32" else stem


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "drive"
        # A llama.cpp build already in place: the catalogue has none for
        # every platform the tests run on, and a build present is never
        # fetched.
        touch(self.root / "bin" / "llama" / "cpu" / exe("llama-server"))
        self.installed: list[tuple] = []
        self.progress: list[prepare.Progress] = []

    def tearDown(self):
        self.tmp.cleanup()

    def install_selected(self, accel, dest, only=(), on_step=None):
        self.installed.append((accel, tuple(only)))
        for name in only:
            stem = {"kiwix-tools": "kiwix-serve", "llama.cpp": "llama-server"}.get(name)
            if stem:
                touch(Path(dest) / exe(stem))

    def install_drive_tool(self, tool, root, report):
        self.installed.append((tool.id,))
        touch(prepare.python_exe(root) if tool.id == "python" else root / "bin" / exe(tool.provides[0]))

    def run_prepare(self, **plan) -> Path:
        plan = prepare.Plan(dest=self.root, accelerators=["cpu"], skeleton=True, build_index=False, **plan)
        with mock.patch("museion.tools.install_selected", self.install_selected), \
             mock.patch("museion.prepare.install_drive_tool", self.install_drive_tool):
            return prepare.run(plan, self.progress.append)

    def test_the_estimate_counts_what_is_still_to_fetch(self):
        est = prepare.estimate(prepare.Plan(dest=self.root, accelerators=["cpu"], skeleton=True))
        names = dict(est.items)
        self.assertIn("Museion's code", names)
        self.assertIn("opencode", names)
        self.assertIn("ripgrep", names)
        self.assertGreater(names["Museion's code"], 0)
        self.assertEqual(est.bytes, sum(names.values()))
        # The llama.cpp build is already there, so it is not still to fetch.
        self.assertEqual(est.remaining, est.bytes - names["llama.cpp (cpu)"])
        self.assertGreater(est.free_bytes, 0)

        full = prepare.estimate(prepare.Plan(dest=self.root, accelerators=["cpu"],
                                             models=["qwen3-4b-instruct-q4"]))
        titles = [name for name, _ in full.items]
        reranker = catalog.reranker_model()
        self.assertIn(reranker.title, titles)
        _, sources = catalog.bundle("go-bag")
        self.assertTrue(all(s.title in titles for s in sources))
        self.assertGreater(full.remaining, est.remaining + reranker.size_bytes)

    def test_the_plan_is_checked_against_the_catalogue(self):
        with self.assertRaisesRegex(ValueError, "no model"):
            prepare.estimate(prepare.Plan(dest=self.root, accelerators=["cpu"], models=["nonesuch"]))
        with self.assertRaisesRegex(ValueError, "at least one chat model"):
            prepare.estimate(prepare.Plan(dest=self.root, accelerators=["cpu"]))
        with self.assertRaisesRegex(ValueError, "not a chat model"):
            prepare.estimate(prepare.Plan(dest=self.root, accelerators=["cpu"], models=["qwen3-reranker-0.6b"]))

    def test_a_drive_without_room_is_refused_before_anything_is_written(self):
        with mock.patch("museion.disk.free_space", return_value=0), self.assertRaisesRegex(RuntimeError, "needs"):
            self.run_prepare()
        self.assertFalse((self.root / portable.MARKER).exists())

    def test_a_skeleton_drive_holds_museion_and_runs_it(self):
        root = self.run_prepare()
        self.assertEqual(root, self.root)
        self.assertTrue((root / portable.MARKER).read_text(encoding="utf-8").startswith("drive-"))
        for folder in ("library", "models", "support/LICENSES"):
            self.assertTrue((root / folder).is_dir(), folder)
        self.assertTrue(prepare.python_exe(root).exists())
        self.assertTrue((root / "bin" / exe("opencode")).exists())
        # opencode's search runs rg, and downloads it if it is not on the PATH.
        self.assertTrue((root / "bin" / exe("rg")).exists())
        self.assertTrue((root / "bin" / exe("kiwix-serve")).exists())

        # Museion's code, what someone changing it needs, and no caches.
        app = root / "app"
        for part in ("museion/app.py", "museion/ui/index.html", "catalog", "drive/opencode.toml",
                     "agents/librarian/agent.toml", "tests/test_prepare.py", "AGENTS.md"):
            self.assertTrue((app / part).exists(), part)
        self.assertEqual(list(app.rglob("__pycache__")), [])
        with zipfile.ZipFile(root / "support" / prepare.ORIGINAL_ZIP) as z:
            names = set(z.namelist())
        self.assertIn("app/museion/app.py", names)
        self.assertIn("app/AGENTS.md", names)

        for name, text in launchers.launchers().items():
            data = (root / name).read_bytes()
            self.assertIn(b"-m museion", data)
            if name.endswith(".cmd"):
                self.assertEqual(data.count(b"\r\n"), text.count("\n"))
        readme = (root / "README.txt").read_text(encoding="utf-8")
        self.assertTrue(all(name in readme for name in launchers.launchers()))
        # What the drive holds is written down for a later upgrade to read.
        held = package.read(root)
        self.assertEqual((held.version, held.platform), (version.VERSION, catalog.current_platform()))
        self.assertEqual(package.app_version(root), version.VERSION)
        self.assertIn("kiwix-tools", held.helpers)
        # A skeleton says it has no library yet, not that it carries one.
        self.assertIn("does not hold a library", readme)
        self.assertNotIn("This drive carries", readme)
        self.assertIn("Museion itself", (root / "support" / "LICENSES" / "README.txt").read_text(encoding="utf-8"))
        self.assertEqual(manifest.read(root), [])

        # The config is relative, names the CPU build and the catalogue's
        # smallest chat model for the first run to fetch.
        data = json.loads((root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["library_dir"], "library")
        self.assertEqual(data["llama_exe"], f"bin/llama/cpu/{exe('llama-server')}")
        self.assertEqual(data["chat_model"], f"models/{SMALL}")

        # And Museion, started from the drive's app/, finds it all there.
        drive = portable.detect(app, Path(self.tmp.name) / "local")
        self.assertEqual(drive.root, root)
        with mock.patch("museion.hardware.detect", return_value=CPU):
            cfg = AppState(drive.config_path(), drive.tuning_path(), drive=drive).config()
        self.assertEqual(cfg.library_dir, root / "library")
        self.assertEqual(cfg.kiwix_exe, root / "bin" / exe("kiwix-serve"))
        self.assertEqual(cfg.llama_exe, root / "bin" / "llama" / "cpu" / exe("llama-server"))
        self.assertEqual(cfg.chat_model, root / "models" / SMALL)

    def test_every_part_of_the_repository_is_carried(self):
        # The agents folder was added to the repository and not to APP_PARTS,
        # and drives went out with an empty Agents tab. In a git checkout,
        # everything tracked at the top is carried unless it is only for
        # GitHub or git.
        try:
            out = subprocess.run(["git", "ls-files"], cwd=prepare.APP_DIR, capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        tracked = {line.split("/")[0] for line in out.stdout.splitlines()}
        missing = {t for t in tracked if not t.startswith(".")} - set(prepare.APP_PARTS)
        self.assertEqual(missing, set(), "add these to prepare.APP_PARTS")

    def test_preparing_again_keeps_what_is_there(self):
        self.run_prepare()
        marker = (self.root / portable.MARKER).read_text(encoding="utf-8")
        changed = self.root / "app" / "AGENTS.md"
        changed.write_text("Changed on the drive.\n", encoding="utf-8")
        (self.root / "config.json").write_text('{"chat_port": 9000}', encoding="utf-8")
        self.installed.clear()

        self.run_prepare()
        self.assertEqual(self.installed, [])
        self.assertEqual((self.root / portable.MARKER).read_text(encoding="utf-8"), marker)
        self.assertEqual(changed.read_text(encoding="utf-8"), "Changed on the drive.\n")
        self.assertEqual(json.loads((self.root / "config.json").read_text(encoding="utf-8")), {"chat_port": 9000})
        notes = [p.item for p in self.progress if p.kind == prepare.NOTE]
        self.assertIn("app/ already there; left as it is", notes)

    def test_a_helper_cut_off_while_unpacking_is_installed_again(self):
        # The program unpacked, its runtime not yet: the network dropped.
        real = self.install_selected

        def cut_off(accel, dest, only=(), on_step=None):
            if accel == "cuda" and not cut:
                cut.append(dest)
                touch(Path(dest) / exe("llama-server"))
                raise OSError("connection reset while fetching cudart")
            real(accel, dest, only, on_step)
        cut = []
        plan = prepare.Plan(dest=self.root, accelerators=["cuda"], skeleton=True, build_index=False)
        # No platform the tests run on has every build in the catalogue.
        room = mock.Mock(free_bytes=1 << 50, remaining=0)
        with mock.patch("museion.tools.install_selected", cut_off), \
             mock.patch("museion.prepare.estimate", return_value=room), \
             mock.patch("museion.prepare.install_drive_tool", self.install_drive_tool):
            with self.assertRaises(OSError):
                prepare.run(plan, self.progress.append)
            self.assertIsNone(prepare.tools.find_exe(self.root / "bin" / "llama" / "cuda", "llama-server"))
            prepare.run(plan, self.progress.append)
        self.assertIn(("cuda", ("llama.cpp",)), self.installed)
        self.assertTrue((self.root / "bin" / "llama" / "cuda" / exe("llama-server")).exists())
        self.assertFalse((self.root / "bin" / "llama" / "cuda.part").exists())

    def test_without_opencode_neither_it_nor_ripgrep_is_carried(self):
        self.run_prepare(opencode=False)
        self.assertFalse((self.root / "bin" / exe("opencode")).exists())
        self.assertFalse((self.root / "bin" / exe("rg")).exists())
        self.assertNotIn(("opencode",), self.installed)
        self.assertNotIn(("ripgrep",), self.installed)


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.carry(catalog.bundle("go-bag")[1][0], "library", b"an archive")
        self.model = self.carry(catalog.reranker_model(), "models", b"a model")
        manifest.write(self.root, "go-bag", ["cpu", "vulkan"], [self.archive, self.model])

    def tearDown(self):
        self.tmp.cleanup()

    def carry(self, item, folder: str, data: bytes):
        """A catalogue entry for a small file put on the drive."""
        path = self.root / folder / item.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return dataclasses.replace(item, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def test_it_lists_what_was_carried(self):
        entries = manifest.read(self.root)
        self.assertEqual([e["path"] for e in entries],
                         [f"library/{self.archive.filename}", f"models/{self.model.filename}"])
        self.assertEqual(entries[1]["sha256"], self.model.sha256)
        text = (self.root / manifest.NAME).read_text(encoding="utf-8")
        self.assertIn("accelerators: [cpu, vulkan]", text)

    def test_an_untouched_drive_verifies(self):
        seen = []
        self.assertEqual(manifest.verify(self.root, lambda item, done, total: seen.append((done, total))), [])
        self.assertEqual(seen, [(1, 2), (2, 2)])

    def test_damage_is_named(self):
        (self.root / "library" / self.archive.filename).write_bytes(b"an archivf")
        (self.root / "models" / self.model.filename).write_bytes(b"short")
        problems = manifest.verify(self.root)
        self.assertIn("checksum no longer matches", problems[0])
        self.assertIn("is 5 bytes, not 7", problems[1])
        (self.root / "models" / self.model.filename).unlink()
        self.assertIn("is missing", manifest.verify(self.root)[1])
        (self.root / manifest.NAME).unlink()
        self.assertIn("no manifest", manifest.verify(self.root)[0])


class DriveToolTest(unittest.TestCase):
    """install_drive_tool itself, with the download replaced by an archive
    laid out as the real one is."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "drive"

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, tool_id: str, members: list[str]) -> None:
        tool = next(t for t in catalog.drive_tools() if t.id == tool_id)

        def fetch(title, urls, target, size, sha256, report):
            with tarfile.open(target, "w:gz") as tf:
                for name in members:
                    info = tarfile.TarInfo(name)
                    info.size = 2
                    tf.addfile(info, io.BytesIO(b"#!"))
        with mock.patch("museion.prepare.fetch_verified", fetch):
            prepare.install_drive_tool(tool, self.root, lambda p: None)

    def test_the_portable_python_lands_where_the_drive_runs_it(self):
        # What an earlier Python and an interrupted run left behind.
        touch(self.root / "python" / "old")
        touch(self.root / "python.part" / "half")
        self.install("python", [prepare.python_exe(Path()).as_posix(), "python/lib/os.py"])
        self.assertTrue(prepare.python_exe(self.root).is_file())
        self.assertTrue((self.root / "python" / "lib" / "os.py").is_file())
        self.assertFalse((self.root / "python" / "old").exists())
        self.assertFalse((self.root / "python.part").exists())


if __name__ == "__main__":
    unittest.main()
