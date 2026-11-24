"""Helper programs the person brings, where the catalogue has none to fetch."""

import os
import tempfile
import unittest
from pathlib import Path

from museion import catalog, hardware, own_programs, pdf
from museion.appstate import UiError, open_state
from museion.hardware import Accelerator, Hardware

CPU_BOX = Hardware(Accelerator(hardware.CPU), 16000, hardware.MODEST)


class On:
    """An app state in a temporary folder, pretending to be another system."""

    def __init__(self, platform: str, arch: str = "x64"):
        self.platform, self.arch = platform, arch

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.real = catalog.current_platform, catalog.current_arch
        catalog.current_platform = lambda: self.platform
        catalog.current_arch = lambda: self.arch
        state = open_state(Path(self.dir.name))
        state._hardware = CPU_BOX
        return state, Path(self.dir.name)

    def __exit__(self, *exc):
        catalog.current_platform, catalog.current_arch = self.real
        self.dir.cleanup()


def program(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class Missing(unittest.TestCase):
    def test_windows_and_apple_silicon_need_nothing_of_their_own(self):
        mac = Hardware(Accelerator(hardware.METAL, "Apple M3", 36864), 36864, hardware.CAPABLE)
        for platform, arch, hw in [("windows", "x64", CPU_BOX), ("darwin", "arm64", mac)]:
            with On(platform, arch) as (state, _):
                state._hardware = hw
                self.assertEqual(own_programs.missing(state), [], platform)

    def test_linux_is_asked_for_all_three_until_the_catalogue_has_them(self):
        with On("linux") as (state, _):
            ids = [p["id"] for p in own_programs.missing(state)]
        # If Linux builds join tools/*.toml this list shrinks, and so does the wizard's step.
        expected = [p["id"] for p in own_programs.PROGRAMS
                    if not any(p["id"] in t.provides and any(v.platform == "linux" for v in t.variants.values())
                               for t in catalog.tools())]
        self.assertEqual(ids, expected)

    def test_an_intel_mac_is_asked_only_for_llama(self):
        with On("darwin", "x64") as (state, _):
            self.assertEqual([p["id"] for p in own_programs.missing(state)], ["llama-server"])

    def test_a_path_already_in_settings_is_offered(self):
        with On("darwin", "x64") as (state, folder):
            mine = program(folder, "llama-server")
            cfg = state.config()
            cfg.llama_exe = mine
            state.save_config(cfg)
            self.assertEqual(own_programs.missing(state)[0]["path"], str(mine))


class Use(unittest.TestCase):
    def test_every_problem_is_said_at_once(self):
        with On("linux") as (state, folder):
            if not own_programs.missing(state):
                self.skipTest("the catalogue has Linux builds of everything")
            with self.assertRaises(UiError) as caught:
                own_programs.use_own_programs(state, {"llama-server": str(folder / "nope"), "kiwix-serve": ""})
            said = str(caught.exception)
        self.assertIn("there is no file", said)
        self.assertIn("kiwix-serve: not given", said)

    @unittest.skipIf(os.name == "nt", "the execute bit is not a Windows idea")
    def test_a_file_that_cannot_be_run_is_refused(self):
        with On("darwin", "x64") as (state, folder):
            (folder / "llama-server").write_text("not a program")
            with self.assertRaises(UiError) as caught:
                own_programs.use_own_programs(state, {"llama-server": str(folder / "llama-server")})
        self.assertIn("may run", str(caught.exception))

    def test_good_paths_are_saved_and_a_missing_fit_tool_is_mentioned(self):
        with On("darwin", "x64") as (state, folder):
            mine = program(folder, "llama-server")
            said = own_programs.use_own_programs(state, {"llama-server": str(mine)})
            self.assertEqual(state.config().llama_exe, mine)
            self.assertIn("llama-fit-params", said)
            program(folder, "llama-fit-params")
            self.assertEqual(own_programs.use_own_programs(state, {"llama-server": str(mine)}), "Saved.")

    def test_pdfium_is_a_folder_with_the_library_in_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            entry = next(p for p in own_programs.PROGRAMS if p["id"] == "pdfium")
            self.assertIn(pdf.library_filename(), own_programs.problem(entry, str(folder)))
            (folder / "lib").mkdir()
            (folder / "lib" / pdf.library_filename()).write_bytes(b"")
            self.assertIsNone(own_programs.problem(entry, str(folder)))


if __name__ == "__main__":
    unittest.main()
