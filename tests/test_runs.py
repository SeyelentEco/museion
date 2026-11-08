"""Run logs, chats, and running an agent's cases."""

import tempfile
import unittest
from pathlib import Path

from museion import agents, evals, hardware
from museion.agent_run import Agents, CaseRunner, save_case
from museion.runs import ChatStore, title_from
from agent_fakes import ScriptedChat, calls, says


class ChatStoreTest(unittest.TestCase):
    def test_chats(self):
        store = ChatStore(Path(tempfile.mkdtemp()))
        a = store.new("librarian", "Water")
        b = store.new("stores-keeper")
        a.messages.append({"role": "user", "text": "hi"})
        store.save(a)
        self.assertEqual([c["id"] for c in store.list()], [a.id, b.id])
        self.assertEqual([c["id"] for c in store.list("stores-keeper")], [b.id])
        self.assertEqual(store.get(a.id).messages[0]["text"], "hi")
        store.delete(b.id)
        with self.assertRaises(KeyError):
            store.get(b.id)
        with self.assertRaises(KeyError):
            store.get("../config")

    def test_title(self):
        self.assertEqual(title_from("  How much   water? "), "How much water?")
        self.assertTrue(title_from("word " * 30).endswith("…"))


class CaseRunnerTest(unittest.TestCase):
    def test_given_files_person_and_checks(self):
        root = Path(tempfile.mkdtemp())
        env = Agents(root / "library", root / "mine", hardware.CAPABLE)
        spec = env.get("stores-keeper")
        case = evals.load_cases('''
[[cases]]
id = "days"
prompt = "How long will the rice last?"
given_files = { "stores.csv" = "item,quantity,unit,per_person_per_day,notes\\nrice,10,kg,0.2,\\n" }
person_says = ["We are 4."]
expect_tool = ["read_file", "ask_person", "calculate"]
expect_number = { value = 12.5, tolerance = 0 }
max_rounds_used = 5
''', agent=True)
        chat = ScriptedChat(
            calls(("read_file", {"name": "stores.csv"})),
            calls(("ask_person", {"question": "How many of you?"})),
            calls(("calculate", {"expression": "10 / (0.2 * 4)"})),
            says("The rice lasts 12.5 days."),
        )
        run = evals.run(CaseRunner(env, spec, chat), case, "t")
        self.assertTrue(run.cases[0].passed, run.cases[0].checks)
        read = [m["content"] for m in chat.calls[1][0] if m["role"] == "tool"][0]
        self.assertIn("rice,10,kg", read)
        self.assertFalse((root / "library").exists())  # the real folder was not touched

    def test_save_case(self):
        root = Path(tempfile.mkdtemp())
        spec = agents.load("water-planner", None, hardware.CAPABLE)
        path = save_case(spec, root, {"id": "new-one", "prompt": "How much?", "expect_any": ["litre"],
                                      "expect_number": {"value": 4.5, "tolerance": 0.1}})
        cases = evals.load_cases(path.read_text(encoding="utf-8"), agent=True)
        self.assertEqual(cases[-1].id, "new-one")
        self.assertGreater(len(cases), 1)  # the shipped cases came with it
        with self.assertRaises(ValueError):
            save_case(spec, root, {"id": "new-one", "prompt": "again"})


class BasisTest(unittest.TestCase):
    def test_own_files_only_without_passages_or_sums(self):
        from museion.answer import basis_of
        self.assertEqual(basis_of(False, False, ["read_file"]), "own_files")
        self.assertEqual(basis_of(False, False, ["search_library"]), "general_knowledge")
        self.assertEqual(basis_of(False, True, ["read_file"]), "computed")
        self.assertEqual(basis_of(True, False, ["notes"]), "library")

    def test_writing_a_note_does_not_vouch_for_an_answer(self):
        from museion.answer import basis_of
        # Searched, found nothing, noted the household, answered from memory.
        self.assertEqual(basis_of(False, False, ["search_library", "notes"]), "general_knowledge")
        self.assertEqual(basis_of(False, False, ["write_file"]), "general_knowledge")
        self.assertEqual(basis_of(False, False, ["search_library", "read_file"]), "general_knowledge")



if __name__ == "__main__":
    unittest.main()
