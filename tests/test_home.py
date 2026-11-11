"""Your home area (museion/home_commands.py): the home row in places.csv,
setting it up (weather records, the almanac's frost dates), and the agents'
tools using it."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from museion import almanac, climate, home_commands, maps, notes
from museion.agent_tools import ToolError, all_tools
from museion.appstate import AppState, UiError
from agent_fakes import FakeRun
from test_climate import fake_sources


class HomeRowTest(unittest.TestCase):
    def test_one_home_kept_up_to_date_in_places_csv(self):
        folder = Path(tempfile.mkdtemp())
        self.assertIsNone(maps.home(folder))
        maps.add_place(folder, "Spring", "water", 47.5, -122.1, "good all year")
        maps.set_home(folder, "SeaTac", 47.44, -122.29)
        self.assertEqual((maps.home(folder)["name"], maps.home(folder)["lat"]), ("SeaTac", 47.44))
        notes.write(folder, maps.PLACES, notes.read(folder, maps.PLACES).replace(",home,47.440000,-122.290000,",
                                                                                ",home,47.440000,-122.290000,our house"))
        maps.set_home(folder, "Hounslow", 51.47, -0.36)
        rows = maps.places(folder)
        self.assertEqual([(p["name"], p["kind"]) for p in rows], [("Spring", "water"), ("Hounslow", "home")],
                         "moved, not added")
        self.assertEqual(rows[1]["notes"], "our house", "its notes are kept")
        self.assertEqual(rows[0]["notes"], "good all year")
        with self.assertRaises(maps.MapError):
            maps.set_home(folder, "Nowhere", 95, 0)


class SetUpTest(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.lib = root / "library"
        self.lib.mkdir()
        (root / "config.json").write_text(json.dumps({"library_dir": str(self.lib)}))
        self.events = []
        self.state = AppState(root / "config.json", root / "tuning.toml",
                              lambda kind, data: self.events.append((kind, data)))
        self.get, self.asked = fake_sources()
        patch = mock.patch.object(climate, "http_get", self.get)
        patch.start()
        self.addCleanup(patch.stop)

    def test_setting_up_fills_the_almanac_and_can_be_done_again(self):
        self.assertIsNone(home_commands.home_area(self.state)["home"])
        got = home_commands.set_up_home(self.state, "SeaTac", 47.44, -122.29)
        self.assertEqual(got["home"]["name"], "SeaTac")
        self.assertEqual((got["done"]["stations"], got["done"]["frosts"]), (1, "filled"))
        self.assertEqual(got["climate"]["region"], "within 150 km of SeaTac")
        self.assertEqual(got["estimate"]["zone"], "9a")
        self.assertTrue(got["text"].startswith("Station averages for SeaTac"))
        steps = [d["step"] for kind, d in self.events if kind == "home-progress"]
        self.assertEqual((steps[0], steps[-1]), ("stations", "done"))
        self.assertIn("almanac", steps)
        # The almanac was started, with home's dates, zone and where they came from.
        notes_dir = self.lib / "notes"
        a = almanac.coming_up(notes_dir)
        self.assertEqual((a.frosts, a.zone), ({"last": "03-05", "first": "11-27"}, "9a"))
        self.assertTrue(a.frost_note.startswith(almanac.LOOKED_UP))
        self.assertEqual(a.needs_frost, {}, "the starting almanac's tasks are all timed")
        # Again: nothing is downloaded twice.
        self.asked.clear()
        home_commands.set_up_home(self.state, "SeaTac", 47.44, -122.29)
        self.assertEqual(self.asked, [])
        # Dates the person typed are kept.
        almanac.set_frosts(notes_dir, "04-01", "10-20")
        again = home_commands.set_up_home(self.state, "SeaTac", 47.44, -122.29)
        self.assertTrue(again["done"]["frosts"].startswith("kept"))
        self.assertEqual(almanac.coming_up(notes_dir).frosts, {"last": "04-01", "first": "10-20"})
        # Coming up names home, so the page can look it up.
        from museion import notes_commands
        self.assertEqual(notes_commands.coming_up(self.state)["home"], "SeaTac")

    def test_a_map_of_home_can_be_made_too(self):
        from museion import map_commands
        with mock.patch.object(map_commands, "make_map", return_value={"name": "SeaTac"}) as made:
            got = home_commands.set_up_home(self.state, "SeaTac", 47.44, -122.29, map_km=10)
        self.assertEqual(got["done"]["map"], "SeaTac")
        made.assert_called_once_with(self.state, "SeaTac", 47.44, -122.29, 10.0)

    def test_a_failed_download_keeps_home_and_says_why(self):
        with mock.patch.object(climate, "http_get", side_effect=OSError("no route to host")), \
                self.assertRaisesRegex(UiError, "this needs the internet"):
            home_commands.set_up_home(self.state, "SeaTac", 47.44, -122.29)
        h = home_commands.home_area(self.state)
        self.assertEqual((h["home"]["name"], h["climate"]), ("SeaTac", None))
        with self.assertRaisesRegex(UiError, "No weather station within 150 km of Lincoln"):
            home_commands.set_up_home(self.state, "Lincoln", 40.81, -96.70)
        with self.assertRaisesRegex(UiError, "name"):
            home_commands.set_up_home(self.state, "  ", 40.81, -96.70)


class ToolsTest(unittest.TestCase):
    """With a home area set, the agents need not ask where the person lives."""

    def setUp(self):
        self.lib = Path(tempfile.mkdtemp())
        get, _ = fake_sources()
        climate.build(self.lib, "US-WA", get)
        self.notes = self.lib / "notes"
        self.notes.mkdir()
        notes.write(self.notes, "almanac.csv", "task,kind,when,notes\nlast frost,frost,,\n"
                    "Sow peas outdoors,garden,3-6 weeks before last frost,\n")
        self.run = FakeRun()
        self.run.setting.library_dir = self.lib
        self.run.setting.notes_dir = self.notes

    def test_the_almanac_and_climate_tools_use_home(self):
        almanac_tool, climate_tool = all_tools()["almanac"], all_tools()["climate"]
        self.assertIn("Call almanac again with place", almanac_tool.run({"starting": "February"}, self.run))
        with self.assertRaisesRegex(ToolError, "has not set their home area"):
            climate_tool.run({}, self.run)
        maps.set_home(self.notes, "SeaTac", 47.44, -122.29)
        said = almanac_tool.run({"starting": "February"}, self.run)
        self.assertIn("Station averages for the person's home area, SeaTac", said)
        self.assertIn("Frost dates used: last frost 03-05", said)
        self.assertIn("Sow peas outdoors", said)
        self.assertIn("hardiness zone 9a", climate_tool.run({}, self.run))
        # Dates in almanac.csv still come first.
        almanac.set_frosts(self.notes, "04-10", "10-20")
        self.assertNotIn("Station averages", almanac_tool.run({"starting": "February"}, self.run))


if __name__ == "__main__":
    unittest.main()
