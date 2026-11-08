"""Chats and the Agents tab's commands, run as the page runs them, against a
stand-in chat model: a chat is saved with its answer, a question card waits
for the person's reply, Stop ends a waiting run, and a saved setting goes to
the person's copy only."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from museion import agent_commands, chat_commands, hardware
from museion.appstate import AppState, UiError
from fake_llama import FakeLlama, call_fragment, text


def port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


class Setup:
    """A config and library in a temporary folder, a stand-in chat model, and
    an Ample computer, so every agent can run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        self.chat = FakeLlama()
        self.addCleanup(self.chat.close)
        (folder / "config").mkdir()
        (folder / "library").mkdir()
        config = {"library_dir": str(folder / "library"), "chat_port": port_of(self.chat.url),
                  "reranker_port": 9, "kiwix_port": 9}
        (folder / "config" / "config.json").write_text(json.dumps(config))
        self.events: list[tuple[str, dict]] = []
        self.state = AppState(folder / "config" / "config.json", folder / "config" / "tuning.toml",
                              lambda kind, data: self.events.append((kind, data)))
        tier = mock.patch.object(AppState, "hardware", lambda s: mock.Mock(tier=hardware.AMPLE))
        tier.start()
        self.addCleanup(tier.stop)
        self.addCleanup(self.tmp.cleanup)

    def waiting(self, chat: str) -> dict:
        for _ in range(200):
            live = chat_commands.RUNNING.get(chat)
            if live and live.waiting:
                return live.waiting
            time.sleep(0.05)
        self.fail("the run never waited for the person")


class ChatCommandsTest(Setup, unittest.TestCase):
    def test_a_chat_keeps_its_messages_and_files(self):
        chat = chat_commands.new_chat(self.state, "stores-keeper")
        self.chat.script = [
            [call_fragment(0, "c0", "list_notes", "{}")],
            [call_fragment(0, "c1", "write_note", json.dumps({"name": "stores.csv", "text": "item,quantity\nbeans,12\n"}))],
            [text("Saved 12 cans of beans.")],
        ]
        out = {}
        t = threading.Thread(target=lambda: out.update(
            chat_commands.send_message(self.state, chat["id"], "We have 12 cans of beans.")))
        t.start()
        card = self.waiting(chat["id"])
        self.assertEqual(card["tool"], "write_note")
        self.assertIn("beans,12", card["summary"])
        chat_commands.answer_agent(self.state, chat["id"], decision="allow")
        t.join(10)
        reply = out
        self.assertEqual(reply["text"], "Saved 12 cans of beans.")
        self.assertEqual(reply["files"], ["notes/stores.csv"])
        self.assertEqual(reply["basis"], "own_files")
        library = Path(self.tmp.name) / "library"
        self.assertEqual((library / "notes" / "stores.csv").read_text(), "item,quantity\nbeans,12\n")
        saved = chat_commands.get_chat(self.state, chat["id"])
        self.assertEqual([m["role"] for m in saved["messages"]], ["user", "assistant"])
        self.assertEqual(saved["title"], "We have 12 cans of beans.")
        self.assertTrue(any(k == "agent-step" for k, _ in self.events))
        listed = chat_commands.list_chats(self.state)
        self.assertEqual(listed["chats"][0]["agent_name"], "Stores keeper")

    def test_a_question_card_waits_for_the_answer(self):
        chat = chat_commands.new_chat(self.state, "stores-keeper")
        self.chat.script = [
            [call_fragment(0, "c1", "ask_person", json.dumps({"question": "How many people?"}))],
            [text("Noted: four people.")],
        ]
        out = {}
        t = threading.Thread(target=lambda: out.update(chat_commands.send_message(self.state, chat["id"], "Plan for us.")))
        t.start()
        self.assertEqual(self.waiting(chat["id"])["question"], "How many people?")
        chat_commands.answer_agent(self.state, chat["id"], answer="Four")
        t.join(10)
        self.assertEqual(out["text"], "Noted: four people.")
        sent = self.chat.requests[-1]["messages"]
        self.assertIn("Four", sent[-1]["content"])

    def test_stop_ends_a_waiting_run(self):
        chat = chat_commands.new_chat(self.state, "stores-keeper")
        self.chat.script = [[call_fragment(0, "c1", "ask_person", json.dumps({"question": "How many?"}))]]
        out = {}
        t = threading.Thread(target=lambda: out.update(chat_commands.send_message(self.state, chat["id"], "Plan.")))
        t.start()
        self.waiting(chat["id"])
        chat_commands.stop_run(self.state, chat["id"])
        t.join(10)
        self.assertTrue(out["stopped"])
        self.assertNotIn(chat["id"], chat_commands.RUNNING)
        with self.assertRaises(UiError):
            chat_commands.answer_agent(self.state, chat["id"], answer="late")

    def test_saving_settings_writes_only_the_persons_copy(self):
        agent_commands.save_agent(self.state, "ration-scaler", {"max_rounds": 3}, None)
        got = agent_commands.get_agent(self.state, "ration-scaler")
        self.assertEqual(got["settings"]["max_rounds"], 3)
        self.assertTrue(got["yours"])
        mine = Path(got["your_folder"]) / "agent.toml"
        self.assertIn("max_rounds = 3", mine.read_text())
        self.assertNotIn("goal", mine.read_text())


class ExportImportTest(Setup, unittest.TestCase):
    def test_export_keeps_tier_tables_and_import_round_trips(self):
        import tomllib
        import zipfile
        agent_commands.save_agent(self.state, "study-guide", {"max_rounds": 7}, None)
        path = agent_commands.export_agent(self.state, "study-guide")
        with zipfile.ZipFile(path) as z:
            data = tomllib.loads(z.read("study-guide/agent.toml").decode())
        shipped = tomllib.loads((Path(agent_commands.agents.SHIPPED_DIR) / "study-guide" / "agent.toml").read_text())
        self.assertEqual(data["max_rounds"], 7)
        # Not flattened for this computer: the tables are kept, less the
        # setting the person's copy made for every tier.
        self.assertEqual(data["tier"]["Generous"], {k: v for k, v in shipped["tier"]["Generous"].items() if k != "max_rounds"})
        self.assertNotIn("shared_rules", data)
        got = agent_commands.import_agent(self.state, path)
        self.assertEqual(got["settings"]["max_rounds"], 7)
        self.assertEqual(got["problems"], [])

    def test_import_refuses_names_that_could_leave_the_folder(self):
        import zipfile
        for bad in ("x/..\\..\\evil.md", "x/C:\\Users\\evil.md", "x/../evil.md", "x/.hidden.md", "x/run.py"):
            path = Path(self.tmp.name) / "bad.zip"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("x/agent.toml", 'name = "X"\n')
                z.writestr(bad, "boo")
            with self.subTest(bad), self.assertRaises(UiError):
                agent_commands.import_agent(self.state, str(path))

    def test_a_helper_that_cannot_run_here_is_refused(self):
        from museion.harness import Harness
        env = chat_commands.environment(self.state)
        agent_commands.save_agent(self.state, "ration-scaler", {"enabled": False}, None)
        setting = env.setting(None)
        with self.assertRaises(Exception) as caught:
            Harness(env.get("three-day-planner"), setting).delegate("ration-scaler", "scale")
        self.assertIn("turned off", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
