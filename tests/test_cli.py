"""The command line, end to end over a small index, with the fake llama-server
answering as the chat model."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from fake_llama import FakeLlama, text
from museion import cli, sidecar
from museion.index import Index
from museion.pdf import Extracted, Page

# Each page needs twenty words or more, or it reads as a scan with no text.
PAGES = {
    "Boiling water": "Bring the water to a rolling boil for one minute to make it safe to drink. "
                     "At high altitude, above two thousand metres, boil it for three minutes instead.",
    "Storing water": "Store four litres of drinking water per person per day in clean sealed containers, "
                     "away from sunlight, and replace the stored water every six months.",
}


def run(*argv) -> tuple[int, str, str]:
    """Run the command line; returns the exit status, stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    status = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main([str(a) for a in argv])
        except SystemExit as e:
            status = e.code
    return status, out.getvalue(), err.getvalue()


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name, "museion-index.db")
        index = Index(self.db)
        for title, body in PAGES.items():
            index.add_document("zimgit-water", f"{title}.pdf", title, "", "Peace Corps",
                               Extracted([Page(1, body)], 1, len(body.split())))
        index.close()
        self.fake = FakeLlama()
        # Nothing listens here, so kiwix-serve reads as not running.
        self.nowhere = f"http://127.0.0.1:{sidecar.free_port()}"

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def test_status(self):
        status, out, _ = run("status", "--db", self.db)
        self.assertEqual(status, 0)
        self.assertIn("2 documents, 2 pages", out)
        self.assertIn("zimgit-water", out)

    def test_search(self):
        status, out, _ = run("search", "--library", self.tmp.name, "boil", "water")
        self.assertEqual(status, 0)
        self.assertIn(" 1. Boiling water  -- Peace Corps", out)
        self.assertIn("page 1 of zimgit-water", out)

    def test_gather_without_a_reranker(self):
        status, out, _ = run("gather", "--db", self.db, "--no-rerank", "--kiwix", self.nowhere, "boil water")
        self.assertEqual(status, 0)
        self.assertIn("keyword ranking only", out)
        self.assertIn("Boiling water -- page 1", out)

    def test_ask_streams_the_answer_and_lists_what_it_cited(self):
        self.fake.chunks = [text("Boil it for one minute [1].")]
        status, out, _ = run("ask", "--db", self.db, "--llama", self.fake.url, "--no-rerank",
                             "--kiwix", self.nowhere, "how", "do", "I", "make", "water", "safe")
        self.assertEqual(status, 0, out)
        self.assertIn("Boil it for one minute [1].", out)
        self.assertIn("Basis: from the library", out)
        self.assertIn("[1] Boiling water, Peace Corps -- page 1", out)

    def test_arithmetic_needs_no_model(self):
        status, out, _ = run("ask", "--db", self.db, "--llama", self.nowhere, "--no-rerank",
                             "--kiwix", self.nowhere, "17*23")
        self.assertEqual(status, 0)
        self.assertIn("17*23 = **391**", out)
        self.assertIn("Basis: computed", out)

    def test_no_chat_model_is_one_clear_line(self):
        status, _, err = run("ask", "--db", self.db, "--llama", self.nowhere, "--no-rerank",
                             "--kiwix", self.nowhere, "boil water")
        self.assertEqual(status, 1)
        self.assertIn("error: no chat model at", err)

    def test_an_empty_index_says_to_build_it(self):
        status, _, err = run("ask", "--db", Path(self.tmp.name, "empty.db"), "boil water")
        self.assertEqual(status, 1)
        self.assertIn("the index is empty", err)

    def test_eval_runs_the_chosen_cases_and_saves_the_run(self):
        self.fake.chunks = [text("Boil it for one minute [1].")]
        self.fake.rerank_scores = [0.9, 0.2]
        status, out, _ = run("eval", "--db", self.db, "--llama", self.fake.url, "--reranker", self.fake.url,
                             "--kiwix", self.nowhere, "--only", "calculate", "--label", "test")
        self.assertEqual(status, 0, out)
        self.assertIn("with reranker", out)
        saved = list(Path(self.tmp.name, "evals").glob("run-*.json"))
        self.assertEqual(len(saved), 1)
        run_data = json.loads(saved[0].read_text(encoding="utf-8"))
        self.assertEqual(run_data["label"], "test")
        self.assertTrue(run_data["reranker"])
        self.assertEqual({c["capability"] for c in run_data["cases"]}, {"calculate"})

    def test_a_db_is_needed(self):
        status, _, err = run("status")
        self.assertEqual(status, 1)
        self.assertIn("need --db", err)


if __name__ == "__main__":
    unittest.main()
