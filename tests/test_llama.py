"""The llama-server client, against a fake server that sends what the real
one sends. No model needed."""

import unittest

from fake_llama import FakeLlama, call_fragment, text
from museion import llama


class TurnTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLlama()
        self.client = llama.Llama(self.fake.url)

    def tearDown(self):
        self.fake.close()

    def test_streamed_text_is_joined_and_passed_on_as_it_arrives(self):
        self.fake.chunks = [text("Boil "), text("for one "), text("minute.", finish="stop")]
        seen = []
        turn = self.client.turn([llama.user("water?")], on_token=seen.append)
        self.assertEqual(turn.text, "Boil for one minute.")
        self.assertEqual(seen, ["Boil ", "for one ", "minute."])
        self.assertFalse(turn.hit_token_limit)

    def test_tool_call_fragments_are_stitched_together(self):
        self.fake.chunks = [
            call_fragment(0, id="abc", name="calculate"),
            call_fragment(0, arguments='{"expression": '),
            call_fragment(0, arguments='"55 * 0.25"}'),
            call_fragment(1, name="search_library", arguments='{"query": "bleach"}'),
        ]
        turn = self.client.turn([llama.user("dose?")], tools=[llama.tool("calculate", "", {})])
        self.assertEqual(len(turn.tool_calls), 2)
        self.assertEqual(turn.tool_calls[0]["id"], "abc")
        self.assertEqual(turn.tool_calls[0]["function"], {"name": "calculate", "arguments": '{"expression": "55 * 0.25"}'})
        # A call the server gave no id gets one, so its result can be matched to it.
        self.assertEqual(turn.tool_calls[1]["id"], "call_1")

    def test_a_call_never_named_is_dropped(self):
        self.fake.chunks = [text("Here."), call_fragment(0, arguments="{}")]
        self.assertEqual(self.client.turn([llama.user("x")]).tool_calls, [])

    def test_hitting_the_token_limit_is_reported(self):
        self.fake.chunks = [text("Step one", finish="length")]
        self.assertTrue(self.client.turn([llama.user("x")]).hit_token_limit)

    def test_an_empty_answer_is_an_error_not_a_blank(self):
        self.fake.chunks = [text("", finish="length")]
        with self.assertRaisesRegex(RuntimeError, "returned nothing"):
            self.client.turn([llama.user("x")])

    def test_tools_make_it_greedy_and_thinking_is_off(self):
        self.fake.chunks = [text("ok")]
        self.client.turn([llama.user("x")], tools=[llama.tool("calculate", "", {})])
        self.client.turn([llama.user("x")])
        with_tools, without = self.fake.requests
        self.assertEqual(with_tools["temperature"], 0.0)
        self.assertEqual(without["temperature"], 0.2)
        self.assertNotIn("tools", without)
        self.assertEqual(with_tools["chat_template_kwargs"], {"enable_thinking": False})

    def test_model_id(self):
        self.assertEqual(self.client.model_id(), "fake-model.gguf")
        self.assertTrue(self.client.is_up())

    def test_nothing_listening_is_not_up(self):
        self.assertFalse(llama.Llama("http://127.0.0.1:9").is_up())


class RerankTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLlama()
        self.client = llama.Llama(self.fake.url)

    def tearDown(self):
        self.fake.close()

    def test_results_come_back_best_first(self):
        self.fake.rerank_scores = [0.1, 0.9, 0.5]
        self.assertEqual(self.client.rerank("q", ["a", "b", "c"]), [(1, 0.9), (2, 0.5), (0, 0.1)])

    def test_identical_scores_are_refused(self):
        # The symptom of a GGUF conversion that lost the classification head.
        self.fake.rerank_scores = [4.5e-23, 4.5e-23, 4.5e-23]
        with self.assertRaisesRegex(RuntimeError, "identical score"):
            self.client.rerank("q", ["a", "b", "c"])

    def test_no_documents_needs_no_request(self):
        self.assertEqual(self.client.rerank("q", []), [])
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
