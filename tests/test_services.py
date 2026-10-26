"""Starting the chat model, and choosing another one, from Settings.

Found by choosing the 35B in Settings on a real PC: it kept the small
model's arguments, Start services kept the small model loaded, and nothing
said a fit was due. The supervisor is replaced by one that records what it
is asked, so no helper starts.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from museion import catalog, commands, hardware, services, sidecar
from museion.appstate import AppState
from museion.config import Config

GPU = hardware.Hardware(hardware.Accelerator(hardware.CUDA, "RTX 3060", 12288), 32000, hardware.CAPABLE)
CPU = hardware.Hardware(hardware.Accelerator(hardware.CPU), 16000, hardware.MODEST)


class FakeSupervisor:
    def __init__(self) -> None:
        self.args: list[str] | None = None
        self.calls: list = []

    def started_with(self, role):
        return self.args

    def stop(self, role):
        self.calls.append("stop")
        self.args = None

    def adopt(self, role, port, health_url, expect_model=None):
        return self.args is not None

    def start_llama(self, role, exe, model, port, gpu_layers=0, extra=None):
        self.calls.append(("start", model.name, gpu_layers))
        self.args = sidecar.llama_args(exe, model, port, gpu_layers, extra or [])

    def wait_ready(self, role, health_url, timeout):
        pass


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.small = next(m for m in catalog.models() if m.tiers == [hardware.MODEST])
        self.large = next(m for m in catalog.models() if m.role == "chat" and hardware.CAPABLE in m.tiers)
        for m in (self.small, self.large):
            (self.dir / m.filename).write_bytes(b"")
        # The small model, fitted: a split measured for it alone.
        Config(llama_exe=self.dir / "llama-server", chat_model=self.dir / self.small.filename,
               chat_args=["--ctx-size", "16384", "-ot", "blk\\.(2[0-9])\\.ffn_.*=CPU"], chat_gpu_layers=37
               ).save(self.dir / "config.json")
        self.state = AppState(self.dir / "config.json", self.dir / "tuning.toml")
        self.state._hardware = GPU
        self.sup = FakeSupervisor()
        self.state.supervisor = self.sup

    def tearDown(self):
        self.tmp.cleanup()

    def choose(self, model: Path):
        return commands.set_config(self.state, {**self.state.config().to_json(), "chat_model": str(model)})

    def start(self):
        cfg = self.state.config()
        with mock.patch("museion.services.time.sleep"):
            services.start_chat(self.state, cfg, cfg.chat_model, cfg.chat_args, cfg.chat_gpu_layers)


class ChooseModelTest(Settings):
    def test_another_model_gets_its_own_arguments_not_the_last_ones_split(self):
        said = self.choose(self.dir / self.large.filename)
        cfg = self.state.config()
        self.assertEqual(cfg.chat_args, self.large.server_args)
        self.assertEqual(cfg.chat_gpu_layers, 999)
        self.assertIn("Fit model", said)
        saved = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["chat_args"], self.large.server_args)

    def test_saving_the_same_model_keeps_its_fit(self):
        self.assertIsNone(self.choose(self.dir / self.small.filename))
        cfg = self.state.config()
        self.assertEqual((cfg.chat_args[2], cfg.chat_gpu_layers), ("-ot", 37))

    def test_a_model_of_ones_own_keeps_what_was_added_but_not_the_split(self):
        mine = self.dir / "my-own.gguf"
        mine.write_bytes(b"")
        self.choose(mine)
        self.assertEqual(self.state.config().chat_args, ["--ctx-size", "16384"])

    def test_without_a_gpu_there_is_nothing_to_fit(self):
        self.state._hardware = CPU
        self.assertEqual(self.choose(self.dir / self.large.filename), "Press Start services to load the new model.")
        self.assertEqual(self.state.config().chat_gpu_layers, 0)


class StartChatTest(Settings):
    def test_a_chat_model_running_as_configured_is_kept(self):
        self.start()
        self.start()
        self.assertEqual(self.sup.calls, [("start", self.small.filename, 37)])

    def test_a_chat_model_started_before_the_settings_changed_is_replaced(self):
        self.start()
        self.choose(self.dir / self.large.filename)
        self.start()
        self.assertEqual(self.sup.calls, [("start", self.small.filename, 37), "stop",
                                          ("start", self.large.filename, 999)])


if __name__ == "__main__":
    unittest.main()
