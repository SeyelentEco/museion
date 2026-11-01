"""Catalogue entries written from what Kiwix publishes beside an archive, with
a stand-in for the network: no test here reaches the internet."""

import io
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from museion import catalog, catalog_sync

SOURCES = catalog.CATALOG_DIR / "sources"


class FakeKiwix:
    """Answers fetch(url, method) from a table, and fails for anything else,
    as a 404 would."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.asked: list[tuple[str, str]] = []

    def __call__(self, url: str, method: str = "GET") -> str:
        self.asked.append((url, method))
        if url not in self.pages:
            raise OSError(f"404 for {url}")
        return self.pages[url]


def entry_xml(title: str, summary: str, tags: str) -> str:
    return f"<feed><entry><title>{title}</title><summary>{summary}</summary><tags>{tags}</tags></entry></feed>"


def kiwix_for(source: dict) -> FakeKiwix:
    """What download.kiwix.org and its catalogue said about a source when its
    entry was written."""
    url = source["urls"][0]
    stem, _ = catalog_sync.parse_zim_name(source["filename"])
    return FakeKiwix({
        f"{url}.sha256": f"{source['sha256']}  {source['filename']}\n",
        f"{url}.magnet": source["magnet"] + "\n",
        f"{url}.torrent": "",
        f"{catalog_sync.KIWIX_CATALOGUE}?name={stem}&count=5": entry_xml(
            source["title"], source["description"],
            "_ftindex:yes" if source.get("claims_fulltext_index") else "_ftindex:no;_category:other"),
    })


class SyncSourceTest(unittest.TestCase):
    def test_kiwix_names_split_into_a_stable_part_and_a_date(self):
        self.assertEqual(catalog_sync.parse_zim_name("zimgit-post-disaster_en_2024-05.zim"),
                         ("zimgit-post-disaster_en", "2024-05"))
        self.assertEqual(catalog_sync.parse_zim_name("wikipedia_en_chemistry_nopic_2026-07.zim"),
                         ("wikipedia_en_chemistry_nopic", "2026-07"))
        self.assertEqual(catalog_sync.parse_zim_name("found.zim"), ("found", None))

    def test_mirrors_come_from_the_magnet_with_the_doubled_scheme_repaired(self):
        url = "https://download.kiwix.org/zim/other/x_en_2024-08.zim"
        magnet = ("magnet:?xt=urn:btih:00&ws=https://https://mirrors.dotsrc.org/kiwix/zim/other/x_en_2024-08.zim"
                  "&ws=https://lb.download.kiwix.org/zim/other/x_en_2024-08.zim&ws=not-a-url&ws=" + url)
        self.assertEqual(catalog_sync.mirrors_for(url, magnet), [
            "https://lb.download.kiwix.org/zim/other/x_en_2024-08.zim",
            "https://www.mirrorservice.org/sites/download.kiwix.org/zim/other/x_en_2024-08.zim",
            "https://mirrors.dotsrc.org/kiwix/zim/other/x_en_2024-08.zim",
        ])

    def test_every_entry_in_the_catalogue_comes_out_the_same_when_written_again(self):
        # The entries were written by the script this replaced; written again
        # from what Kiwix said then, each must hold exactly the same data.
        for path in sorted(SOURCES.glob("*.toml")):
            text = path.read_text(encoding="utf-8")
            source = tomllib.loads(text)
            if not source["urls"]:
                continue
            recipe = next(line for line in text.splitlines() if line.startswith(catalog_sync.RECIPE))
            title = recipe.split(" --title ")[1].strip('"') if " --title " in recipe else None
            with self.subTest(path.name):
                id, again = catalog_sync.source_entry(
                    source["urls"][0], source["category"], source["id"], title,
                    source["license"]["name"], source["license"]["attribution"],
                    get=kiwix_for(source), say=lambda s: None)
                self.assertEqual(id, source["id"])
                self.assertEqual(tomllib.loads(again), source)
                self.assertEqual(again, text)

    def test_the_size_comes_from_the_server_when_there_is_no_magnet(self):
        url = "https://download.kiwix.org/zim/other/new-thing_en_2026-01.zim"
        get = FakeKiwix({f"{url}.sha256": "ab" * 32 + "  new-thing_en_2026-01.zim", url: "4096"})
        with tempfile.TemporaryDirectory() as tmp:
            path = catalog_sync.sync_source(url, "making", sources_dir=Path(tmp), get=get, say=lambda s: None)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(b"\r", path.read_bytes())  # LF on Windows too, as the repository has it
        data = tomllib.loads(text)
        self.assertEqual(path.name, "new-thing_en.toml")
        self.assertEqual(data["size_bytes"], 4096)
        self.assertEqual(data["title"], "new-thing_en")  # nothing better is known
        self.assertNotIn("magnet", data)
        self.assertNotIn("torrent", data)
        self.assertIn((url, "HEAD"), get.asked)
        self.assertIn("python -m museion sync-source " + url + " --category making --id new-thing_en\n", text)
        catalog._build(catalog.Source, data)

    def test_a_sha256_file_without_a_sha256_is_refused(self):
        url = "https://download.kiwix.org/zim/other/x_en_2024-08.zim"
        with self.assertRaises(ValueError):
            catalog_sync.probe(url, FakeKiwix({f"{url}.sha256": "<html>Not found</html>"}))

    def test_a_shelf_the_library_has_no_heading_for_is_refused(self):
        with self.assertRaises(ValueError):
            catalog_sync.source_entry("https://example.org/x.zim", "science", get=FakeKiwix({}))


class CheckSourcesTest(unittest.TestCase):
    def test_a_newer_release_upstream_is_reported_with_the_command_to_take_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            water = (SOURCES / "zimgit-water.toml").read_text(encoding="utf-8")
            (folder / "zimgit-water.toml").write_text(water, encoding="utf-8")
            knots = (SOURCES / "zimgit-knots.toml").read_text(encoding="utf-8")
            (folder / "zimgit-knots.toml").write_text(knots, encoding="utf-8")
            (folder / "found.toml").write_text(
                'id = "found"\ntitle = "Found"\ncategory = "reference"\nfilename = "found.zim"\n'
                'size_bytes = 1\nsha256 = "' + "0" * 64 + '"\nurls = []\n', encoding="utf-8")
            listing = ('<a href="zimgit-water_en_2024-08.zim">'
                       '<a href="zimgit-water_en_2025-03.zim"><a href="zimgit-water_en_2025-03.zim.torrent">'
                       '<a href="zimgit-knots_en_2024-08.zim">')
            out = io.StringIO()
            with redirect_stdout(out):
                attention = catalog_sync.check(folder, FakeKiwix({"https://download.kiwix.org/zim/other/": listing}))
        printed = out.getvalue()
        self.assertEqual(attention, 1)
        self.assertIn("STALE    zimgit-water  have 2024-08, upstream has 2025-03", printed)
        self.assertIn("python -m museion sync-source https://download.kiwix.org/zim/other/zimgit-water_en_2025-03.zim"
                      " --category survival --id zimgit-water", printed)
        self.assertIn("current  zimgit-knots  2024-08", printed)
        self.assertNotIn("found", printed)  # added offline: nowhere to look


if __name__ == "__main__":
    unittest.main()
