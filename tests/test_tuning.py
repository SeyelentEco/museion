"""The prompts and limits, and the override file a person edits."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from museion import tuning


class TuningTest(unittest.TestCase):
    def test_the_built_in_defaults_parse_and_keep_their_safety_clauses(self):
        t = tuning.defaults()
        prompt = t.system_prompt.replace("\n", " ")
        self.assertIn("do not improvise", prompt)
        self.assertIn("give no doses, amounts or schedules from general knowledge", prompt)
        self.assertIn("Never do arithmetic or unit conversion yourself", prompt)
        self.assertIn("EXACTLY as calculate returned it", prompt)
        self.assertIn("The library has nothing on this, so from general knowledge:", prompt)
        self.assertIn("Cite each with its bracketed number", prompt)
        self.assertEqual(t.keep, 8)
        self.assertGreaterEqual(t.answer_tokens, 1000)
        self.assertIsInstance(t.min_rerank_score, float)

    def test_an_override_changes_only_what_it_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "tuning.toml")
            path.write_text('keep = 3\nmin_rerank_score = 1\nsystem_prompt = """\nBe terse. do not improvise.\n"""\n')
            t = tuning.load(path)
        self.assertEqual(t.keep, 3)
        self.assertEqual(t.min_rerank_score, 1.0)
        self.assertEqual(t.system_prompt.strip(), "Be terse. do not improvise.")
        self.assertEqual(t.max_rounds, tuning.defaults().max_rounds)

    def test_a_broken_override_falls_back_to_the_defaults(self):
        for broken in ["keep = [not a number\n", 'keep = "eight"\n', "keep = -1\n", "system_prompt = 3\n"]:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "tuning.toml")
                path.write_text(broken)
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    t = tuning.load(path)
            self.assertEqual(t, tuning.defaults(), broken)
            self.assertIn("ignoring", err.getvalue())

    def test_no_override_file_means_the_defaults(self):
        self.assertEqual(tuning.load(Path("no-such-file.toml")), tuning.defaults())
        self.assertEqual(tuning.load(None), tuning.defaults())


if __name__ == "__main__":
    unittest.main()
