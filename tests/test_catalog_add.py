import hashlib
import tempfile
import tomllib
import unittest
from pathlib import Path

from museion import catalog, catalog_add


class AddSourceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.sources = self.dir / "sources"
        self.sources.mkdir()
        self.zim = self.dir / "wikipedia_en_chemistry_nopic_2026-07.zim"
        self.zim.write_bytes(b"ZIM" * 1000)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_id_is_the_name_without_its_date(self):
        self.assertEqual(catalog_add.source_id("wikipedia_en_chemistry_nopic_2026-07.zim"),
                         "wikipedia_en_chemistry_nopic")
        self.assertEqual(catalog_add.source_id("zimgit-knots_en_2024-08.zim"), "zimgit-knots_en")
        self.assertEqual(catalog_add.source_id("found.zim"), "found")
        self.assertEqual(catalog_add.source_id("nhs.uk_en_medicines_2026-09b.zim"), "nhs.uk_en_medicines")

    def test_the_entry_is_measured_from_the_file_and_loads_as_a_source(self):
        path = catalog_add.add_source(self.zim, 'Chemistry by "Wikipedia"', "reference",
                                      "Chemistry articles", self.sources)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn(b"\r", path.read_bytes())  # LF on Windows too, as the other entries have it
        source = catalog._build(catalog.Source, data)
        self.assertEqual(source.id, "wikipedia_en_chemistry_nopic")
        self.assertEqual(source.title, 'Chemistry by "Wikipedia"')
        self.assertEqual(source.size_bytes, 3000)
        self.assertEqual(source.sha256, hashlib.sha256(b"ZIM" * 1000).hexdigest())
        # Nothing that cannot be known offline: no links to download it from.
        self.assertEqual(source.urls, [])
        self.assertEqual(source.mirrors, [])
        self.assertIsNone(source.magnet)

    def test_an_existing_source_is_never_overwritten(self):
        (self.sources / "wikipedia_en_chemistry_nopic.toml").write_text("kept", encoding="utf-8")
        with self.assertRaises(ValueError):
            catalog_add.add_source(self.zim, "Chemistry", "reference", sources_dir=self.sources)
        self.assertEqual((self.sources / "wikipedia_en_chemistry_nopic.toml").read_text(encoding="utf-8"), "kept")

    def test_a_shelf_the_library_has_no_heading_for_is_refused(self):
        with self.assertRaises(ValueError):
            catalog_add.add_source(self.zim, "Chemistry", "science", sources_dir=self.sources)
        with self.assertRaises(ValueError):
            catalog_add.add_source(self.zim, " ", "reference", sources_dir=self.sources)
        self.assertEqual(list(self.sources.iterdir()), [])

    def test_the_categories_are_the_library_tabs_headings(self):
        js = (catalog.ROOT / "museion" / "ui" / "library.js").read_text(encoding="utf-8")
        for c in catalog_add.CATEGORIES:
            self.assertIn(f"  {c}: '", js)


class TomlValueTest(unittest.TestCase):
    def test_keys_with_dots_are_quoted_and_read_back_whole(self):
        import tomllib
        from museion.catalog_add import toml_lines
        text = toml_lines({"given_files": {"stores.csv": "flour,5"}, "x": float("inf"), "y": -float("inf")})
        data = tomllib.loads(text)
        self.assertEqual(data["given_files"], {"stores.csv": "flour,5"})
        self.assertEqual((data["x"], data["y"]), (float("inf"), -float("inf")))


if __name__ == "__main__":
    unittest.main()
