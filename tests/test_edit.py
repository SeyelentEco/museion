"""`python -m museion edit`: opencode on Museion's own code, talking to the
chat model on this computer and to nothing on the network.

opencode itself is replaced by this Python, run with a script that writes
down what it was given, so the tests need neither opencode nor a model.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from museion import catalog, edit, sidecar
from museion.appstate import AppState, UiError
from museion.config import Config
from museion.portable import APP_DIR

# Stands in for opencode: records its arguments, folder and environment.
RECORD = """import json, os, sys
json.dump({"args": sys.argv[2:], "cwd": os.getcwd(), "env": dict(os.environ)}, open(sys.argv[1], "w"))
sys.exit(3)
"""


class EditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        model = self.dir / "small.gguf"
        model.write_bytes(b"")
        # A port nothing answers on, so no chat model is found running.
        Config(chat_model=model, llama_exe=self.dir / "llama-server", chat_port=sidecar.free_port(),
               chat_args=["--ctx-size", "16384", "--temp", "0.7"]).save(self.dir / "config.json")
        self.state = AppState(self.dir / "config.json", self.dir / "tuning.toml")
        self.started: list[list[str]] = []

    def tearDown(self):
        self.tmp.cleanup()

    def start_chat(self, state, cfg, model, extra, gpu_layers):
        self.started.append(extra)

    def run_edit(self, args):
        with mock.patch("museion.edit.find_opencode", return_value=Path(sys.executable)), \
             mock.patch("museion.services.start_chat", self.start_chat), \
             mock.patch("builtins.print"):
            return edit.run(self.state, args)

    def test_the_context_is_raised_to_what_opencode_needs_never_lowered(self):
        self.assertEqual(edit.with_context(["--ctx-size", "16384", "--jinja"], 32768),
                         ["--jinja", "--ctx-size", "32768"])
        self.assertEqual(edit.with_context(["-c", "65536"], 32768), ["--ctx-size", "65536"])
        self.assertEqual(edit.with_context([], 32768), ["--ctx-size", "32768"])

    def test_opencode_is_given_one_model_the_one_on_this_computer(self):
        c = edit.opencode_config("http://127.0.0.1:8080", "small.gguf", 32768)
        provider = c["provider"]["museion"]
        self.assertEqual(provider["options"]["baseURL"], "http://127.0.0.1:8080/v1")
        self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(provider["models"]["local"]["limit"]["context"], 32768)
        self.assertEqual((c["model"], c["small_model"]), ("museion/local", "museion/local"))
        self.assertFalse(c["autoupdate"])
        # Sub-agents overflow the context; the main agent does the work.
        self.assertTrue(c["agent"]["explore"]["disable"])
        self.assertTrue(c["agent"]["general"]["disable"])

    def test_opencode_stays_off_the_network_and_off_the_drive(self):
        env = edit.opencode_env(self.state, "http://127.0.0.1:8080", "small.gguf", 32768, self.dir / "bin")
        for name in ("OPENCODE_PURE", "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH",
                     "OPENCODE_DISABLE_LSP_DOWNLOAD"):
            self.assertEqual(env[name], "1", name)
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["model"], "museion/local")
        self.assertEqual(Path(env["XDG_DATA_HOME"]), self.dir / "opencode" / "data")
        # The computer's own opencode settings are not read.
        self.assertEqual(Path(env["XDG_CONFIG_HOME"]), self.dir / "opencode" / "config")
        # `python` in its shell is the one running Museion, and rg (which it
        # would otherwise download) is found beside opencode.
        self.assertEqual(env["PATH"].split(os.pathsep)[:2], [str(Path(sys.executable).parent), str(self.dir / "bin")])

    def test_the_small_model_is_said_to_be_unfit_for_changing_code(self):
        small = next(m for m in catalog.models() if m.tiers == ["Modest"])
        self.assertIn("small model", edit.small_model_note(Path(small.filename)))
        large = next(m for m in catalog.models() if "Capable" in m.tiers)
        self.assertIsNone(edit.small_model_note(Path(large.filename)))
        self.assertIsNone(edit.small_model_note(Path("my-own.gguf")))

    def test_opencode_is_found_beside_the_helper_programs(self):
        name = "opencode.exe" if os.name == "nt" else "opencode"
        (self.dir / name).write_bytes(b"")
        cfg = self.state.config()
        cfg.kiwix_exe = self.dir / "kiwix-serve"
        self.state.save_config(cfg)
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(edit.find_opencode(self.state), self.dir / name)

    def test_it_starts_the_model_with_room_and_runs_opencode_on_museion(self):
        out = self.dir / "seen.json"
        code = self.run_edit(["-c", RECORD, str(out), "--continue"])
        self.assertEqual(code, 3)
        self.assertEqual(self.started, [["--temp", "0.7", "--ctx-size", str(edit.CONTEXT)]])
        seen = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(seen["args"], ["--continue"])
        self.assertEqual(Path(seen["cwd"]).resolve(), APP_DIR)
        self.assertEqual(seen["env"]["OPENCODE_PURE"], "1")
        config = json.loads(seen["env"]["OPENCODE_CONFIG_CONTENT"])
        port = self.state.config().chat_port
        self.assertEqual(config["provider"]["museion"]["options"]["baseURL"], f"http://127.0.0.1:{port}/v1")
        self.assertEqual(config["provider"]["museion"]["models"]["local"]["name"], "small.gguf")

    def test_a_model_already_running_with_too_little_room_is_not_used(self):
        with mock.patch("museion.edit.Llama") as llama, mock.patch("museion.edit.chat_context", return_value=16384):
            llama.return_value.is_up.return_value = True
            with self.assertRaisesRegex(UiError, "16384 tokens"):
                self.run_edit(["-c", "pass"])
        self.assertEqual(self.started, [])

    def test_without_opencode_or_a_model_it_says_what_is_missing(self):
        with mock.patch("museion.edit.find_opencode", return_value=None), \
             self.assertRaisesRegex(UiError, "opencode is not"):
            edit.run(self.state, [])
        cfg = self.state.config()
        cfg.chat_model = self.dir / "gone.gguf"
        self.state.save_config(cfg)
        with self.assertRaisesRegex(UiError, "No chat model"):
            self.run_edit([])


if __name__ == "__main__":
    unittest.main()
