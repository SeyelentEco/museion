"""config.json: read as the first version wrote it, and forgiving of hand edits."""

import json
import tempfile
import unittest
from pathlib import Path

from museion import config
from museion.config import Config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_what_the_first_version_wrote(self):
        # Its file for a configured machine, abridged.
        self.path.write_text(json.dumps({
            "library_dir": "E:\\museion-library",
            "extra_dirs": ["D:\\found"],
            "kiwix_exe": "E:\\bin\\kiwix-serve.exe",
            "llama_exe": None,
            "pdfium_dir": None,
            "models_dir": "E:\\models",
            "chat_model": "E:\\models\\qwen.gguf",
            "reranker_model": None,
            "chat_args": ["--ctx-size", "32768", "-ot", "blk\\.(1[5-9]|[2-3][0-9]|40)\\.ffn_.*_exps=CPU"],
            "chat_gpu_layers": 41,
            "kiwix_port": 8090,
            "chat_port": 8080,
            "reranker_port": 8081,
            "autostart": False,
        }), encoding="utf-8")
        c = config.load(self.path)
        self.assertEqual(c.library_dir, Path("E:\\museion-library"))
        self.assertEqual(c.extra_dirs, [Path("D:\\found")])
        self.assertIsNone(c.llama_exe)
        self.assertEqual(c.chat_gpu_layers, 41)
        self.assertEqual(c.chat_args[3], "blk\\.(1[5-9]|[2-3][0-9]|40)\\.ffn_.*_exps=CPU")
        self.assertFalse(c.autostart)
        self.assertEqual(c.index_path(), Path("E:\\museion-library") / "museion-index.db")

    def test_writes_it_back_the_same_way(self):
        c = Config(library_dir=Path("lib"), extra_dirs=[Path("found")], chat_args=["-c", "4096"])
        c.save(self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["library_dir"], "lib")
        self.assertEqual(data["extra_dirs"], ["found"])
        self.assertIsNone(data["chat_model"])
        self.assertEqual(config.load(self.path), c)

    def test_a_byte_order_mark_from_notepad_is_not_a_broken_file(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"library_dir": "lib"}).encode())
        self.assertEqual(config.load(self.path).library_dir, Path("lib"))

    def test_missing_ports_and_unknown_fields_are_tolerated(self):
        self.path.write_text(json.dumps({"kiwix_port": 0, "something_newer": True}), encoding="utf-8")
        c = config.load(self.path)
        self.assertEqual((c.kiwix_port, c.chat_port, c.reranker_port), (8090, 8080, 8081))
        self.assertTrue(c.autostart)

    def test_no_file_or_a_broken_one_means_the_defaults(self):
        self.assertEqual(config.load(self.path), Config())
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(config.load(self.path), Config())
        self.assertFalse(Config().is_ready())

    def test_a_blank_path_from_the_page_is_no_path(self):
        self.assertIsNone(Config.from_json({"chat_model": "  "}).chat_model)

    def test_content_folders_are_the_library_then_the_extras(self):
        c = Config(library_dir=Path("lib"), extra_dirs=[Path("a"), Path("b")])
        self.assertEqual(c.content_dirs(), [Path("lib"), Path("a"), Path("b")])
        self.assertEqual(Config(extra_dirs=[Path("a")]).content_dirs(), [Path("a")])


if __name__ == "__main__":
    unittest.main()
