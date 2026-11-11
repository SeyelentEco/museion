"""The harness with a scripted model: tools, approvals, guards, compaction,
delegation, the run log, and stopping."""

import tempfile
import threading
import unittest
from pathlib import Path

from museion import guards
from museion.agent_tools import all_tools
from museion.agents import AgentSpec, problems_of
from museion.harness import Harness, Person, Setting, Stopped
from museion.index import Hit
from museion.runs import RunLog, read_log
from agent_fakes import ScriptedChat, calls, says


def spec(**kw) -> AgentSpec:
    base = dict(id="tester", name="Tester", goal="test", tools=["calculate", "write_file", "ask_person"],
                guards=["calculate_first", "call_again", "cite_or_stand"], prompt="Be useful.", max_rounds=4)
    base.update(kw)
    return AgentSpec(**base)


class Approving(Person):
    def __init__(self, decision="allow", note=""):
        self.decision, self.note, self.requests, self.questions = decision, note, [], []

    def approve(self, request):
        self.requests.append(request)
        return self.decision, self.note

    def ask(self, agent, question):
        self.questions.append(question)
        return "Four of us."


def hit(n):
    return Hit(doc_title=f"Book {n}", author="", book="b", file=f"{n}.pdf", page=n, snippet="",
               rank=0.0, coverage=1.0, body=f"Passage {n} body.")


class FakeLibrary:
    def __init__(self):
        self.tuning = type("T", (), {"keep": 8})()
        self.reranker = None

    def gather(self, query):
        return [hit(1), hit(2)], True


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_tool_then_answer(self):
        chat = ScriptedChat(calls(("calculate", {"expression": "2*3"})), says("It is 6."))
        answer = Harness(spec(), Setting(chat=chat)).run("2 times 3?")
        self.assertEqual(answer.text, "It is 6.")
        self.assertEqual(answer.basis, "computed")
        self.assertEqual(answer.tools, ["calculate"])
        self.assertEqual(answer.rounds, 2)
        # Only the agent's own tools are offered.
        offered = [t["function"]["name"] for t in chat.calls[0][1]]
        self.assertEqual(offered, ["calculate", "write_file", "ask_person"])

    def test_a_tool_that_failed_is_not_counted_as_used(self):
        chat = ScriptedChat(calls(("calculate", {"expression": "1/0"})), says("Cannot."))
        answer = Harness(spec(), Setting(chat=chat)).run("1/0?")
        self.assertEqual(answer.tools, [])
        self.assertEqual(answer.basis, "general_knowledge")

    def test_a_guard_after_a_continuation_sends_the_answer_back_once(self):
        chat = ScriptedChat(says("First part, 20 / 8 = 2.5", limited=True), says(" and the rest."),
                            calls(("calculate", {"expression": "20/8"})), says("It is 2.5."))
        Harness(spec(guards=["calculate_first"]), Setting(chat=chat)).run("how much?")
        sent = chat.calls[2][0]
        firsts = [m for m in sent if m["role"] == "assistant" and "First part" in (m.get("content") or "")]
        self.assertEqual(len(firsts), 1)
        self.assertEqual(sent[-1]["content"], guards.CALCULATE_FIRST)
        self.assertNotIn(guards.CONTINUE, [m.get("content") for m in sent])

    def test_unknown_and_bad_arguments_go_back_to_the_model(self):
        chat = ScriptedChat(calls(("delete_everything", {}), ("calculate", {"expr": "1"})), says("Done."))
        Harness(spec(), Setting(chat=chat)).run("go")
        results = [m["content"] for m in chat.calls[1][0] if m["role"] == "tool"]
        self.assertEqual(results[0], "Unknown tool delete_everything.")
        self.assertIn("no parameter expr", results[1])

    def test_workspace_write_runs_without_asking_and_is_recorded(self):
        person = Approving("deny")
        chat = ScriptedChat(calls(("write_file", {"name": "a.md", "text": "hello"})), says("Saved."))
        answer = Harness(spec(), Setting(chat=chat, person=person), workspace=self.dir).run("save")
        self.assertEqual((self.dir / "a.md").read_text(), "hello\n")
        self.assertEqual(answer.files, ["a.md"])
        self.assertEqual(person.requests, [])

    def test_confirm_all_asks_before_workspace_writes_and_deny_is_told(self):
        person = Approving("deny", "not that file")
        chat = ScriptedChat(calls(("write_file", {"name": "a.md", "text": "x"})), says("Fine."))
        Harness(spec(confirm="all"), Setting(chat=chat, person=person), workspace=self.dir).run("save")
        self.assertFalse((self.dir / "a.md").exists())
        self.assertEqual(person.requests[0].tool, "write_file")
        told = [m["content"] for m in chat.calls[1][0] if m["role"] == "tool"][0]
        self.assertIn("The person declined: not that file", told)

    def test_ask_risk_tools_always_ask_and_allow_for_run_is_remembered(self):
        person = Approving("allow_run")
        lib = self.dir / "library"
        lib.mkdir()
        (lib / "x.zim").write_bytes(b"zim")
        args = {"file": "x.zim", "title": "X", "category": "reference"}
        chat = ScriptedChat(calls(("add_source", args)), calls(("add_source", args)), says("Done."))
        s = spec(tools=["add_source"], guards=[])
        from museion import catalog
        original = catalog.CATALOG_DIR
        catalog.CATALOG_DIR = self.dir / "catalog"
        (self.dir / "catalog" / "sources").mkdir(parents=True)
        try:
            Harness(s, Setting(chat=chat, person=person, library_dir=lib)).run("add it")
        finally:
            catalog.CATALOG_DIR = original
        self.assertTrue((self.dir / "catalog" / "sources" / "x.toml").exists())
        self.assertEqual(len(person.requests), 1)  # the second identical call was allowed already
        self.assertIn("add x.zim to the catalogue", person.requests[0].summary)

    def test_ask_person(self):
        person = Approving()
        chat = ScriptedChat(calls(("ask_person", {"question": "How many?"})), says("For four, then."))
        Harness(spec(), Setting(chat=chat, person=person)).run("plan")
        self.assertEqual(person.questions, ["How many?"])

    def test_a_question_in_prose_is_asked_with_the_card(self):
        person = Approving()
        chat = ScriptedChat(says("How many people are in your household?"),
                            calls(("ask_person", {"question": "How many people?"})), says("Plan for four."))
        s = spec(tools=["ask_person"], guards=[])
        answer = Harness(s, Setting(chat=chat, person=person)).run("plan")
        self.assertEqual(chat.calls[1][0][-1]["content"], guards.ASK_BY_CARD)
        self.assertEqual(person.questions, ["How many people?"])
        self.assertEqual(answer.text, "Plan for four.")

    def test_questions_that_end_with_a_promise_are_asked_with_the_card_too(self):
        # What the 35B wrote: the question mark is not on the last line.
        prose = ("I need to know your household.\n\n1. How many adults are there?\n2. Any children?\n\n"
                 "Once I have these numbers, I can calculate the total.")
        chat = ScriptedChat(says(prose), calls(("ask_person", {"question": "How many?"})), says("Plan for four."))
        answer = Harness(spec(tools=["ask_person"], guards=[]), Setting(chat=chat, person=Approving())).run("plan")
        self.assertEqual(chat.calls[1][0][-1]["content"], guards.ASK_BY_CARD)
        self.assertEqual(answer.text, "Plan for four.")

    def test_a_cited_answer_that_asks_on_the_way_stands(self):
        text = "Boil it for a minute [1]. Is the water cloudy? Filter it first [1]."
        chat = ScriptedChat(says(text))
        Harness(spec(tools=["ask_person"], guards=[]), Setting(chat=chat)).run("water")
        self.assertEqual(len(chat.calls), 1, "not sent back")

    def test_without_ask_person_a_question_stands(self):
        chat = ScriptedChat(says("How many people are you?"))
        answer = Harness(spec(tools=["calculate"], guards=[]), Setting(chat=chat)).run("plan")
        self.assertEqual(answer.text, "How many people are you?")

    def test_guard_fires_once_and_withholding_ends_tools(self):
        lib = FakeLibrary()
        chat = ScriptedChat(calls(("search_library", {"query": "water"})), says("Boil it."),
                            says("Boil it [1]."))
        s = spec(tools=["search_library"], guards=["cite_or_stand"])
        answer = Harness(s, Setting(chat=chat, library=lib)).run("water?")
        self.assertEqual(chat.calls[2][0][-1]["content"], guards.CITE_OR_STAND)
        self.assertIsNone(chat.calls[2][1])  # no tools after cite_or_stand
        self.assertEqual(answer.basis, "library")
        self.assertEqual(answer.text, "Boil it [1].")
        self.assertEqual(answer.rounds, 3, "the model's turns, not the round the guard jumped to")

    def test_an_answer_without_searching_is_sent_back_to_search(self):
        # On the PC the clinic told someone how to stop bleeding from memory,
        # with the library's first-aid books one search away.
        lib = FakeLibrary()
        chat = ScriptedChat(says("Press on it hard."), calls(("search_library", {"query": "bleeding"})),
                            says("Press on it hard [1]."))
        s = spec(tools=["search_library", "read_note"], guards=["search_first", "cite_or_stand"])
        answer = Harness(s, Setting(chat=chat, library=lib)).run("bleeding?")
        self.assertEqual(chat.calls[1][0][-1]["content"], guards.SEARCH_FIRST)
        self.assertIsNotNone(chat.calls[1][1], "tools stay on offer, to search")
        self.assertEqual((answer.text, answer.basis), ("Press on it hard [1].", "library"))

    def test_search_first_lets_a_search_or_the_records_stand(self):
        class Run:
            round, max_rounds, sources, calculations = 1, 4, [], []
            tools = {"search_library": None, "read_note": None}
            used: list = []

        run = Run()
        self.assertIsNotNone(guards.search_first("Boil it.", run))
        for used in (["search_library"], ["read_note"], ["check_dates"]):
            run.used = used  # searched and found nothing, or answered from the person's records
            self.assertIsNone(guards.search_first("Boil it.", run), used)
        run.used, run.round = [], 4
        self.assertIsNone(guards.search_first("Boil it.", run), "the last round must answer")
        run.round, run.tools = 1, {"read_note": None}
        self.assertIsNone(guards.search_first("Boil it.", run), "an agent that cannot search")

    def test_asking_twice_is_the_limit(self):
        # On the PC first-steps asked thirteen times in one answer, told
        # "I don't know." each time, and never made the plan.
        person = Approving()
        ask = ("ask_person", {"question": "How many are you?"})
        chat = ScriptedChat(calls(ask), calls(ask, ask), says("A plan for four."))
        answer = Harness(spec(), Setting(chat=chat, person=person)).run("plan")
        self.assertEqual(len(person.questions), 2)
        results = [m["content"] for m in chat.calls[2][0] if m["role"] == "tool"]
        spent = all_tools()["ask_person"].spent
        self.assertTrue(results[1].endswith(spent))  # told on its last question...
        self.assertEqual(results[2], spent)  # ...and a third gets nothing from the person
        self.assertNotIn("ask_person", [t["function"]["name"] for t in chat.calls[2][1]])
        self.assertEqual((answer.text, answer.tools), ("A plan for four.", ["ask_person", "ask_person"]))

    def test_an_agent_can_limit_a_tool(self):
        lib = FakeLibrary()
        chat = ScriptedChat(calls(("search_library", {"query": "beans"})), says("Beans [1]."))
        s = spec(tools=["search_library", "calculate"], guards=[], max_calls={"search_library": 1})
        Harness(s, Setting(chat=chat, library=lib)).run("beans?")
        self.assertIn("cannot be used again", [m["content"] for m in chat.calls[1][0] if m["role"] == "tool"][0])
        self.assertEqual([t["function"]["name"] for t in chat.calls[1][1]], ["calculate"])
        self.assertIn("max_calls names read_page, which is not one of its tools",
                      problems_of(spec(max_calls={"read_page": 2})))
        self.assertIn("max_calls for calculate must be a whole number, 1 or more",
                      problems_of(spec(max_calls={"calculate": 0})))

    def test_a_plan_left_unsaved_gets_a_last_round_to_save_it(self):
        notes_dir = self.dir / "notes"
        notes_dir.mkdir()
        s = spec(tools=["calculate", "write_note"], guards=[], max_rounds=2, saves="plan.md")
        plan = ("write_note", {"name": "plan.md", "text": "Potatoes, 40 m2."})
        chat = ScriptedChat(calls(("calculate", {"expression": "10*4"})), calls(("calculate", {"expression": "40/2"})),
                            calls(plan), says("Plant potatoes: 40 m2."))
        person = Approving("allow")
        answer = Harness(s, Setting(chat=chat, notes_dir=notes_dir, person=person)).run("plan it")
        self.assertEqual(answer.text, "Plant potatoes: 40 m2.")
        self.assertEqual((notes_dir / "plan.md").read_text(encoding="utf-8"), "Potatoes, 40 m2.\n")
        self.assertEqual(answer.files, ["notes/plan.md"])
        self.assertEqual(len(person.requests), 1, "the person still allows the save")
        # The save round offers write_note alone, and the round after it nothing.
        self.assertEqual([t["function"]["name"] for t in chat.calls[2][1]], ["write_note"])
        self.assertEqual(chat.calls[2][0][-1]["content"], guards.save_now("plan.md"))
        self.assertIsNone(chat.calls[3][1])
        self.assertEqual(chat.calls[3][0][-1]["content"], guards.NO_MORE_TOOLS)

    def test_no_save_round_when_saved_declined_or_nothing_found(self):
        notes_dir = self.dir / "notes"
        notes_dir.mkdir()
        s = spec(tools=["calculate", "write_note"], guards=[], max_rounds=2, saves="plan.md")
        saved = ScriptedChat(calls(("write_note", {"name": "other.md", "text": "x"})),
                             calls(("calculate", {"expression": "1+1"})), says("Done."))
        declined = ScriptedChat(calls(("calculate", {"expression": "1+1"})),
                                calls(("write_note", {"name": "plan.md", "text": "x"})), says("Done."))
        nothing = ScriptedChat(calls(("list_notes", {})), calls(("list_notes", {})), says("Done."))
        cases = [(saved, "allow"), (declined, "deny")]
        for chat, decision in cases:
            with self.subTest(decision):
                Harness(s, Setting(chat=chat, notes_dir=notes_dir, person=Approving(decision))).run("plan it")
                self.assertIsNone(chat.calls[-1][1])
                self.assertEqual(chat.calls[-1][0][-1]["content"], guards.NO_MORE_TOOLS)
        # Nothing searched or worked out: nothing to save, whatever was written.
        s = spec(tools=["list_notes", "write_note"], guards=[], max_rounds=2, saves="plan.md")
        Harness(s, Setting(chat=nothing, notes_dir=notes_dir)).run("plan it")
        self.assertIsNone(nothing.calls[-1][1])
        self.assertEqual(nothing.calls[-1][0][-1]["content"], guards.NO_MORE_TOOLS)
        self.assertIn("saves names a note, but write_note is not one of its tools",
                      problems_of(spec(saves="plan.md")))

    def test_no_tools_means_no_tool_offer_and_no_no_more_tools(self):
        chat = ScriptedChat(says("Hello."))
        Harness(spec(tools=[], guards=[]), Setting(chat=chat)).run("hi")
        self.assertIsNone(chat.calls[0][1])
        self.assertNotIn(guards.NO_MORE_TOOLS, [m["content"] for m in chat.calls[0][0]])

    def test_compaction_folds_old_searches_and_keeps_numbers(self):
        lib = FakeLibrary()
        lib.gather = lambda q: ([Hit(f"T{q}", "", "b", f"{q}.pdf", p, "", 0.0, 1.0, "x" * 3000)
                                 for p in range(1, 9)], False)
        turns = [calls(("search_library", {"query": str(i)})) for i in range(4)] + [says("Done [1].")]
        chat = ScriptedChat(*turns, context=16000)
        s = spec(tools=["search_library"], guards=[], max_rounds=6, keep_rounds=1)
        steps = []
        answer = Harness(s, Setting(chat=chat, library=lib, on_step=lambda k, d: steps.append(k))).run("q")
        self.assertIn("compact", steps)
        folded = [m["content"] for m in chat.calls[-1][0] if m["role"] == "tool" and "removed to save room" in m["content"]]
        self.assertTrue(folded)
        self.assertIn("passages 1", folded[0])
        self.assertEqual(answer.basis, "library")  # [1] still names the first passage

    def test_compaction_off_answers_early_instead(self):
        lib = FakeLibrary()
        lib.gather = lambda q: ([Hit("T", "", "b", f"{q}.pdf", p, "", 0.0, 1.0, "x" * 3000)
                                 for p in range(1, 9)], False)
        chat = ScriptedChat(calls(("search_library", {"query": "a"})), calls(("search_library", {"query": "b"})),
                            says("Done [1]."), context=16000)
        s = spec(tools=["search_library"], guards=[], compact=False, max_rounds=6)
        Harness(s, Setting(chat=chat, library=lib)).run("q")
        self.assertIn(guards.NO_MORE_TOOLS, [m["content"] for m in chat.calls[-1][0]])

    def test_delegate_runs_a_helper_in_a_fresh_context(self):
        helper = spec(id="helper", tools=["search_library", "delegate", "add_source"], guards=[])
        lib = FakeLibrary()
        chat = ScriptedChat(
            calls(("delegate", {"agent": "helper", "task": "find water"})),
            calls(("search_library", {"query": "water"})),  # the helper's turns
            says("Boil it [2]."),
            says("Plan: boil water [1]."),
        )
        parent = spec(tools=["delegate", "calculate"], guards=[])
        setting = Setting(chat=chat, library=lib, find_agent=lambda i: helper)
        answer = Harness(parent, setting).run("plan")
        helper_first = chat.calls[1]
        self.assertEqual(helper_first[0][-1]["content"], "find water")  # fresh: only its task
        offered = [t["function"]["name"] for t in helper_first[1]]
        self.assertEqual(offered, ["search_library"])  # no delegate, nothing riskier than the parent
        result = [m["content"] for m in chat.calls[3][0] if m["role"] == "tool"][0]
        self.assertIn("Boil it [1]", result)  # renumbered into the parent's passages
        self.assertEqual(answer.basis, "library")
        self.assertEqual(answer.sources[0].doc_title, "Book 2")

    def test_log_and_steps(self):
        log = RunLog(self.dir / "runs")
        chat = ScriptedChat(calls(("calculate", {"expression": "1+1"})), says("2."))
        setting = Setting(chat=chat, open_log=lambda s: log)
        answer = Harness(spec(), setting).run("1+1 please")
        kinds = [s["kind"] for s in read_log(log.path)]
        self.assertEqual(kinds, ["start", "turn", "tool", "turn", "done"])
        self.assertEqual(answer.log, str(log.path))

    def test_stop(self):
        stop = threading.Event()
        stop.set()
        chat = ScriptedChat(says("never"))
        with self.assertRaises(Stopped):
            Harness(spec(), Setting(chat=chat, stop=stop)).run("go")

    def test_notes_go_in_the_prompt(self):
        (self.dir / "notes.md").write_text("- The household is 4 people.\n")
        chat = ScriptedChat(says("ok"))
        Harness(spec(tools=["notes"]), Setting(chat=chat), workspace=self.dir).run("hi")
        self.assertIn("The household is 4 people", chat.calls[0][0][0]["content"])

    def test_arguments_written_as_text(self):
        chat = ScriptedChat(says("x"))
        h = Harness(spec(), Setting(chat=chat))
        call = {"id": "1", "function": {"name": "calculate", "arguments": "2+2"}}
        self.assertIn("2+2 = 4", h.call(call, ""))


if __name__ == "__main__":
    unittest.main()
