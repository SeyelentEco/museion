"""The seasonal almanac (museion/almanac.py): reading a task's time, what is
due when, the starter record, the frost dates, and the almanac tool."""

import datetime
import tempfile
import unittest
from pathlib import Path

from museion import almanac, notes
from museion.agent_tools import ToolError, all_tools
from agent_fakes import FakeRun

D = datetime.date
FROSTS = {"last": (4, 20), "first": (10, 15)}


def window(text, year=2026):
    return almanac.parse_when(text).windows(year, FROSTS)[1]


class WhenTest(unittest.TestCase):
    def test_times_from_the_frost_dates(self):
        self.assertEqual(window("6-8 weeks before last frost"), (D(2026, 2, 23), D(2026, 3, 9)))
        self.assertEqual(window("8-6 weeks before the last spring frost"), (D(2026, 2, 23), D(2026, 3, 9)))
        self.assertEqual(window("1-2 weeks after last frost"), (D(2026, 4, 27), D(2026, 5, 4)))
        self.assertEqual(window("10 days before first frost"), (D(2026, 10, 5), D(2026, 10, 11)),
                         "one time is the week from it")
        self.assertEqual(window("at first frost"), (D(2026, 10, 15), D(2026, 10, 21)))
        self.assertEqual(window("First Fall Frost"), (D(2026, 10, 15), D(2026, 10, 21)))

    def test_months_and_dates(self):
        self.assertEqual(window("March"), (D(2026, 3, 1), D(2026, 3, 31)))
        self.assertEqual(window("early March"), (D(2026, 3, 1), D(2026, 3, 10)))
        self.assertEqual(window("mid-Sept"), (D(2026, 9, 11), D(2026, 9, 20)))
        self.assertEqual(window("late February"), (D(2026, 2, 21), D(2026, 2, 28)))
        self.assertEqual(window("late February", 2028), (D(2028, 2, 21), D(2028, 2, 29)))
        self.assertEqual(window("November to February"), (D(2026, 11, 1), D(2027, 2, 28)))
        self.assertEqual(window("04-15"), (D(2026, 4, 15), D(2026, 4, 21)))
        self.assertEqual(window("15 April to 10 May"), (D(2026, 4, 15), D(2026, 5, 10)))
        self.assertIsNone(almanac.parse_when("  "))
        for bad in ("whenever", "13-01", "early Smarch", "04-15 to soon"):
            with self.subTest(bad), self.assertRaises(almanac.WhenError):
                almanac.parse_when(bad)

    def test_the_southern_hemisphere_needs_nothing_special(self):
        # Last frost in September, first in May: the same words work.
        south = {"last": (9, 15), "first": (5, 1)}
        w = almanac.parse_when("6-8 weeks before last frost").windows(2026, south)[1]
        self.assertEqual(w, (D(2026, 7, 21), D(2026, 8, 4)))


class ComingUpTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_the_starter_almanac_is_all_readable(self):
        notes.start_record(self.dir, "almanac.csv")
        _, rows = notes.rows_of(notes.read(self.dir, "almanac.csv"))
        self.assertGreater(len(rows), 20)
        found = almanac.coming_up(self.dir, D(2026, 3, 1))
        self.assertEqual(found.problems, [])
        self.assertEqual(found.frosts, {}, "the frost dates are the person's to give")
        self.assertGreater(found.needs_frost["last"], 5)
        self.assertGreater(found.needs_frost["first"], 5)
        self.assertEqual(found.items, [])
        almanac.set_frosts(self.dir, "04-20", "October 15")
        found = almanac.coming_up(self.dir, D(2026, 3, 1))
        self.assertEqual((found.frosts, found.needs_frost), ({"last": "04-20", "first": "10-15"}, {}))
        tasks = {i.task: i.status for i in found.items}
        self.assertEqual(tasks["Start tomatoes and peppers indoors"], "now")
        self.assertEqual(tasks["Plant potatoes"], "soon")
        self.assertNotIn("Plant garlic", tasks)
        # The rest of the record is kept as it was, and so is the old version.
        text = notes.read(self.dir, "almanac.csv")
        self.assertTrue(text.startswith("task,kind,when,notes\nlast frost,frost,04-20,"))
        self.assertIn("Plant garlic,garden,1-3 weeks after first frost,", text)
        self.assertTrue(list((self.dir / ".history").iterdir()))

    def test_due_now_soon_and_overdue_from_all_three_records(self):
        notes.write(self.dir, "almanac.csv", "task,kind,when,notes\n"
                    "last frost,frost,04-20,\n"
                    "Sow peas,garden,3-6 weeks before last frost,once the soil can be worked\n"
                    "Plant out tomatoes,garden,1-2 weeks after last frost,\n"
                    "Clean the chimney,maintenance,September,\n"
                    "Prune apples,garden,late January to February,\n"
                    "Plant garlic,garden,at first frost,\n"
                    "Lime the beds,garden,whenever,\n"
                    "Weed,garden,,\n")
        notes.write(self.dir, "maintenance.csv", "machine,task,every_days,last_done,notes\n"
                    "generator,oil change,100,2026-01-01,\nwell pump,grease,365,2025-03-20,\n")
        notes.write(self.dir, "medicines.csv", "name,strength,expires\nparacetamol,500 mg,2026-04-01\nzinc,,2027-01-01\n")
        found = almanac.coming_up(self.dir, D(2026, 3, 20), weeks=6)
        got = [(i.task, i.status, i.days) for i in found.items]
        self.assertEqual(got, [
            ("Sow peas", "now", 0),
            ("well pump: grease", "now", 0),
            ("paracetamol 500 mg expires", "soon", 12),
            ("generator: oil change", "soon", 22),
            ("Plant out tomatoes", "soon", 38),
        ])
        self.assertEqual(found.needs_frost, {"first": 1})
        self.assertEqual(len(found.problems), 1)
        self.assertIn("Lime the beds", found.problems[0])
        # A window that began last year is still now.
        self.assertIn(("Prune apples", "now", 0), [(i.task, i.status, i.days) for i in
                                                   almanac.coming_up(self.dir, D(2027, 2, 2)).items])
        # What is overdue comes first, the longest overdue at the top.
        later = almanac.coming_up(self.dir, D(2026, 4, 25)).items
        self.assertEqual([(i.task, i.status, i.days) for i in later[:2]],
                         [("well pump: grease", "overdue", -36), ("paracetamol 500 mg expires", "overdue", -24)])

    def test_no_records_at_all(self):
        found = almanac.coming_up(self.dir, D(2026, 3, 20))
        self.assertEqual((found.items, found.has_almanac, found.problems), ([], False, []))

    def test_frost_dates_are_checked_and_added_if_missing(self):
        with self.assertRaises(almanac.WhenError):
            almanac.set_frosts(self.dir, "spring", "")
        notes.write(self.dir, "almanac.csv", "task,kind,when,notes\nPlant garlic,garden,at first frost,\n")
        almanac.set_frosts(self.dir, "", "15 Oct")
        self.assertEqual(notes.read(self.dir, "almanac.csv"),
                         "task,kind,when,notes\nlast frost,frost,,\nfirst frost,frost,10-15,\nPlant garlic,garden,at first frost,\n")


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun()
        self.dir = Path(tempfile.mkdtemp())
        self.run.setting.notes_dir = self.dir
        self.tool = all_tools()["almanac"]

    def test_says_what_is_due_and_what_is_missing(self):
        said = self.tool.run({}, self.run)
        self.assertIn("no almanac.csv yet", said)
        notes.start_record(self.dir, "almanac.csv")
        said = self.tool.run({"starting": "March"}, self.run)
        self.assertIn("timed from the last frost, which almanac.csv does not give yet", said)
        almanac.set_frosts(self.dir, "04-20", "10-15")
        said = self.tool.run({"starting": "March", "weeks": 4}, self.run)
        self.assertIn("Frost dates in almanac.csv: last frost 04-20, first frost 10-15.", said)
        self.assertIn("- Start tomatoes and peppers indoors (garden, almanac.csv): due now, until ", said)
        self.assertIn("the record says: 6-8 weeks before last frost", said)
        self.assertNotIn("Plant garlic", said)
        with self.assertRaises(ToolError):
            self.tool.run({"starting": "Smarch"}, self.run)

    def test_a_month_means_the_next_one(self):
        from museion.agent_tools.almanac import starting_day
        today = D(2026, 11, 11)
        self.assertEqual(starting_day("March", today), D(2027, 3, 1))
        self.assertEqual(starting_day("december", today), D(2026, 12, 1))
        self.assertEqual(starting_day("November", today), today)
        self.assertEqual(starting_day("2026-11-20", today), D(2026, 11, 20))
        for now in ("", "today", "Now", "this month"):
            self.assertEqual(starting_day(now, today), today)
        # A model that does not know the date guesses one from its training.
        with self.assertRaisesRegex(ToolError, "2023-10-01 is in the past: today is 2026-11-11"):
            starting_day("2023-10-01", today)


if __name__ == "__main__":
    unittest.main()
