"""fetch and verify --library from the command line, against a local web
server standing in for the mirrors; and the retrieval check's scoring."""

import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fake_http import FakeServer
from museion import catalog, catalog_cli, cli, retrieval_eval


def run(*argv: str) -> tuple[str, int]:
    """Run a command; what it printed, and its exit code."""
    out = io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(out):
        try:
            cli.main(list(argv))
        except SystemExit as e:
            code = e.code or 0
    return out.getvalue(), code


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.body = b"an archive " * 500
        self.server.files["/zim/x_en_2026-01.zim"] = self.body
        self.source = catalog.Source(
            id="x", title="X", category="survival", filename="x_en_2026-01.zim",
            size_bytes=len(self.body), sha256=hashlib.sha256(self.body).hexdigest(),
            urls=[f"{self.server.url}/gone.zim"], mirrors=[f"{self.server.url}/zim/x_en_2026-01.zim"],
            magnet="magnet:?xt=urn:btih:00&xl=5500&as=https://x")
        self._tmp = tempfile.TemporaryDirectory()
        self.library = Path(self._tmp.name)
        patcher = mock.patch.object(catalog_cli, "items", lambda id: ("Archive: X", [self.source]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.server.close()
        self._tmp.cleanup()

    def test_fetches_from_a_mirror_when_the_first_address_fails_and_skips_it_next_time(self):
        printed, code = run("fetch", "x", "--dest", str(self.library))
        self.assertEqual(code, 0, printed)
        self.assertEqual((self.library / "x_en_2026-01.zim").read_bytes(), self.body)
        self.assertIn("1 downloaded", printed)
        printed, code = run("fetch", "x", "--dest", str(self.library))
        self.assertIn("already here, checked", printed)
        self.assertIn("0 downloaded, 1 already here", printed)

    def test_when_every_mirror_fails_the_torrent_is_offered_for_the_persons_own_client(self):
        self.server.files.clear()
        printed, code = run("fetch", "x", "--dest", str(self.library))
        self.assertEqual(code, 1)
        self.assertIn("also on BitTorrent", printed)
        self.assertIn("magnet:  magnet:?xt=urn:btih:00&xl=5500\n", printed)
        self.assertIn(f"python -m museion verify --library {self.library} x", printed)

    def test_verify_finds_a_damaged_archive_and_says_how_to_fetch_it_again(self):
        target = self.library / "x_en_2026-01.zim"
        target.write_bytes(self.body)
        with mock.patch.object(catalog, "sources", lambda: [self.source]):
            printed, code = run("verify", "--library", str(self.library))
            self.assertEqual(code, 0, printed)
            self.assertIn("1 correct, 0 damaged", printed)
            target.write_bytes(self.body[:-1] + b"!")
            printed, code = run("verify", "--library", str(self.library))
        self.assertEqual(code, 2)
        self.assertIn("checksum differs", printed)
        self.assertIn(f"python -m museion fetch x --dest {self.library} --force", printed)


class CatalogueIdsTest(unittest.TestCase):
    def test_an_id_names_a_bundle_an_archive_or_a_model(self):
        what, items = catalog_cli.items("go-bag")
        self.assertTrue(what.startswith("Bundle:"))
        self.assertGreater(len(items), 1)
        self.assertEqual(catalog_cli.items("zimgit-water")[1][0].id, "zimgit-water")
        self.assertEqual(catalog_cli.items("qwen3-reranker-0.6b")[1][0].role, "reranker")
        with self.assertRaises(ValueError):
            catalog_cli.items("nothing-by-this-name")

    def test_the_list_shows_every_bundle_archive_and_model(self):
        printed, code = run("fetch", "--list")
        self.assertEqual(code, 0)
        for item in [*catalog.bundles(), *catalog.sources(), *catalog.models()]:
            self.assertIn(item.id, printed)


class RetrievalCheckTest(unittest.TestCase):
    def test_the_cases_parse_and_end_with_one_that_must_find_nothing(self):
        cases = retrieval_eval.cases()
        self.assertGreater(len(cases), 5)
        self.assertTrue(cases[-1].find_nothing)

    def test_scoring(self):
        water = retrieval_eval.Case("purify water", "why", ["boil", "distill"])
        self.assertEqual(retrieval_eval.score(water, " 1. Boiling water -- page 4"), (True, "matched 1/2: boil"))
        self.assertFalse(retrieval_eval.score(water, "1. Knots -- page 2")[0])
        self.assertEqual(retrieval_eval.score(water, "\nNothing in the library covers that.\n"),
                         (False, "found nothing"))
        history = retrieval_eval.Case("Bronze Age", "why", find_nothing=True)
        self.assertTrue(retrieval_eval.score(history, "Nothing in the library matches that.")[0])
        self.assertFalse(retrieval_eval.score(history, " 1. Pottery -- page 9")[0])

    def test_a_case_with_nothing_to_look_for_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "retrieval.toml")
            path.write_text('[[case]]\nquestion = "q"\nwhy = "w"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                retrieval_eval.cases(path)
            path.write_text('[[case]]\nquestion = "q"\nwhy = "w"\nexpect_al = ["x"]\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                retrieval_eval.cases(path)


if __name__ == "__main__":
    unittest.main()
