"""Folding a conversation to fit, and the order guards are tried in."""

import json
import unittest

from museion import guards
from museion.compaction import compact, prompt_chars


def call(i, name, args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def result(i, text):
    return {"role": "tool", "content": text, "tool_call_id": f"c{i}"}


class CompactTest(unittest.TestCase):
    def conversation(self):
        return [
            {"role": "system", "content": "S" * 100},
            {"role": "user", "content": "goal"},
            call(1, "search_library", {"query": "water"}), result(1, "[1] A\n" + "a" * 5000 + "\n[2] B\nb"),
            call(2, "calculate", {"expression": "1+1"}), result(2, "1+1 = 2" + " " * 3000),
            call(3, "search_library", {"query": "food"}), result(3, "[3] C\n" + "c" * 5000),
        ]

    def test_oldest_search_goes_first_and_keeps_its_numbers(self):
        m = self.conversation()
        removed = compact(m, prompt_chars(m) - 1000, keep_rounds=1)
        self.assertEqual(len(removed), 1)
        self.assertIn("passages 1-2", m[3]["content"])
        self.assertIn("1+1 = 2", m[5]["content"])  # calculator kept
        self.assertTrue(m[7]["content"].startswith("[3] C"))  # last round kept
        self.assertEqual(m[1]["content"], "goal")

    def test_never_the_calculator_or_the_last_rounds(self):
        m = self.conversation()
        compact(m, 0, keep_rounds=1)
        self.assertIn("1+1 = 2", m[5]["content"])
        self.assertTrue(m[7]["content"].startswith("[3] C"))

    def test_folding_twice_changes_nothing_more(self):
        m = self.conversation()
        compact(m, 0, keep_rounds=1)
        self.assertEqual(compact(m, 0, keep_rounds=1), [])

    def test_nothing_when_it_fits(self):
        m = self.conversation()
        self.assertEqual(compact(m, 10**6, keep_rounds=1), [])


class GuardOrderTest(unittest.TestCase):
    class Run:
        round, max_rounds, sources, calculations = 1, 6, ["a passage"], []

    def test_sum_in_prose_is_fixed_before_citations(self):
        name, push = guards.check(["cite_or_stand", "calculate_first"], "4 × 2 = 8", self.Run(), set())
        self.assertEqual(name, "calculate_first")
        self.assertFalse(push.withhold_tools)

    def test_each_fires_once(self):
        name, _ = guards.check(["cite_or_stand", "calculate_first"], "4 × 2 = 8", self.Run(), {"calculate_first"})
        self.assertEqual(name, "cite_or_stand")
        self.assertIsNone(guards.check(["calculate_first"], "4 × 2 = 8", self.Run(), {"calculate_first"}))


if __name__ == "__main__":
    unittest.main()
