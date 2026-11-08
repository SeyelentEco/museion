"""The ask loop, with a scripted model, index, reranker and kiwix standing in
for the real ones, so every branch can be driven without a model."""

import contextlib
import copy
import io
import json
import unittest

from museion import ask, llama
from museion.index import Hit
from museion.tuning import Tuning

TUNING = Tuning(
    system_prompt="Answer from the passages. do not improvise.",
    rewrite_prompt="Name the subjects.",
    answer_tokens=1500, max_rounds=6, max_continuations=2, max_history=4,
    history_chars=2500, keep=8, min_rerank_score=0.15,
)


def hit(title: str, page: int = 1, body: str = "", book: str = "zimgit", file: str = "") -> Hit:
    return Hit(doc_title=title, author="", book=book, file=file or f"{title}.pdf", page=page,
               snippet="", rank=0.0, coverage=1.0, body=body or f"All about {title.lower()}.")


class FakeIndex:
    def __init__(self, hits=(), coverage=1.0):
        self.hits = list(hits)
        self.coverage = coverage
        self.queries = []

    def search_ungated(self, query, limit):
        self.queries.append((query, limit))
        return list(self.hits[:limit]), self.coverage


class ScriptedChat:
    """Plays back one llama.Turn per call to turn(), and answers the rewrite
    request with `rewrite`."""

    def __init__(self, *turns, rewrite="water purification"):
        self.turns = list(turns)
        self.rewrite = rewrite
        self.calls = []  # (messages, tools) for each turn()

    def chat(self, messages, max_tokens=1024):
        if isinstance(self.rewrite, Exception):
            raise self.rewrite
        return self.rewrite

    def turn(self, messages, tools=None, max_tokens=1024, on_token=None):
        self.calls.append((copy.deepcopy(messages), tools))
        turn = self.turns.pop(0)
        if turn.text and on_token:
            on_token(turn.text)
        return turn


class FakeReranker:
    def __init__(self, score=lambda doc: 0.9, fail=False):
        self.score = score
        self.fail = fail
        self.queries = []

    def rerank(self, query, documents):
        self.queries.append(query)
        if self.fail:
            raise ConnectionError("reranker is down")
        scored = [(i, self.score(d)) for i, d in enumerate(documents)]
        return sorted(scored, key=lambda pair: pair[1], reverse=True)


class FakeSearchHit:
    def __init__(self, title, path):
        self.title = title
        self.snippet = ""
        self._path = path

    def path(self):
        return self._path


class FakeKiwix:
    def __init__(self, articles):
        self.articles = articles  # {path: text}
        self.read = []

    def search(self, book, pattern, limit):
        return [FakeSearchHit(path.title(), path) for path in self.articles][:limit]

    def article_text(self, book, path):
        self.read.append(path)
        return self.articles[path]


def says(text, limited=False):
    return llama.Turn(text=text, hit_token_limit=limited)


def calls(name, arguments, id="call_0"):
    return llama.Turn(tool_calls=[{"id": id, "type": "function",
                                   "function": {"name": name, "arguments": json.dumps(arguments)}}])


def asker(chat, index=None, reranker=None, **kwargs):
    index = index if index is not None else FakeIndex([hit("Boiling water"), hit("Bleach", page=4)])
    return ask.Asker(index, chat, reranker, tuning=copy.deepcopy(TUNING), **kwargs)


class ArithmeticTest(unittest.TestCase):
    def test_pure_arithmetic_never_reaches_the_model(self):
        chat = ScriptedChat()
        seen = []
        answer = asker(chat).ask("what is 10 x 10?", on_token=seen.append)
        self.assertEqual(answer.text, "10 * 10 = **100**")
        self.assertEqual(answer.basis, ask.COMPUTED)
        self.assertEqual([(c.expression, c.result) for c in answer.calculations], [("10 * 10", "100")])
        self.assertEqual(seen, [answer.text])
        self.assertEqual(chat.calls, [])


class AskTest(unittest.TestCase):
    def test_the_library_is_searched_first_and_replayed_as_a_tool_exchange(self):
        chat = ScriptedChat(says("Boil it for a minute [1]."))
        answer = asker(chat).ask("how do I purify water")
        messages, tools = chat.calls[0]
        self.assertEqual(messages[0]["content"], TUNING.system_prompt)
        request, result = messages[-2], messages[-1]
        self.assertEqual(request["tool_calls"][0]["function"]["arguments"],
                         '{"query":"water purification | how do I purify water"}')
        self.assertEqual(result["tool_call_id"], "seed_search")
        self.assertIn("[1] Boiling water (page 1)\nAll about boiling water.", result["content"])
        self.assertIn("[2] Bleach (page 4)", result["content"])
        self.assertEqual([t["function"]["name"] for t in tools], ["search_library", "calculate"])
        self.assertEqual(answer.basis, ask.LIBRARY)

    def test_only_cited_sources_are_kept_and_renumbered_in_citation_order(self):
        chat = ScriptedChat(says("Add bleach [2], or boil it [2][1]."))
        answer = asker(chat).ask("purify water")
        self.assertEqual(answer.text, "Add bleach [1], or boil it [1][2].")
        self.assertEqual([h.doc_title for h in answer.sources], ["Bleach", "Boiling water"])

    def test_a_number_that_names_no_passage_does_not_move_the_real_citations(self):
        chat = ScriptedChat(says("Boil water [9]. Add bleach [2]."))
        answer = asker(chat).ask("purify water")
        self.assertEqual(answer.text, "Boil water. Add bleach [1].")
        self.assertEqual([h.doc_title for h in answer.sources], ["Bleach"])

    def test_an_answer_citing_nothing_is_asked_once_to_cite_or_stand(self):
        chat = ScriptedChat(says("Boil it."), says("Boil it, from what I know."))
        answer = asker(chat).ask("purify water")
        messages, tools = chat.calls[1]
        self.assertEqual(messages[-1]["content"], ask.CITE_OR_STAND)
        self.assertIsNone(tools, "the repair turn must answer, not search again")
        self.assertEqual(answer.basis, ask.GENERAL_KNOWLEDGE)
        self.assertEqual(answer.sources, [])

    def test_a_tool_call_returned_as_text_is_asked_for_again_with_the_tools(self):
        leak = says('<tool_call>\n{"name": "calculate", "arguments": {"expression": "55*0.25"}}')
        chat = ScriptedChat(leak, calls("calculate", {"expression": "55*0.25"}), says("Use 13.75 tsp [2]."))
        answer = asker(chat).ask("bleach for 55 gallons")
        messages, tools = chat.calls[1]
        self.assertEqual(messages[-1]["content"], ask.CALL_AGAIN)
        self.assertIsNotNone(tools, "the calculator must still be on offer")
        self.assertEqual([(c.expression, c.result) for c in answer.calculations], [("55*0.25", "13.75")])

    def test_a_model_that_keeps_writing_tool_calls_as_text_gives_no_answer_not_the_call(self):
        leak = says("<tool_call>\n<function=search_library>")
        chat = ScriptedChat(leak, leak, leak)
        seen = []
        answer = asker(chat).ask("purify water", on_token=seen.append)
        self.assertEqual(chat.calls[2][0][-1]["content"], ask.NO_MORE_TOOLS)
        self.assertEqual(answer.text, ask.NO_ANSWER)
        self.assertEqual(seen[-2:], [ask.DISCARD, ask.NO_ANSWER])

    def test_narration_before_a_tool_call_is_discarded_and_the_calculator_is_ground_truth(self):
        chat = ScriptedChat(
            llama.Turn(text="That is 339 ml...", tool_calls=calls("calculate", {"expression": "convert(55*0.25, tsp, ml)"}).tool_calls),
            says("Add 67.77 ml [2]."),
        )
        seen = []
        answer = asker(chat).ask("how much bleach for 55 gallons", on_token=seen.append)
        self.assertEqual(seen, ["That is 339 ml...", ask.DISCARD, "Add 67.77 ml [2]."])
        result = chat.calls[1][0][-1]
        self.assertEqual(result["tool_call_id"], "call_0")
        self.assertEqual(result["content"], "convert(55*0.25, tsp, ml) = 67.77. Report this figure exactly; do not convert or rescale it.")
        self.assertEqual(answer.basis, ask.LIBRARY_AND_CALCULATION)
        self.assertEqual(answer.calculations[0].result, "67.77")

    def test_a_bad_expression_is_explained_to_the_model(self):
        chat = ScriptedChat(calls("calculate", {"expression": "two plus two"}), says("Four [1]."))
        asker(chat).ask("two plus two, per the book")
        self.assertIn("Could not evaluate `two plus two`", chat.calls[1][0][-1]["content"])

    def test_a_search_call_numbers_new_passages_after_the_first(self):
        index = FakeIndex([hit("Boiling water")])
        index.search_ungated = lambda query, limit: (
            [hit("Boiling water"), hit("Bleach", page=4)] if query == "bleach" else [hit("Boiling water")], 1.0)
        chat = ScriptedChat(calls("search_library", {"query": "bleach"}), says("Boil [1] or bleach [2]."))
        answer = asker(chat, index).ask("purify water")
        result = chat.calls[1][0][-1]["content"]
        # The page found again keeps its number, and is not quoted twice;
        # the new one gets the next.
        self.assertTrue(result.startswith("[1] Boiling water (page 1): shown above\n"))
        self.assertNotIn("All about boiling water", result)
        self.assertIn("[2] Bleach (page 4)", result)
        self.assertEqual([h.doc_title for h in answer.sources], ["Boiling water", "Bleach"])

    def test_no_more_searches_are_offered_once_another_would_not_fit(self):
        class SmallContext(ScriptedChat):
            def context_size(self):
                return 4096
        chat = SmallContext(says("Boil it [1]."))
        asker(chat).ask("purify water")
        messages, tools = chat.calls[0]
        self.assertIsNone(tools)
        self.assertEqual(messages[-1]["content"], ask.NO_MORE_TOOLS)
        roomy = type("Roomy", (ScriptedChat,), {"context_size": lambda self: 32768})(says("Boil it [1]."))
        asker(roomy).ask("purify water")
        self.assertIsNotNone(roomy.calls[0][1])

    def test_an_unknown_tool_is_named(self):
        chat = ScriptedChat(calls("delete_files", {}), says("Boil it [1]."))
        asker(chat).ask("purify water")
        self.assertEqual(chat.calls[1][0][-1]["content"], "Unknown tool delete_files.")

    def test_arithmetic_done_in_prose_is_sent_back_once(self):
        chat = ScriptedChat(says("4 people × 2 litres = 8 litres [1]."), says("4 people × 2 litres = 8 litres [1]."))
        seen = []
        answer = asker(chat).ask("water for four people", on_token=seen.append)
        self.assertEqual(chat.calls[1][0][-1]["content"], ask.CALCULATE_FIRST)
        self.assertIsNotNone(chat.calls[1][1], "the calculator must be on offer")
        self.assertIn(ask.DISCARD, seen)
        self.assertEqual(answer.basis, ask.LIBRARY)

    def test_an_answer_cut_off_is_continued_then_marked(self):
        chat = ScriptedChat(says("Step one", limited=True), says("step two", limited=True), says("three [1].", limited=True))
        answer = asker(chat).ask("purify water")
        self.assertEqual(chat.calls[1][0][-1]["content"], ask.CONTINUE)
        self.assertIsNone(chat.calls[1][1])
        said = [m["content"] for m in chat.calls[2][0] if m["role"] == "assistant"]
        self.assertEqual(said[-2:], ["Step one", "step two"], "each piece is replayed once")
        self.assertEqual(answer.text, "Step one step two three [1]." + ask.CUT_SHORT)

    def test_the_last_round_withholds_the_tools(self):
        searches = [calls("search_library", {"query": f"q{i}"}) for i in range(TUNING.max_rounds)]
        chat = ScriptedChat(*searches, says("Boil it [1]."))
        asker(chat).ask("purify water")
        messages, tools = chat.calls[-1]
        self.assertIsNone(tools)
        self.assertEqual(messages[-1]["content"], ask.NO_MORE_TOOLS)

    def test_history_is_the_last_few_exchanges_without_their_citations(self):
        history = [ask.Exchange(f"q{i}", f"answer {i} [1].") for i in range(6)]
        chat = ScriptedChat(says("It is [1]."))
        asker(chat).ask("and then?", history)
        messages = chat.calls[0][0]
        replayed = [m["content"] for m in messages[1:-3]]
        self.assertEqual(replayed, ["q2", "answer 2.", "q3", "answer 3.", "q4", "answer 4.", "q5", "answer 5."])

    def test_a_failed_rewrite_still_searches_the_question(self):
        index = FakeIndex([hit("Boiling water")])
        chat = ScriptedChat(says("Boil it [1]."), rewrite=ConnectionError("down"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            asker(chat, index).ask("purify water")
        self.assertIn("search rewrite unavailable: down", err.getvalue())
        self.assertEqual(index.queries, [("purify water", ask.RETRIEVE)])


class GatherTest(unittest.TestCase):
    def test_without_a_reranker_weak_coverage_finds_nothing(self):
        index = FakeIndex([hit("Ice ages")], coverage=0.3)
        self.assertEqual(asker(ScriptedChat(), index).gather("bronze age collapse"), ([], False))
        index.coverage = 0.5
        self.assertEqual(len(asker(ScriptedChat(), index).gather("bronze age collapse")[0]), 1)

    def test_the_reranker_decides_coverage(self):
        reranker = FakeReranker(score=lambda doc: 0.1)
        self.assertEqual(asker(ScriptedChat(), reranker=reranker).gather("bronze age"), ([], True))

    def test_passages_below_the_threshold_go_and_index_pages_come_last(self):
        index_page = "Index " + " ".join(f"water {n}, {n + 3}," for n in range(10, 60, 5))
        index = FakeIndex([hit("Contents", body=index_page), hit("Boiling water"), hit("Weather")])
        scores = {"Contents": 0.95, "Boiling water": 0.8, "Weather": 0.05}
        reranker = FakeReranker(score=lambda doc: scores[doc.split(".")[0]])
        hits, reranked = asker(ScriptedChat(), index, reranker).gather("boil water")
        self.assertTrue(reranked)
        self.assertEqual([h.doc_title for h in hits], ["Boiling water", "Contents"])
        self.assertEqual(reranker.queries[0], "boil water")

    def test_a_failed_reranker_falls_back_to_keywords(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            hits, reranked = asker(ScriptedChat(), reranker=FakeReranker(fail=True)).gather("boil water")
        self.assertIn("reranker unavailable", err.getvalue())
        self.assertFalse(reranked)
        self.assertEqual(len(hits), 2)

    def test_the_seeded_pool_is_ranked_against_the_question_and_its_subjects(self):
        reranker = FakeReranker()
        chat = ScriptedChat(says("Boil it [1]."), rewrite="1. water purification\n2. boiling")
        asker(chat, reranker=reranker).ask("how do I make water safe")
        self.assertEqual(reranker.queries[0], "how do I make water safe (water purification; boiling)")


class NativeArchiveTest(unittest.TestCase):
    ARTICLE = "Water purification is the process of removing undesirable chemicals. " * 5

    def test_articles_join_the_pool_and_each_keeps_its_own_number(self):
        kiwix = FakeKiwix({"A/Water_purification": self.ARTICLE, "A/Boiling": self.ARTICLE,
                           "A/Stub": "Too short to answer anything."})
        chat = ScriptedChat(says("Boil [1], filter [2]."), rewrite="water purification")
        answer = asker(chat, FakeIndex(), kiwix=kiwix, native_books=["wikipedia"]).ask("purify water")
        result = chat.calls[0][0][-1]["content"]
        self.assertIn("[1] A/Water_Purification (wikipedia)", result)
        self.assertIn("[2] A/Boiling (wikipedia)", result)
        self.assertEqual([h.file for h in answer.sources], ["A/Water_purification", "A/Boiling"])
        # Found by both the question and its rewrite, each article is read once.
        self.assertEqual(sorted(kiwix.read), ["A/Boiling", "A/Stub", "A/Water_purification"])


class HelperTest(unittest.TestCase):
    def test_truncation_counts_characters_not_bytes(self):
        s = "évacuation immédiate"
        self.assertEqual(len(ask.truncate_chars(s, 5)), 5)
        self.assertEqual(ask.truncate_chars(s, 500), s)

    def test_tools_are_exactly_the_two_intended(self):
        self.assertEqual([t["function"]["name"] for t in ask.TOOLS], ["search_library", "calculate"])

    def test_tool_arguments_written_as_bare_text_are_used_whole(self):
        self.assertEqual(ask.argument('{"query": "bleach"}', "query"), "bleach")
        self.assertEqual(ask.argument("bleach dose", "query"), "bleach dose")
        self.assertEqual(ask.argument('{"query": 5}', "query"), '{"query": 5}')

    def test_focus_centres_on_the_most_specific_term(self):
        text = "Opening words. " * 100 + "Pasteurization kills germs."
        window = ask.focus(text, "what is pasteurization for", 200)
        # The window opens a quarter of its width before the term.
        self.assertTrue(window.endswith("Opening words. Pasteurization kills germs."))
        self.assertEqual(len(window), 50 + len("Pasteurization kills germs."))
        self.assertTrue(ask.focus(text, "nothing here matches", 50).startswith("Opening words."))


if __name__ == "__main__":
    unittest.main()
