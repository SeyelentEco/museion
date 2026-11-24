"""Running from a drive: finding it, its relative config, and the machine
profile that holds what belongs to each computer rather than the drive."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from museion import commands, hardware, package, portable
from museion.appstate import AppState
from museion.config import Config, load
from museion.portable import MachineProfile

CPU = hardware.Hardware(hardware.Accelerator(hardware.CPU), 16000, hardware.MODEST)
GPU = hardware.Hardware(hardware.Accelerator(hardware.VULKAN, "Radeon", 12000), 32000, hardware.CAPABLE)
SMALL = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
RERANKER = "qwen3-reranker-0.6b-q8_0.gguf"


def exe(stem: str) -> str:
    return f"{stem}.exe" if sys.platform == "win32" else stem


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class Drive(unittest.TestCase):
    """A drive at E/ with app/ on it, and this computer's own folder beside it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "E"
        self.app = self.root / "app"
        self.app.mkdir(parents=True)
        self.local = base / "local"

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, marker: str = "drive-abc123\n") -> portable.Portable:
        (self.root / portable.MARKER).write_text(marker, encoding="utf-8")
        found = portable.detect(self.app, self.local)
        self.assertIsNotNone(found)
        return found

    def stock(self) -> None:
        """A drive carrying a CPU and a Vulkan build, the small model, the
        reranker and kiwix-serve."""
        for kind in ("cpu", "vulkan"):
            touch(self.root / "bin" / "llama" / kind / exe("llama-server"))
        touch(self.root / "bin" / exe("kiwix-serve"))
        touch(self.root / "models" / SMALL)
        touch(self.root / "models" / RERANKER)
        (self.root / "library").mkdir()


class DriveTest(Drive):
    def test_no_marker_means_not_a_drive(self):
        self.assertIsNone(portable.detect(self.app, self.local))

    def test_the_marker_names_this_drives_profile(self):
        d = self.drive("drive-abc123\n")
        self.assertEqual(d.root, self.root)
        self.assertEqual(d.cache_dir, self.local / "portable" / "drive-abc123")
        self.assertEqual(d.profile_path(), d.cache_dir / "machine.json")
        self.assertTrue(d.cache_dir.is_dir())

    def test_a_marker_edited_by_hand_still_names_a_safe_folder(self):
        self.assertEqual(self.drive("﻿../../etc  x\n").id, "etcx")
        self.assertEqual(self.drive("").id, "drive")

    def test_paths_on_the_drive_are_saved_relative_to_it(self):
        elsewhere = self.root.parent / "elsewhere"
        cfg = Config(library_dir=self.root / "library", extra_dirs=[self.root / "more", elsewhere],
                     chat_model=self.root / "models" / SMALL, base=self.root)
        cfg.save(self.root / "config.json")
        data = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["library_dir"], "library")
        self.assertEqual(data["chat_model"], f"models/{SMALL}")
        self.assertEqual(data["extra_dirs"][0], "more")
        # Outside the drive stays absolute: it is on this computer, not the drive.
        self.assertEqual(Path(data["extra_dirs"][1]), elsewhere)
        self.assertNotIn("base", data)
        # Read back on a drive mounted somewhere else, everything follows it.
        moved = self.root.parent / "H"
        back = load(self.root / "config.json").resolved(moved)
        self.assertEqual(back.library_dir, moved / "library")
        self.assertEqual(back.chat_model, moved / "models" / SMALL)
        self.assertEqual(back.extra_dirs[0], moved / "more")

    def test_the_profile_round_trips_and_ignores_what_is_gone(self):
        path = self.local / "machine.json"
        llama = touch(self.root / "bin" / "llama" / "cuda" / exe("llama-server"))
        MachineProfile("pc", llama, self.root / "models" / "gone.gguf", ["-c", "8192"], 41, True).save(path)
        p = MachineProfile.load(path)
        self.assertEqual((p.machine, p.llama_exe, p.chat_gpu_layers, p.fitted), ("pc", llama, 41, True))
        cfg = Config(chat_model=self.root / "models" / SMALL, chat_args=["-c", "4096"])
        p.apply(cfg)
        self.assertEqual(cfg.llama_exe, llama)
        # The profile's model was deleted from the drive: the drive's stands.
        self.assertEqual((cfg.chat_model.name, cfg.chat_args), (SMALL, ["-c", "4096"]))
        self.assertIsNone(MachineProfile.load(self.local / "none.json"))

    def test_a_drive_under_a_new_letter_keeps_this_computers_build(self):
        # Measured with the drive at E/; now it is at H/. The profile's
        # absolute paths are found again under the new root, not dropped for
        # the drive's CPU default.
        old = self.root.parent / "E-before"
        p = MachineProfile("pc", old / "bin" / "llama" / "cuda" / exe("llama-server"),
                           old / "models" / SMALL, ["-c", "8192"], 41, True)
        llama = touch(self.root / "bin" / "llama" / "cuda" / exe("llama-server"))
        model = touch(self.root / "models" / SMALL)
        cpu = self.root / "bin" / "llama" / "cpu" / exe("llama-server")
        cfg = Config(llama_exe=cpu)
        self.assertTrue(p.apply(cfg, self.root))
        self.assertEqual((cfg.llama_exe, cfg.chat_model, cfg.chat_gpu_layers), (llama, model, 41))
        self.assertEqual(p.llama_exe, llama)
        # Nothing moved the second time.
        self.assertFalse(p.apply(Config(), self.root))

    def test_a_copy_of_the_drive_uses_its_own_files_while_the_stick_is_still_in(self):
        stick = self.root.parent / "E-stick"
        p = MachineProfile("pc", touch(stick / "bin" / "llama" / "cuda" / exe("llama-server")),
                           touch(stick / "models" / SMALL), ["-c", "8192"], 41, True)
        llama = touch(self.root / "bin" / "llama" / "cuda" / exe("llama-server"))
        model = touch(self.root / "models" / SMALL)
        cfg = Config()
        self.assertTrue(p.apply(cfg, self.root))
        self.assertEqual((cfg.llama_exe, cfg.chat_model), (llama, model))

    def test_rebasing_needs_more_than_a_file_name(self):
        touch(self.root / "llama-server")
        self.assertIsNone(portable.rebase(self.root.parent / "gone" / "llama-server", self.root))
        touch(self.root / "bin" / "llama-server")
        self.assertEqual(portable.rebase(self.root.parent / "gone" / "x" / "bin" / "llama-server", self.root),
                         self.root / "bin" / "llama-server")

    def test_a_computer_without_a_gpu_gets_the_cpu_build_and_nothing_to_fit(self):
        self.stock()
        cfg = Config()
        profile = portable.adapt(cfg, CPU, self.root)
        self.assertEqual(cfg.llama_exe, self.root / "bin" / "llama" / "cpu" / exe("llama-server"))
        self.assertEqual(cfg.kiwix_exe, self.root / "bin" / exe("kiwix-serve"))
        self.assertEqual(cfg.chat_model, self.root / "models" / SMALL)
        self.assertEqual(cfg.reranker_model, self.root / "models" / RERANKER)
        self.assertEqual((cfg.library_dir, cfg.chat_gpu_layers), (self.root / "library", 0))
        self.assertTrue(profile.fitted)
        self.assertEqual(profile.llama_exe, cfg.llama_exe)

    def test_a_gpu_gets_its_build_and_the_best_model_the_drive_has(self):
        self.stock()
        cfg = Config()
        profile = portable.adapt(cfg, GPU, self.root)
        self.assertEqual(cfg.llama_exe.parent.name, "vulkan")
        # The Capable model is not on the drive; the small one is allowed.
        self.assertEqual(cfg.chat_model.name, SMALL)
        self.assertEqual(cfg.chat_gpu_layers, 999)
        self.assertFalse(profile.fitted)

    def test_a_write_protected_folder_falls_back_to_this_computer(self):
        d = self.drive()
        touch(self.root / "library")  # a file where the folder should be: mkdir fails
        got = d.writable_or_cache(self.root / "library" / "evals")
        self.assertEqual(got, d.cache_dir / "evals")
        self.assertTrue(got.is_dir())
        self.assertEqual(d.writable_or_cache(self.root / "fine"), self.root / "fine")


class AppStateOnADriveTest(Drive):
    def state(self) -> AppState:
        d = portable.detect(self.app, self.local)
        return AppState(d.config_path(), d.tuning_path(), drive=d)

    def test_first_launch_adapts_then_later_launches_reuse_the_profile(self):
        self.stock()
        d = self.drive()
        Config(library_dir=self.root / "library", models_dir=self.root / "models", base=self.root
               ).save(d.config_path())
        with mock.patch("museion.hardware.detect", return_value=CPU):
            state = self.state()
        self.assertEqual(state.config().chat_model, self.root / "models" / SMALL)
        self.assertTrue(d.profile_path().exists())
        self.assertFalse(state.fit_due())
        # The drive's own config is left as it was: what was chosen is this
        # computer's, and the next computer chooses for itself.
        self.assertIsNone(json.loads(d.config_path().read_text(encoding="utf-8"))["chat_model"])

        # The profile says the GPU split was never measured: a fit is due.
        p = MachineProfile.load(d.profile_path())
        p.fitted = False
        p.save(d.profile_path())
        with mock.patch("museion.hardware.detect", side_effect=AssertionError("not asked again")):
            again = self.state()
        self.assertEqual(again.config().chat_model, self.root / "models" / SMALL)
        self.assertTrue(again.fit_due())
        again.mark_fitted()
        self.assertFalse(again.fit_due())

    def test_an_upgrade_that_brings_another_llama_build_makes_a_fit_due(self):
        self.stock()
        d = self.drive()
        Config(library_dir=self.root / "library", models_dir=self.root / "models", base=self.root).save(d.config_path())
        package.write(self.root, package.Package("0.1.0", helpers={"llama.cpp/vulkan": "one", "kiwix-tools": "k"}))
        with mock.patch("museion.hardware.detect", return_value=GPU):
            state = self.state()
        state.mark_fitted()
        self.assertFalse(state.fit_due())
        # Another kiwix-serve changes nothing; another llama.cpp does.
        package.write(self.root, package.Package("0.2.0", helpers={"llama.cpp/vulkan": "one", "kiwix-tools": "k2"}))
        self.assertFalse(state.fit_due())
        package.write(self.root, package.Package("0.3.0", helpers={"llama.cpp/vulkan": "two", "kiwix-tools": "k2"}))
        self.assertTrue(state.fit_due())
        state.mark_fitted()
        self.assertFalse(state.fit_due())
        # A profile from before builds were noted: due once an upgrade has
        # replaced the helpers.
        p = MachineProfile.load(d.profile_path())
        p.llama_build = ""
        p.save(d.profile_path())
        self.assertFalse(state.fit_due())
        touch(self.root / "support" / "installed.json").write_text(json.dumps([{"to": "0.3.0", "parts": ["app", "bin"]}]))
        self.assertTrue(state.fit_due())

    def test_saving_writes_relative_paths_and_this_computers_choices_to_its_profile(self):
        self.stock()
        d = self.drive()
        Config(chat_model=self.root / "models" / SMALL, chat_args=["-c", "4096"], base=self.root
               ).save(d.config_path())
        MachineProfile(chat_model=self.root / "models" / SMALL, fitted=True).save(d.profile_path())
        state = self.state()
        cfg = state.config()
        cfg.library_dir = self.root / "library"
        big = touch(self.root / "models" / "big.gguf")
        cfg.chat_model, cfg.chat_args, cfg.chat_gpu_layers = big, ["-ot", "x=CPU"], 12
        cfg.llama_exe = self.root / "bin" / "llama" / "vulkan" / exe("llama-server")
        state.save_config(cfg)
        data = json.loads(d.config_path().read_text(encoding="utf-8"))
        self.assertEqual(data["library_dir"], "library")
        # The drive keeps its own model and build for the next computer: this
        # one's choices were once written there too.
        self.assertEqual((data["chat_model"], data["chat_args"], data["llama_exe"]), (f"models/{SMALL}", ["-c", "4096"], None))
        p = MachineProfile.load(d.profile_path())
        self.assertEqual((p.chat_model, p.chat_args, p.chat_gpu_layers), (big, ["-ot", "x=CPU"], 12))
        self.assertEqual(state.config().chat_model, big)
        # Another model has not been measured on this computer's GPU.
        self.assertFalse(p.fitted)
        self.assertTrue(state.fit_due())

    def test_saving_the_same_model_keeps_the_fit(self):
        self.stock()
        d = self.drive()
        MachineProfile(chat_model=self.root / "models" / SMALL, fitted=True).save(d.profile_path())
        state = self.state()
        cfg = state.config()
        cfg.autostart = False
        state.save_config(cfg)
        self.assertFalse(state.fit_due())

    def test_choosing_a_model_in_settings_has_it_measured_at_the_next_start(self):
        self.stock()
        d = self.drive()
        MachineProfile(chat_model=self.root / "models" / SMALL, chat_gpu_layers=999, fitted=True
                       ).save(d.profile_path())
        state = self.state()
        state._hardware = GPU
        big = touch(self.root / "models" / "big.gguf")
        said = commands.set_config(state, {**state.config().to_json(), "chat_model": str(big)})
        self.assertEqual(said, "Press Start services to load the new model.")
        self.assertTrue(state.fit_due())

    def test_a_drive_that_cannot_be_written_still_saves_the_profile(self):
        self.stock()
        d = self.drive()
        MachineProfile(fitted=True).save(d.profile_path())
        d.config_path().mkdir()  # writing config.json fails, as on a locked drive
        state = self.state()
        cfg = state.config()
        cfg.chat_model = self.root / "models" / SMALL
        with contextlib.redirect_stdout(io.StringIO()) as said:
            state.save_config(cfg)
        self.assertIn("drive config not written", said.getvalue())
        self.assertEqual(MachineProfile.load(d.profile_path()).chat_model, self.root / "models" / SMALL)
        self.assertEqual(state.config().chat_model, self.root / "models" / SMALL)


if __name__ == "__main__":
    unittest.main()
