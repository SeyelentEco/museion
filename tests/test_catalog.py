"""The catalogue as shipped: it parses, bundles resolve, and the choices the
tests pin down come out the same. Plus what a person editing the
files by hand needs: a mistake names the file and the field."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from museion import catalog, hardware


def llama_cpp() -> catalog.Tool:
    return next(t for t in catalog.tools() if t.id == "llama-cpp")


class ShippedCatalogueTest(unittest.TestCase):
    def test_embedded_catalogue_parses_and_bundles_resolve(self):
        self.assertTrue(catalog.sources())
        b, srcs = catalog.bundle("go-bag")
        self.assertEqual(len(b.sources), len(srcs))
        self.assertTrue(all(len(s.sha256) == 64 for s in srcs))

    def test_the_preferred_model_wins_a_tier_over_directory_order(self):
        m = catalog.chat_model_for("Capable", 64_000)
        self.assertEqual(m.id, "qwen3.6-35b-a3b-q4", "gemma sorts first but qwen is preferred")

    def test_a_tier_short_of_ram_falls_back_rather_than_failing(self):
        # Capable wants 24 GB of system RAM for expert offload.
        self.assertIn("Modest", catalog.chat_model_for("Capable", 8_000).tiers)
        self.assertIn("Capable", catalog.chat_model_for("Capable", 64_000).tiers)

    def test_cuda_machine_gets_the_cuda_llama_build_with_its_companion(self):
        # Naming the platform runs this on any system.
        key, v = catalog.tool_variant(llama_cpp(), "cuda", platform="windows", arch="x64")
        self.assertEqual(key, "windows-cuda")
        self.assertEqual(v.also, ["windows-cudart"])

    def test_every_checksum_is_a_sha256(self):
        hexdigits = set("0123456789abcdef")
        entries = [*catalog.sources(), *catalog.models()]
        entries += [v for t in catalog.tools() for v in t.variants.values()]
        for e in entries:
            self.assertTrue(len(e.sha256) == 64 and set(e.sha256) <= hexdigits, e)
            self.assertGreater(e.size_bytes, 0, e)

    def test_every_bundle_resolves(self):
        for b in catalog.bundles():
            self.assertEqual([s.id for s in catalog.bundle(b.id)[1]], b.sources)

    def test_works_from_any_folder(self):
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.assertTrue(catalog.models())
            finally:
                os.chdir(here)

    def test_the_reranker(self):
        self.assertEqual(catalog.reranker_model().role, "reranker")

    def test_a_big_machine_gets_the_ample_model_and_none_is_left_out(self):
        self.assertIn("Ample", catalog.chat_model_for("Ample", 256_000).tiers)
        # Below Modest there is nothing to fall back to, but Modest needs no RAM.
        self.assertIn("Modest", catalog.chat_model_for("Modest", 0).tiers)

    def test_sources_offer_mirrors_after_the_primary_and_a_short_magnet(self):
        s = catalog.bundle("go-bag")[1][0]
        self.assertEqual(s.all_urls(), s.urls + s.mirrors)
        self.assertTrue(s.magnet.startswith(s.short_magnet()))
        self.assertNotIn("&as=", s.short_magnet())


class ChoosingTest(unittest.TestCase):
    def test_tiers_and_accelerators_come_from_the_hardware_check(self):
        self.assertEqual(catalog.chat_model_for(hardware.CAPABLE, 64_000).id, "qwen3.6-35b-a3b-q4")
        accel = hardware.Accelerator(hardware.CUDA, "RTX 4070", 12_000)
        key, _ = catalog.tool_variant(llama_cpp(), accel, platform="windows")
        self.assertEqual(key, "windows-cuda")
        with self.assertRaisesRegex(ValueError, "unknown tier"):
            catalog.chat_model_for("Huge", 1)
        with self.assertRaisesRegex(ValueError, "unknown accelerator"):
            catalog.tool_variant(llama_cpp(), "tpu")

    def test_a_missing_build_falls_back_to_cpu_and_a_companion_is_never_chosen_alone(self):
        tool = llama_cpp()
        # No Metal build for Windows: the CPU one, not the internal cudart.
        self.assertEqual(catalog.tool_variant_named(tool, "metal", platform="windows")[0], "windows-cpu")
        self.assertEqual(catalog.tool_variant_named(tool, "metal", platform="darwin", arch="arm64")[0], "macos-arm64")
        self.assertIsNone(catalog.tool_variant_named(tool, "vulkan", platform="darwin", arch="arm64"))

    def test_a_build_for_any_accelerator_comes_before_the_cpu_one(self):
        v = lambda accel: catalog.ToolVariant("f", "u", 1, "0" * 64, accelerator=accel)
        tool = catalog.Tool("t", "t", [], {"a-cpu": v("cpu"), "b-any": v(None)})
        self.assertEqual(catalog.tool_variant_named(tool, "cuda")[0], "b-any")

    def test_variants_are_in_name_order(self):
        names = list(llama_cpp().variants)
        self.assertEqual(names, sorted(names))

    def test_unknown_bundles_and_sources_are_named(self):
        with self.assertRaisesRegex(ValueError, "no bundle named 'nope'"):
            catalog.bundle("nope")
        with mock.patch.object(catalog, "sources", return_value=[]):
            with self.assertRaisesRegex(ValueError, "bundle go-bag references unknown source 'zimgit-post-disaster'"):
                catalog.bundle("go-bag")


class HandEditedFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.object(catalog, "MODELS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, text: str) -> None:
        Path(self.dir, name).write_text(text, encoding="utf-8")

    GOOD = ('id = "m"\ntitle = "M"\nrole = "chat"\nfilename = "m.gguf"\nsha256 = "%s"\n'
            'urls = ["https://example.org/m.gguf"]\n' % ("0" * 64))

    def test_unknown_keys_are_ignored_and_defaults_filled(self):
        self.write("m.toml", self.GOOD + 'size_bytes = 5\nquant = "Q4"\n[requires]\nsystem_ram_mb = 1\n')
        [m] = catalog.models()
        self.assertEqual((m.size_bytes, m.tiers, m.preferred, m.requires.system_ram_mb), (5, [], False, 1))

    def test_a_wrong_type_names_the_file_and_the_field(self):
        self.write("m.toml", self.GOOD + 'size_bytes = "big"\n')
        with self.assertRaisesRegex(ValueError, "m.toml: size_bytes should be an int"):
            catalog.models()

    def test_a_missing_field_names_the_file_and_the_field(self):
        self.write("m.toml", self.GOOD)
        with self.assertRaisesRegex(ValueError, "m.toml: missing size_bytes"):
            catalog.models()

    def test_a_syntax_error_names_the_file(self):
        self.write("m.toml", "id = \n")
        with self.assertRaisesRegex(ValueError, "parsing .*m.toml"):
            catalog.models()

    def test_files_are_read_in_name_order_and_only_toml(self):
        self.write("b.toml", self.GOOD.replace('"m"', '"b"') + "size_bytes = 1\n")
        self.write("a.toml", self.GOOD.replace('"m"', '"a"') + "size_bytes = 1\n")
        self.write("c.yaml", "id: c\n")
        self.assertEqual([m.id for m in catalog.models()], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
