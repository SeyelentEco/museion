"""The real llama-server with the tiny built-in model: start it, ask it, stop
it. Runs when MUSEION_LLAMA_SERVER names the llama-server program."""

import os
import struct
import tempfile
import unittest
from pathlib import Path

from museion import llama, sidecar, tiny_gguf

LLAMA_SERVER = os.environ.get("MUSEION_LLAMA_SERVER")


class TinyModelFileTest(unittest.TestCase):
    def test_writes_a_gguf_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = tiny_gguf.write(Path(tmp) / "t.gguf").read_bytes()
        magic, version, tensors, keys = struct.unpack("<4sIQQ", data[:24])
        self.assertEqual((magic, version, tensors), (b"GGUF", 3, 12))
        self.assertGreater(keys, 10)


@unittest.skipUnless(LLAMA_SERVER, "set MUSEION_LLAMA_SERVER to the llama-server program")
class LlamaServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        model = tiny_gguf.write(Path(cls.tmp.name) / "museion-tiny-test.gguf")
        cls.sup = sidecar.Supervisor()
        cls.port = sidecar.free_port()
        cls.sup.start_llama(sidecar.CHAT, Path(LLAMA_SERVER), model, cls.port,
                            extra=["--ctx-size", "512", "--parallel", "1"])
        cls.sup.wait_ready(sidecar.CHAT, f"http://127.0.0.1:{cls.port}/health", timeout=120)
        cls.client = llama.Llama(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.sup.stop_all()
        cls.tmp.cleanup()

    def test_reports_the_model(self):
        self.assertIn("museion-tiny-test", self.client.model_id())

    def test_reports_its_context(self):
        self.assertEqual(self.client.context_size(), 512)

    def test_streams_an_answer_until_the_token_limit(self):
        seen = []
        turn = self.client.turn([llama.system("Be brief."), llama.user("Say hello.")],
                                max_tokens=8, on_token=seen.append)
        self.assertTrue(turn.text)
        self.assertEqual("".join(seen), turn.text)
        self.assertTrue(turn.hit_token_limit)

    def test_adopts_the_running_server_for_its_model(self):
        other = sidecar.Supervisor()
        url = f"http://127.0.0.1:{self.port}/v1/models"
        self.assertTrue(other.adopt(sidecar.CHAT, self.port, url, Path("museion-tiny-test.gguf")))
        self.assertFalse(sidecar.Supervisor().adopt(sidecar.CHAT, self.port, url, Path("qwen3.gguf")))


if __name__ == "__main__":
    unittest.main()
