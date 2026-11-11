"""The person's notes and records: the folder, the index of its own, the
tools, and how an answer built on notes is labelled."""

import copy
import datetime
import os
import tempfile
import time
import unittest
from pathlib import Path

from museion import ask, notes
from museion.runs import read_log
from museion.agent_tools import ToolError, all_tools
from museion.agents import AgentSpec
from museion.harness import Harness, Person, Setting
from museion.index import Hit, Index
from museion.tuning import defaults as default_tuning
from agent_fakes import FakeRun, ScriptedChat, calls, says


def tool(name):
    return all_tools()[name]


class FolderTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_names_stay_inside_and_are_text(self):
        for bad in ("../x.md", "/etc/x.md", "C:/x.md", ".hidden.md", "a/.history/x.md", "x.exe", "", "a/../../x.md"):
            with self.subTest(bad):
                with self.assertRaises(notes.NoteError):
                    notes.path_of(self.dir, bad)
        self.assertEqual(notes.path_of(self.dir, "health/log.md"), (self.dir / "health" / "log.md").resolve())

    def test_write_keeps_the_previous_version_and_listing_skips_museions_files(self):
        notes.write(self.dir, "pump.md", "Washer is 12 mm.")
        notes.write(self.dir, "pump.md", "Washer is 14 mm.")
        notes.write(self.dir, "pump.md", "Spare in the shed.", append=True)
        self.assertEqual(notes.read(self.dir, "pump.md"), "Washer is 14 mm.\nSpare in the shed.\n")
        kept = sorted(p.read_text() for p in (self.dir / notes.HISTORY).iterdir())
        self.assertEqual(kept, ["Washer is 12 mm.\n", "Washer is 14 mm.\n"])
        notes.search(self.dir, "washer", 5)  # builds .index.db
        self.assertEqual([f.name for f in notes.listing(self.dir)], ["pump.md"])

    def test_records_start_with_their_header(self):
        notes.start_record(self.dir, "medicines.csv")
        self.assertTrue(notes.read(self.dir, "medicines.csv").startswith("name,strength,form,quantity"))
        with self.assertRaises(notes.NoteError):
            notes.start_record(self.dir, "nonsense.csv")
        files = [k.file for k in notes.record_kinds()]
        self.assertIn("maintenance.csv", files)
        self.assertEqual(len(files), len(set(files)))


class PartsTest(unittest.TestCase):
    def test_markdown_is_cut_at_headings_and_long_sections(self):
        text = "# Pump\n\nThe washer.\n\n## Spares\n\nIn the shed.\n\n" + ("word " * 600)
        got = notes.parts("pump.md", text)
        self.assertEqual(got.pages[0].text, "# Pump\n\nThe washer.")
        self.assertTrue(got.pages[1].text.startswith("## Spares"))
        self.assertGreater(len(got.pages), 2)
        self.assertTrue(all(len(p.text) <= notes.SECTION_CHARS for p in got.pages))

    def test_a_record_is_written_out_row_by_row_with_its_columns(self):
        text = "item,quantity,unit\nrice,10,kg\n\nbeans,12,cans\n"
        got = notes.parts("stores.csv", text)
        self.assertEqual(got.pages[0].text,
                         "stores.csv, rows 1-2 of 2:\nitem: rice; quantity: 10; unit: kg\nitem: beans; quantity: 12; unit: cans")
        rows = "item,quantity\n" + "".join(f"thing{i},{i}\n" for i in range(60))
        self.assertEqual(len(notes.parts("big.csv", rows).pages), 3)
        self.assertIn("no rows yet", notes.parts("empty.csv", "item,quantity\n").pages[0].text)


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_finds_notes_and_sees_changes(self):
        self.assertEqual(notes.search(self.dir / "missing", "pump", 5), ([], 0.0))
        notes.write(self.dir, "pump.md", "The well pump needed a new leather washer.")
        hits, best = notes.search(self.dir, "well pump washer", 5)
        self.assertEqual([(h.book, h.file, h.page) for h in hits], [("notes", "pump.md", 1)])
        self.assertEqual(best, 1.0)
        # A change is seen at once, even within the same second.
        notes.write(self.dir, "pump.md", "Replaced the impeller instead.")
        later = time.time() + 5
        os.utime(self.dir / "pump.md", (later, later))
        self.assertEqual(notes.search(self.dir, "washer", 5)[0], [])
        self.assertEqual(len(notes.search(self.dir, "impeller", 5)[0]), 1)
        (self.dir / "pump.md").unlink()
        self.assertEqual(notes.search(self.dir, "impeller", 5)[0], [])

    def test_notes_join_the_librarys_candidates_and_are_labelled(self):
        notes.write(self.dir, "stores.csv", "item,quantity,unit\nrice,10,kg\n")
        index = Index(":memory:")
        self.addCleanup(index.close)
        a = ask.Asker(index, ScriptedChat(), tuning=copy.deepcopy(default_tuning()))
        hits, _ = a.gather("how much rice do we have", notes_dir=self.dir)
        self.assertEqual([h.file for h in hits], ["stores.csv"])
        shown = ask.render_search(hits, [])
        self.assertIn("[1] stores.csv -- the person's own note, not a book (part 1)", shown)
        self.assertEqual(a.gather("how much rice do we have")[0], [])


class DatesTest(unittest.TestCase):
    def test_expiry_and_every_n_days(self):
        today = datetime.date(2027, 3, 10)
        meds = "name,expires\namoxicillin,2027-03-01\nparacetamol,2027-04-01\nsaline,\nzinc,2027-03-10\n"
        dated, undated = notes.dates_due(meds, "expires", today=today)
        self.assertEqual([(d.row["name"], d.days) for d in dated],
                         [("amoxicillin", -9), ("zinc", 0), ("paracetamol", 22)])
        self.assertEqual([r["name"] for r in undated], ["saline"])
        jobs = "machine,task,every_days,last_done\ngenerator,oil,30,2027-02-01\npump,grease,90,2027-03-01\n"
        dated, _ = notes.dates_due(jobs, "last_done", "every_days", today=today)
        self.assertEqual([(d.row["machine"], d.due.isoformat(), d.days) for d in dated],
                         [("generator", "2027-03-03", -7), ("pump", "2027-05-30", 81)])
        with self.assertRaises(notes.NoteError):
            notes.dates_due(jobs, "when")


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun()
        self.dir = Path(tempfile.mkdtemp())
        self.run.setting.notes_dir = self.dir

    def test_write_read_list(self):
        self.assertTrue(tool("list_notes").run({}, self.run).startswith("There are no notes yet. Today is "))
        tool("write_note").run({"name": "stores.csv", "text": "item,quantity\nrice,10"}, self.run)
        self.assertEqual(tool("read_note").run({"name": "stores.csv"}, self.run), "item,quantity\nrice,10\n")
        self.assertIn("stores.csv (", tool("list_notes").run({}, self.run))
        self.assertEqual(self.run.files, ["notes/stores.csv"])
        with self.assertRaises(ToolError):
            tool("read_note").run({"name": "nothing.md"}, self.run)
        with self.assertRaises(ToolError):
            tool("write_note").run({"name": "../escape.md", "text": "x"}, self.run)

    def test_no_notes_folder(self):
        self.run.setting.notes_dir = None
        with self.assertRaises(ToolError):
            tool("list_notes").run({}, self.run)

    def test_the_card_shows_what_would_change(self):
        write = tool("write_note")
        self.assertEqual(write.summary({"name": "a.csv", "text": "item\nrice"}, self.run),
                         "write your note a.csv:\nitem\nrice")
        notes.write(self.dir, "a.csv", "item,quantity\nrice,10\nbeans,4\n")
        said = write.summary({"name": "a.csv", "text": "item,quantity\nrice,12\nbeans,4\n"}, self.run)
        self.assertEqual(said, "change your note a.csv (+ added, - removed):\n-rice,10\n+rice,12")
        self.assertEqual(write.risk, "ask")

    def test_a_note_lists_the_sources_it_cites(self):
        def found(n, page):
            return Hit(doc_title=f"Book {n}", author="", book="b", file=f"{n}.pdf", page=page, snippet="",
                       rank=0.0, coverage=1.0, body="")
        self.run.sources = [found(1, 4), found(2, 0), found(3, 9)]
        write = tool("write_note")
        plan = "# Plan\nPotatoes, 40 m2 [3]. Beans [1][3]. Kale [7]."
        listed = "\n\nSources:\n[1] Book 1, page 4\n[3] Book 3, page 9"
        self.assertTrue(write.summary({"name": "plan.md", "text": plan}, self.run).endswith(listed[2:]),
                        "the card shows the list too")
        write.run({"name": "plan.md", "text": plan}, self.run)
        self.assertEqual(notes.read(self.dir, "plan.md"), plan + listed + "\n")
        # A record, a note with its own list, or one citing nothing is left as it is.
        for name, text in (("seeds.csv", "item,source\nbeans,[2]"), ("own.md", "Beans [2].\n\n## Sources\n[2] Mine"),
                           ("bare.md", "Water the beans.")):
            with self.subTest(name):
                write.run({"name": name, "text": text}, self.run)
                self.assertEqual(notes.read(self.dir, name), text + "\n")

    def test_check_dates(self):
        soon = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        gone = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        far = (datetime.date.today() + datetime.timedelta(days=400)).isoformat()
        notes.write(self.dir, "medicines.csv", f"name,expires\nzinc,{soon}\niodine,{gone}\nsalt,{far}\ngauze,\n")
        said = tool("check_dates").run({"name": "medicines.csv", "column": "expires"}, self.run)
        lines = said.splitlines()
        self.assertTrue(lines[0].startswith("Today is "))
        self.assertIn("OVERDUE by 1 day (", lines[1])
        self.assertIn("iodine", lines[1])
        self.assertIn("due in 3 days", lines[2])
        self.assertIn("1 more row(s) fall due after 30 days.", said)
        self.assertIn("1 row(s) have no date in expires: gauze", said)


class BasisTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        notes.write(self.dir, "pump.md", "The well pump needed a new leather washer.")
        index = Index(":memory:")
        self.addCleanup(index.close)
        self.asker = ask.Asker(index, None, tuning=copy.deepcopy(default_tuning()))

    def run_agent(self, *turns, approve="allow"):
        class P(Person):
            def __init__(self):
                self.requests = []

            def approve(self, request):
                self.requests.append(request)
                return approve, ""

        person = P()
        spec = AgentSpec(id="t", name="T", goal="g", tools=["search_library", "write_note"],
                         guards=[], prompt="p", max_rounds=4)
        chat = ScriptedChat(*turns)
        self.asker.chat = chat
        answer = Harness(spec, Setting(chat=chat, library=self.asker, notes_dir=self.dir, person=person)).run("q")
        return answer, person

    def test_an_answer_citing_only_notes_says_so(self):
        answer, _ = self.run_agent(calls(("search_library", {"query": "pump washer"})), says("Your note says a washer [1]."))
        self.assertEqual(answer.basis, "notes")
        self.assertEqual(answer.sources[0].book, "notes")

    def test_writing_a_note_waits_for_allow(self):
        answer, person = self.run_agent(calls(("write_note", {"name": "x.md", "text": "hi"})), says("Saved."),
                                        approve="deny")
        self.assertFalse((self.dir / "x.md").exists())
        self.assertEqual(person.requests[0].summary, "write your note x.md:\nhi")
        answer, _ = self.run_agent(calls(("write_note", {"name": "x.md", "text": "hi"})), says("Saved."))
        self.assertEqual(notes.read(self.dir, "x.md"), "hi\n")
        self.assertEqual(answer.files, ["notes/x.md"])



class FirstStepsFlowTest(unittest.TestCase):
    """The first-steps agent's own case, played through with a scripted model:
    it reads the person's records, works out what runs out first, hands water
    to the water planner and saves its plan in the notes."""

    def test_reads_records_delegates_and_saves_the_plan(self):
        from museion import evals, hardware
        from museion.agent_run import Agents, CaseRunner, load_cases
        root = Path(tempfile.mkdtemp())
        env = Agents(root / "library", root / "mine", hardware.AMPLE)
        spec = env.get("first-steps")
        case = [c for c in load_cases(spec) if c.id == "power-on-comms-down"]
        plan = ("Tom's insulin comes first, then water: it lasts 2 days [1].\n\n## Today\n"
                "- [ ] Fill every clean container with water while the taps run.\n"
                "- [ ] Count Tom's insulin and keep it cool.\n- [ ] Agree a meeting place with neighbours.\n"
                "## This week\n- [ ] Get fuel and food for a month.\n")
        chat = ScriptedChat(
            calls(("list_notes", {})),
            calls(("read_note", {"name": "household.csv"}), ("read_note", {"name": "stores.csv"})),
            calls(("calculate", {"expression": "24 / (4 * 3)"})),
            calls(("delegate", {"agent": "water-planner", "task": "3 people, 24 litres stored, taps still run."})),
            calls(("search_library", {"query": "storing drinking water"})),  # the helper searches first
            says("You have 24 litres [1]: fill every clean container now and keep it covered."),
            calls(("write_note", {"name": "first-steps-plan.md", "text": plan})),
            says(plan),
        )
        index = Index(":memory:")
        self.addCleanup(index.close)
        library = ask.Asker(index, chat, tuning=copy.deepcopy(default_tuning()))
        run = evals.run(CaseRunner(env, spec, chat, library), case, "t")
        self.assertTrue(run.cases[0].passed, run.cases[0].checks)
        read = [m["content"] for m in chat.calls[2][0] if m["role"] == "tool"]
        self.assertTrue(any("type 1 diabetes" in r for r in read), read)
        self.assertIn("Today is ", [m["content"] for m in chat.calls[1][0] if m["role"] == "tool"][0])
        self.assertFalse((root / "library" / "notes").exists())  # the person's notes were never touched
        log = Path(run.cases[0].log)  # kept beside the results, with the water planner's beside it
        self.assertEqual([e["kind"] for e in read_log(log)][::len(read_log(log)) - 1], ["start", "done"])
        self.assertEqual(len(list(log.parent.glob("*.jsonl"))), 2)


if __name__ == "__main__":
    unittest.main()
