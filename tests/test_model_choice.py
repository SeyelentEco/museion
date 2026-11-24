"""The model chooser: what fits which computer, and what it is told."""

import tempfile
import unittest
from pathlib import Path

from museion import catalog, hardware, model_choice
from museion.appstate import open_state
from museion.hardware import Accelerator, Hardware

RTX3060 = Hardware(Accelerator(hardware.CUDA, "NVIDIA GeForce RTX 3060", 12288), 65276, hardware.CAPABLE)
LAPTOP = Hardware(Accelerator(hardware.CPU), 16000, hardware.MODEST)
SMALL_RAM = Hardware(Accelerator(hardware.CUDA, "RTX 3060", 12288), 16000, hardware.MODEST)
BIG = Hardware(Accelerator(hardware.CUDA, "A big card", 49000), 131072, hardware.AMPLE)
MAC = Hardware(Accelerator(hardware.METAL, "Apple M3 Max", 36864), 36864, hardware.CAPABLE)


def model(id: str) -> catalog.Model:
    return next(m for m in catalog.models() if m.id == id)


class StateWith:
    """A real app state in a temporary folder, on a pretended computer."""

    def __init__(self, hw: Hardware):
        self.dir = tempfile.TemporaryDirectory()
        self.state = open_state(Path(self.dir.name))
        self.state._hardware = hw

    def __enter__(self):
        return self.state

    def __exit__(self, *exc):
        self.dir.cleanup()


class Fit(unittest.TestCase):
    def test_small_model_fits_everything(self):
        for hw in (RTX3060, LAPTOP, BIG, MAC):
            self.assertIsNone(model_choice.fit(model("qwen3-4b-instruct-q4"), hw))

    def test_moe_fits_a_12gb_card_with_ram(self):
        self.assertIsNone(model_choice.fit(model("qwen3.6-35b-a3b-q4"), RTX3060))
        self.assertIsNone(model_choice.fit(model("gemma-4-26b-a4b-q4"), RTX3060))

    def test_q4_fits_a_bigger_computer_than_its_tiers_name(self):
        self.assertIsNone(model_choice.fit(model("qwen3.6-35b-a3b-q4"), BIG))

    def test_too_little_system_memory_is_said_in_gb(self):
        why = model_choice.fit(model("qwen3.6-35b-a3b-q4"), SMALL_RAM)
        self.assertIn("24 GB of system memory", why)
        self.assertIn("has 16 GB", why)

    def test_q8_is_too_big_for_a_12gb_card(self):
        why = model_choice.fit(model("qwen3.6-35b-a3b-q8"), RTX3060)
        self.assertIn("32 GB", why)

    def test_every_chat_model_has_words_for_the_chooser(self):
        for m in catalog.models():
            if m.role == "chat":
                self.assertTrue(m.summary and m.good_for and m.limits, m.id)


class AgentTier(unittest.TestCase):
    def test_small_model_on_a_big_computer_is_still_small(self):
        small = Path("models") / model("qwen3-4b-instruct-q4").filename
        self.assertEqual(model_choice.agent_tier(hardware.CAPABLE, small), hardware.MODEST)
        self.assertEqual(model_choice.agent_tier(hardware.AMPLE, small), hardware.MODEST)

    def test_big_model_keeps_the_computers_tier(self):
        q4 = Path(model("qwen3.6-35b-a3b-q4").filename)
        self.assertEqual(model_choice.agent_tier(hardware.CAPABLE, q4), hardware.CAPABLE)
        self.assertEqual(model_choice.agent_tier(hardware.MODEST, q4), hardware.MODEST)

    def test_unknown_or_no_model_keeps_the_computers_tier(self):
        self.assertEqual(model_choice.agent_tier(hardware.CAPABLE, Path("my-own.gguf")), hardware.CAPABLE)
        self.assertEqual(model_choice.agent_tier(hardware.CAPABLE, None), hardware.CAPABLE)


class Choices(unittest.TestCase):
    def test_a_3060_is_offered_the_moe_and_told_what_the_small_one_cannot_do(self):
        with StateWith(RTX3060) as state:
            got = model_choice.model_choices(state, state.config_path.parent.as_posix())
        by_id = {m["id"]: m for m in got["models"]}
        self.assertEqual(got["recommended"], "qwen3.6-35b-a3b-q4")
        self.assertEqual(got["spare"], "qwen3-4b-instruct-q4")
        self.assertTrue(by_id["qwen3.6-35b-a3b-q4"]["recommended"])
        self.assertTrue(by_id["gemma-4-26b-a4b-q4"]["fits"])
        self.assertFalse(by_id["qwen3.6-35b-a3b-q8"]["fits"])
        small = by_id["qwen3-4b-instruct-q4"]
        self.assertTrue(small["fits"])
        self.assertIn("Librarian", small["helpers_run"])
        self.assertIn("Stores keeper", small["helpers_not"])
        self.assertEqual(by_id["qwen3.6-35b-a3b-q4"]["helpers_not"], [])
        self.assertIn("RTX 3060", got["machine"][0])
        self.assertIsNotNone(got["free_bytes"])

    def test_a_laptop_is_offered_the_small_model(self):
        with StateWith(LAPTOP) as state:
            got = model_choice.model_choices(state)
        self.assertEqual(got["recommended"], "qwen3-4b-instruct-q4")
        self.assertEqual([m["id"] for m in got["models"] if m["fits"]], ["qwen3-4b-instruct-q4"])
        self.assertIn("processor", got["machine"][0])

    def test_a_model_already_in_the_folder_is_seen_by_its_size(self):
        with StateWith(LAPTOP) as state:
            folder = state.config_path.parent
            small = model("qwen3-4b-instruct-q4")
            with open(folder / small.filename, "wb") as f:
                f.truncate(small.size_bytes)  # sparse: the size without the bytes
            got = model_choice.model_choices(state, str(folder))
        self.assertTrue(next(m for m in got["models"] if m["id"] == small.id)["on_disk"])

    def test_no_room_is_said(self):
        with StateWith(RTX3060) as state:
            real = model_choice.disk.free_space
            model_choice.disk.free_space = lambda path: 5 * 1024**3
            try:
                got = model_choice.model_choices(state, "/somewhere")
            finally:
                model_choice.disk.free_space = real
        by_id = {m["id"]: m for m in got["models"]}
        self.assertTrue(by_id["qwen3.6-35b-a3b-q4"]["no_room"])
        self.assertFalse(by_id["qwen3-4b-instruct-q4"]["no_room"])


class UseModel(unittest.TestCase):
    def test_switching_to_a_model_on_disk_sets_it_and_its_arguments(self):
        with StateWith(LAPTOP) as state:
            folder = state.config_path.parent / "models"
            folder.mkdir()
            small = model("qwen3-4b-instruct-q4")
            with open(folder / small.filename, "wb") as f:
                f.truncate(small.size_bytes)
            cfg = state.config()
            cfg.models_dir = folder
            cfg.chat_args = ["-ngl", "41", "-ot", "blk"]
            state.save_config(cfg)
            said = model_choice.use_model(state, small.id)
            cfg = state.config()
        self.assertEqual(cfg.chat_model.name, small.filename)
        self.assertEqual(cfg.chat_args, small.server_args)
        self.assertEqual(cfg.chat_gpu_layers, 0)  # no graphics card
        self.assertIn("Press Start services", said)

    def test_no_models_folder_is_a_sentence(self):
        from museion.appstate import UiError

        with StateWith(LAPTOP) as state:
            with self.assertRaises(UiError):
                model_choice.use_model(state, "qwen3-4b-instruct-q4")


if __name__ == "__main__":
    unittest.main()
