"""Reading what the model wrote: citations, suggested searches, arithmetic in
prose and tool calls written as text."""

import unittest

from museion import model_text


class ModelTextTest(unittest.TestCase):
    def test_citations_are_read_and_renumbered_in_order_of_use(self):
        text = "Boil it [3]. Or add bleach [1][5], see also [3, 1]."
        self.assertEqual(model_text.cited_numbers(text), [3, 1, 5])
        self.assertEqual(model_text.renumber_citations(text, [3, 1, 5]),
                         "Boil it [1]. Or add bleach [2][3], see also [1, 2].")
        # Prose brackets that are not citations are left alone.
        self.assertEqual(model_text.cited_numbers("see [note] and [2024]"), [2024])

    def test_rewrite_replies_become_short_searches(self):
        q = "I'm not near a medical center how can I help deliver this baby?"
        reply = ("Searches:\n1. \"delivering a baby\"\n- stages of labor\n* stages of labor\n\n"
                 "This is a long sentence that explains itself at great length and is not a search at all\n"
                 "3) cutting the umbilical cord\n4) placenta delivery\n")
        self.assertEqual(model_text.parse_queries(reply, q),
                         ["delivering a baby", "stages of labor", "cutting the umbilical cord"])
        self.assertEqual(model_text.parse_queries(q, q), [])

    def test_history_answers_lose_their_citation_numbers(self):
        self.assertEqual(model_text.strip_citations("Boil it [3]. Add bleach [1][5], see [3, 1]."),
                         "Boil it. Add bleach, see.")
        self.assertEqual(model_text.strip_citations("see [note] and [2024]"), "see [note] and ")

    def test_arithmetic_worked_out_in_prose_is_noticed(self):
        self.assertTrue(model_text.has_prose_arithmetic("Daily use: 4 people × 2 litres per person = 8 litres per day."))
        self.assertTrue(model_text.has_prose_arithmetic("20 litres ÷ 8 litres per day = 2.5 days"))
        self.assertTrue(model_text.has_prose_arithmetic("so 55*0.25=13.75 teaspoons"))
        self.assertTrue(model_text.has_prose_arithmetic("1,000 litres / 4 = 250 litres"))
        self.assertTrue(model_text.has_prose_arithmetic("55 gallons × ¼ tsp = 339 ml"))
        self.assertTrue(model_text.has_prose_arithmetic("1½ cups × 4 = 6 cups"))
        self.assertFalse(model_text.has_prose_arithmetic("Use ¼ teaspoon per gallon [1]."))
        self.assertFalse(model_text.has_prose_arithmetic("Wait 5-10 minutes, then check the pH = 7 reading [1]."))
        self.assertFalse(model_text.has_prose_arithmetic("You need 2 litres per person per day [2]."))
        self.assertFalse(model_text.has_prose_arithmetic("Between 1 and 2 = a range, not a sum"))

    def test_a_tool_call_returned_as_text_is_recognised(self):
        self.assertTrue(model_text.looks_like_leaked_tool_call(
            "<tool_call>\n<function=search_library>\n<parameter=query>\nfood safety\n</parameter>\n</function>\n</tool_call>"))
        self.assertTrue(model_text.looks_like_leaked_tool_call("  <function=calculate>"))
        self.assertFalse(model_text.looks_like_leaked_tool_call("Boil the water for one minute [1]."))


if __name__ == "__main__":
    unittest.main()
