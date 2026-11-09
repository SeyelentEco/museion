"""Scoring the benchmark, and saving and reading runs."""

import tempfile
import unittest
from pathlib import Path

from museion import evals
from museion.ask import Answer, Calculation, LIBRARY
from museion.index import Hit


def case(**checks) -> evals.Case:
    return evals.Case(id="t", capability="reason", horizon="month", prompt="", **checks)


class BenchmarkFileTest(unittest.TestCase):
    def test_the_builtin_benchmark_parses_and_is_well_formed(self):
        cases = evals.builtin_cases()
        self.assertGreaterEqual(len(cases), 20)
        capabilities = {"retrieve_pdf", "retrieve_article", "calculate", "reason", "plan", "refuse"}
        horizons = {"month", "six_months", "year", "five_years", "any"}
        for c in cases:
            self.assertIn(c.capability, capabilities, c.id)
            self.assertIn(c.horizon, horizons, c.id)
        ids = [c.id for c in cases]
        self.assertEqual(len(set(ids)), len(ids), "duplicate case ids")
        self.assertTrue(any(isinstance(c.expect_number, evals.NumberCheck) for c in cases))

    def test_a_misspelt_check_is_an_error(self):
        text = '[[cases]]\nid = "x"\ncapability = "plan"\nhorizon = "any"\nprompt = "?"\nexpect_al = ["y"]\n'
        with self.assertRaisesRegex(ValueError, "case x: unknown field.*expect_al"):
            evals.load_cases(text)


class ScoreTest(unittest.TestCase):
    def test_numbers_come_from_the_calculator_first(self):
        c = case(expect_number=evals.NumberCheck(67.8, 1.0), must_not_contain=["343"])
        calcs = [Calculation("convert(55*0.25,tsp,ml)", "67.77")]
        good = evals.score(c, "add 67.77 ml", "library_and_calculation", [], calcs)
        self.assertTrue(all(r.passed for r in good), good)
        bad = evals.score(c, "add 343.75 ml", "library_and_calculation", [], calcs)
        self.assertTrue(any(r.check == "must_not_contain" and not r.passed for r in bad))

    def test_steps_are_counted_from_lists(self):
        c = case(min_steps=3)
        self.assertTrue(evals.score(c, "Do this:\n1. water\n2. shelter\n3. food\n", "library", [], [])[0].passed)
        self.assertFalse(evals.score(c, "Just one paragraph.", "library", [], [])[0].passed)

    def test_a_number_only_in_the_prose_is_not_a_calculation(self):
        c = case(expect_number=evals.NumberCheck(41.84, 0.1))
        self.assertFalse(evals.score(c, "26 miles is about 41.8 kilometres.", "general_knowledge", [], [])[0].passed)

    def test_a_source_page_number_is_not_a_wrong_figure_in_the_answer(self):
        c = case(must_not_contain=["343"])
        results = evals.score(c, "Add 67.8 ml [1].", "library", ["Water Treatment (p.343) [zimgit]"], [])
        self.assertTrue(results[0].passed, results)

    def test_basis_alternatives_and_sources(self):
        c = case(expect_basis="library | library_and_calculation", source_from="where there is no doctor", min_sources=1)
        results = evals.score(c, "Keep warm [1].", "library", ["Where There Is No Doctor (p.12) [zimgit]"], [])
        self.assertEqual([r.check for r in results], ["expect_basis", "min_sources", "source_from"])
        self.assertTrue(all(r.passed for r in results), results)
        self.assertFalse(evals.score(c, "Keep warm.", "general_knowledge", [], [])[0].passed)

    def test_a_case_with_no_checks_only_needs_an_answer(self):
        self.assertEqual([(r.check, r.passed) for r in evals.score(case(), "  ", "library", [], [])],
                         [("answered", False)])


class FakeAsker:
    def __init__(self, answers):
        self.answers = answers
        self.reranker = None
        self.chat = self

    def model_id(self):
        return "fake-model.gguf"

    def ask(self, prompt):
        answer = self.answers[prompt]
        if isinstance(answer, Exception):
            raise answer
        return answer


class RunTest(unittest.TestCase):
    def test_a_run_is_scored_tallied_saved_and_read_back(self):
        page = Hit("Where There Is No Doctor", "", "zimgit", "wtnd.pdf", 12, "", 0.0, 1.0, "")
        article = Hit("Hypothermia", "", "wikipedia", "A/Hypothermia", 0, "", 0.0, 1.0, "")
        cases = [
            evals.Case("warm", "retrieve_pdf", "month", "cold?", expect_basis="library"),
            evals.Case("bronze", "refuse", "any", "bronze age?", expect_basis="general_knowledge"),
        ]
        asker = FakeAsker({"cold?": Answer("Keep warm [1][2].", [page, article], True, LIBRARY,
                                           tools=["search_library", "read_page"], rounds=3),
                           "bronze age?": RuntimeError("no chat model")})
        seen = []
        run = evals.run(asker, cases, "test", lambda *progress: seen.append(progress))
        self.assertEqual(seen, [(1, 2, "warm", None), (1, 2, "warm", True), (2, 2, "bronze", None), (2, 2, "bronze", False)])
        self.assertEqual((run.passed, run.total, run.model), (1, 2, "fake-model.gguf"))
        self.assertEqual(run.cases[0].sources, ["Where There Is No Doctor (p.12) [zimgit]", "Hypothermia [wikipedia]"])
        self.assertEqual((run.cases[0].tools, run.cases[0].rounds), (["search_library", "read_page"], 3))
        self.assertEqual(run.cases[1].error, "no chat model")
        self.assertEqual(run.by_capability, [("retrieve_pdf", 1, 1), ("refuse", 0, 1)])

        with tempfile.TemporaryDirectory() as tmp:
            older = evals.Run(**{**run.__dict__, "id": "1000"})
            evals.save_run(Path(tmp), older)
            path = evals.save_run(Path(tmp), run)
            Path(tmp, "notes.json").write_text("not a run")
            self.assertEqual(path.name, f"run-{run.id}.json")
            runs = evals.list_runs(Path(tmp))
        self.assertEqual([r.id for r in runs], [run.id, "1000"])
        self.assertEqual(runs[0], run)


if __name__ == "__main__":
    unittest.main()
