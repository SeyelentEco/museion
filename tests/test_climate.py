"""Frost dates and hardiness zones from station records (museion/climate.py):
reading GHCN-Daily, a station's statistics, the region, the table and
looking a place up."""

import datetime
import gzip
import math
import tempfile
import unittest
from pathlib import Path

from museion import almanac, climate, notes
from museion.agent_tools import ToolError, all_tools
from agent_fakes import FakeRun

D = datetime.date


def lows(lat=47.0, coldest=-5.0, warmest=25.0, shift=lambda y: 0, years=(1991, 2020), skip=lambda d: False):
    """A smooth year of daily lows, coldest in mid-January (mid-July in the
    south), with each year moved by shift(year) degrees."""
    mid, amp = (coldest + warmest) / 2, (warmest - coldest) / 2
    peak = 15 if lat >= 0 else 196
    day = D(years[0] - (1 if lat < 0 else 0), 1, 1)
    while day <= D(years[1] + (1 if lat < 0 else 0), 12, 31):
        n = day.timetuple().tm_yday
        if not skip(day):
            yield day, round(mid - amp * math.cos(2 * math.pi * (n - peak) / 365) + shift(day.year), 1)
        day += datetime.timedelta(days=1)


class ZoneTest(unittest.TestCase):
    def test_usda_zones(self):
        # Zone 9a is 20 to 25 °F (-6.7 to -3.9 °C); 6b is -5 to 0 °F; 1a below -55 °F.
        self.assertEqual(climate.zone(-5.0), "9a")
        self.assertEqual(climate.zone(-18.0), "6b")
        self.assertEqual(climate.zone(-17.7), "7a")  # 0.14 °F
        self.assertEqual(climate.zone(-60.0), "1a")
        self.assertEqual(climate.zone(25.0), "13b")


class StationTest(unittest.TestCase):
    def test_frost_dates_and_zone_of_a_steady_climate(self):
        st = climate.station_stats(47.0, lows())
        self.assertEqual(st.years, 30)
        # The low is at or below 0 °C (rounded, as GHCN gives it) within 49
        # days of 15 January: day 64, 5 March, to day 331, 27 November. In
        # the 8 leap years the curve falls a day earlier from March, which
        # the 1-in-10 autumn date shows.
        self.assertEqual((st.last_frost, st.last_frost_late), ("03-05", "03-05"))
        self.assertEqual((st.first_frost, st.first_frost_early), ("11-27", "11-26"))
        self.assertEqual(st.frost_free_days, 266)
        self.assertEqual((st.frost_years, st.coldest_c, st.zone), (1.0, -5.0, "9a"))

    def test_one_year_in_ten_is_later_in_spring_and_earlier_in_autumn(self):
        # Every third year three degrees colder: its frosts run later and earlier.
        st = climate.station_stats(47.0, lows(shift=lambda y: -3 if y % 3 == 0 else 0))
        self.assertEqual((st.last_frost, st.last_frost_late), ("03-05", "03-19"))
        self.assertEqual((st.first_frost, st.first_frost_early), ("11-26", "11-12"))
        self.assertEqual((st.coldest_c, st.zone), (-6.0, "9a"), "21.2 °F")

    def test_southern_stations_count_the_winter_whole(self):
        st = climate.station_stats(-43.5, lows(lat=-43.5))
        # Coldest in mid-July: frost from late May to early September.
        self.assertEqual((st.first_frost, st.last_frost), ("05-27", "09-02"))
        self.assertEqual(st.zone, "9a")

    def test_no_frost_and_too_little_data(self):
        warm = climate.station_stats(20.0, lows(coldest=6.0))
        self.assertEqual((warm.last_frost, warm.first_frost, warm.frost_years, warm.frost_free_days),
                         ("", "", 0.0, 365))
        self.assertIsNone(climate.station_stats(47.0, lows(years=(2001, 2015))))
        # Readings missing from every spring: no season counts.
        self.assertIsNone(climate.station_stats(47.0, lows(skip=lambda d: d.month in (2, 3))))

    def test_reading_dly_and_by_year_files(self):
        def line(year, month, element, values):
            days = "".join(f"{v:>5}{' '}{q}{'N'}" for v, q in values)
            return f"USW00024233{year}{month:02d}{element}{days}"
        values = [(-12, " "), (-9999, " "), (34, "I")] + [(5, " ")] * 28
        text = [line(2001, 2, "TMIN", values), line(2001, 2, "TMAX", values)]
        got = list(climate.dly_lows(text))
        # 1 Feb, then 2 Feb missing, 3 Feb failed its check, 29-31 Feb do not exist.
        self.assertEqual(got[0], (D(2001, 2, 1), -1.2))
        self.assertEqual(len(got), 26)
        rows = [["USW00024233", "20010201", "TMIN", "-12", "", "", "N", ""],
                ["USW00024233", "20010202", "TMIN", "34", "", "I", "N", ""],
                ["OTHER000000", "20010201", "TMIN", "-50", "", "", "N", ""]]
        self.assertEqual(list(climate.csv_lows(rows, "USW00024233")), [(D(2001, 2, 1), -1.2)])


STATIONS = """\
USW00024233  47.4444 -122.3139  112.8 WA SEATTLE TACOMA INTL AP              GSN     72793
USC00457938  47.6544 -121.9878  -999.9 WA SNOQUALMIE FALLS
UK000003772  51.4789   -0.4489   25.3    LONDON HEATHROW                     GSN     03772
"""
INVENTORY = """\
USW00024233  47.4444 -122.3139 TMIN 1948 2026
USC00457938  47.6544 -121.9878 TMIN 2010 2026
UK000003772  51.4789   -0.4489 TMIN 1973 2026
"""


class TableTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "climate"

    def test_station_list_inventory_and_region(self):
        stations = climate.read_stations(STATIONS.splitlines())
        sea = stations["USW00024233"]
        self.assertEqual((sea.country, sea.state, sea.name, sea.elev_m), ("US", "WA", "SEATTLE TACOMA INTL AP", 112.8))
        self.assertIsNone(stations["USC00457938"].elev_m)
        inventory = climate.tmin_years(INVENTORY.splitlines())
        picked = climate.candidates(stations, inventory, climate.parse_region("US-WA UK"))
        self.assertEqual([s.id for s in picked], ["USW00024233", "UK000003772"], "2010-2020 is too short")
        self.assertEqual([s.id for s in climate.candidates(stations, inventory, climate.parse_region("box:50,-1,52,0"))],
                         ["UK000003772"])
        for bad in ("Washington", "box:1,2", ""):
            with self.subTest(bad), self.assertRaises(climate.ClimateError):
                climate.parse_region(bad)

    def test_write_read_and_estimate(self):
        stations = climate.read_stations(STATIONS.splitlines())
        near = climate.Station("USW00099999", 47.5, -122.3, 10.0, "WA", "NEAR")
        high = climate.Station("USW00088888", 47.45, -122.25, 1500.0, "WA", "MOUNTAIN")
        rows = [climate.row_of(stations["USW00024233"], climate.station_stats(47.4, lows())),
                climate.row_of(near, climate.station_stats(47.5, lows(shift=lambda y: 1))),
                climate.row_of(high, climate.station_stats(47.45, lows(coldest=-15, warmest=15)))]
        climate.write_table(self.dir, rows, {"region": "US-WA", "source": climate.SOURCE})
        table = climate.read_table(self.dir)
        self.assertEqual([r["id"] for r in table], ["USW00024233", "USW00088888", "USW00099999"])
        self.assertEqual(climate.about(self.dir)["region"], "US-WA")
        # Without a height the mountain station counts as much as its distance says.
        e = climate.estimate(table, 47.47, -122.3)
        self.assertEqual(len(e.stations), 3)
        self.assertAlmostEqual(sum(n.weight for n in e.stations), 1.0)
        self.assertIn("MOUNTAIN", [n.row["name"] for n in e.stations])
        # At sea level the mountain station 1,450 m higher is left out: it
        # counts as if 145 km away.
        low = climate.estimate(table, 47.47, -122.3, elev_m=50)
        self.assertEqual([n.row["name"] for n in low.stations], ["NEAR", "SEATTLE TACOMA INTL AP"])
        self.assertEqual(low.zone, "9a")
        self.assertFalse(low.far)
        words = climate.describe(low, "Burien")
        self.assertIn("Station averages for Burien: last spring frost about 03-0", words)
        self.assertIn("hardiness zone 9a", words)
        self.assertIn("Seattle Tacoma Intl Ap", words)
        # Far from every station: the nearest only, and said to be rough.
        far = climate.estimate(table, 40.0, -100.0)
        self.assertEqual((len(far.stations), far.far), (1, True))
        self.assertIn("only a rough guide", climate.describe(far))
        with self.assertRaises(climate.ClimateError):
            climate.read_table(self.dir.parent / "none")

    def test_finding_a_place_offline(self):
        self.dir.mkdir(parents=True)
        (self.dir / climate.TOWNS).write_text(
            "name,label,lat,lon,elev_m,population\n"
            "Bellevue,\"Bellevue, Washington, US\",47.61,-122.20,40,151854\n"
            "Bellevue,\"Bellevue, Nebraska, US\",41.14,-95.91,320,64176\n"
            "Seattle,\"Seattle, Washington, US\",47.61,-122.33,56,737015\n", encoding="utf-8")
        found = climate.find_place("bellevue", self.dir)
        self.assertEqual([p.label for p in found], ["Bellevue, Washington, US", "Bellevue, Nebraska, US"])
        self.assertEqual([p.label for p in climate.find_place("Bellevue, Nebraska", self.dir)],
                         ["Bellevue, Nebraska, US"])
        saved = [{"name": "Home", "lat": 47.5, "lon": -122.0}]
        self.assertEqual(climate.find_place("home", self.dir, saved)[0].label, "Home (your saved place)")
        saved = [{"name": "Hilltop", "kind": "home", "lat": 47.5, "lon": -122.0}]
        self.assertEqual(climate.find_place("hilltop", self.dir, saved)[0].label, "Hilltop (your home area)")
        self.assertEqual(climate.find_place("47.61, -122.33", self.dir)[0].lat, 47.61)
        self.assertEqual(climate.find_place("Atlantis", self.dir), [])
        self.assertEqual(found[0].elev_m, 40.0)


TABLE = ("id,name,country,state,lat,lon,elev_m,years,last_frost,last_frost_late,first_frost,first_frost_early,"
         "frost_free_days,frost_years,coldest_c,zone\n"
         "USC00450872,BELLEVUE,US,WA,47.6000,-122.1500,60,25,03-28,04-20,11-02,10-20,218,1.00,-7.9,8b\n")
TOWNS = "name,label,lat,lon,elev_m,population\nBellevue,\"Bellevue, Washington, US\",47.61,-122.20,40,151854\n"


class UseTest(unittest.TestCase):
    """The almanac's zone row, the agents' tools and the page's look-up."""

    def setUp(self):
        self.lib = Path(tempfile.mkdtemp())
        (self.lib / "climate").mkdir()
        (self.lib / "climate" / climate.STATIONS).write_text(TABLE, encoding="utf-8")
        (self.lib / "climate" / climate.TOWNS).write_text(TOWNS, encoding="utf-8")
        self.notes = self.lib / "notes"
        self.notes.mkdir()
        self.run = FakeRun()
        self.run.setting.library_dir = self.lib
        self.run.setting.notes_dir = self.notes

    def test_the_climate_tool(self):
        said = all_tools()["climate"].run({"place": "Bellevue"}, self.run)
        self.assertIn("Station averages for Bellevue, Washington, US: last spring frost about 03-28", said)
        self.assertIn("hardiness zone 8b", said)
        with self.assertRaisesRegex(ToolError, "No town called 'Atlantis'"):
            all_tools()["climate"].run({"place": "Atlantis"}, self.run)
        # A point far outside the table's region is refused, not guessed.
        with self.assertRaisesRegex(ToolError, "2,1.. km from the nearest station"):
            all_tools()["climate"].run({"place": "41.14, -95.91"}, self.run)

    def test_the_almanac_tool_takes_frost_dates_from_a_place(self):
        notes.write(self.notes, "almanac.csv", "task,kind,when,notes\nlast frost,frost,,\n"
                    "Sow peas outdoors,garden,3-6 weeks before last frost,\n")
        tool = all_tools()["almanac"]
        said = tool.run({"starting": "March"}, self.run)
        self.assertIn("Call almanac again with place: the person's town", said)
        self.assertNotIn("Sow peas", said)
        said = tool.run({"starting": "March", "place": "Bellevue"}, self.run)
        self.assertIn("Station averages for Bellevue, Washington, US", said)
        self.assertIn("Frost dates used: last frost 03-28, first frost 11-02.", said)
        self.assertIn("- Sow peas outdoors (garden, almanac.csv): due now", said)
        # Without a climate table it asks for the dates, as before.
        self.run.setting.library_dir = self.lib / "none"
        self.assertIn("Ask the person for their average last frost date", tool.run({"starting": "March"}, self.run))

    def test_saving_a_look_up_with_its_zone_and_source(self):
        _, chosen, e = climate.look_up(self.lib, "Bellevue")
        note = climate.describe(e, chosen.name)
        almanac.set_frosts(self.notes, e.last_frost, e.first_frost, e.zone, note)
        got = almanac.coming_up(self.notes)
        self.assertEqual((got.frosts, got.zone), ({"last": "03-28", "first": "11-02"}, "8b"))
        self.assertTrue(got.frost_note.startswith("Station averages for Bellevue"))
        self.assertEqual(got.problems, [], "the zone row is not a task")
        # Dates typed by hand later drop the note that no longer applies.
        almanac.set_frosts(self.notes, "04-01", "11-01")
        self.assertEqual(almanac.coming_up(self.notes).frost_note, "")
        with self.assertRaises(almanac.WhenError):
            almanac.set_frosts(self.notes, "", "", "warm")


def dly(station, days):
    """GHCN-Daily .dly lines for these (date, low) pairs, with a TMAX line
    that the build should drop."""
    months: dict[tuple[int, int], dict[int, float]] = {}
    for day, low in days:
        months.setdefault((day.year, day.month), {})[day.day] = low
    out = []
    for (y, m), values in sorted(months.items()):
        cells = "".join(f"{round(values[d] * 10) if d in values else -9999:>5}  N" for d in range(1, 32))
        out.append(f"{station}{y}{m:02d}TMIN{cells}")
        out.append(f"{station}{y}{m:02d}TMAX{cells}")
    return "\n".join(out) + "\n"


LONDON = "UK000003772  51.4789   -0.4489   25.3    LONDON HEATHROW"


def fake_sources(more: dict | None = None):
    """A stand-in for NOAA and GeoNames: get(url) and the URLs it was asked."""
    import io as _io
    import urllib.error
    import zipfile
    cities = _io.BytesIO()
    with zipfile.ZipFile(cities, "w") as z:
        row = lambda gid, name, lat, lon, cc, a1, pop, elev, dem: "\t".join(
            [gid, name, name, "", lat, lon, "P", "PPL", cc, "", a1, "", "", "", pop, elev, dem, "", ""])
        z.writestr("cities1000.txt", "\n".join([
            row("1", "SeaTac", "47.44", "-122.29", "US", "WA", "29000", "", "110"),
            row("2", "Lincoln", "40.81", "-96.70", "US", "NE", "290000", "358", "357"),
            row("3", "Hounslow", "51.47", "-0.36", "GB", "ENG", "103337", "", "20"),
        ]) + "\n")
    files = {
        "ghcnd-stations.txt": STATIONS, "ghcnd-inventory.txt": INVENTORY,
        "all/USW00024233.dly": dly("USW00024233", lows(lat=47.44)),
        "all/UK000003772.dly": dly("UK000003772", lows(lat=51.48, coldest=-1, warmest=14)),
        "admin1CodesASCII.txt": "US.WA\tWashington\tWashington\t5815135\nGB.ENG\tEngland\tEngland\t6269131\n",
        "cities1000.zip": cities.getvalue(),
        **(more or {}),
    }
    asked = []

    def get(url):
        asked.append(url)
        name = url.split("/daily/")[-1].split("/dump/")[-1]
        if name not in files:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        data = files[name]
        return data.encode("utf-8") if isinstance(data, str) else data

    return get, asked


class BuildTest(unittest.TestCase):
    def test_a_region_is_downloaded_once_and_can_be_made_again_offline(self):
        get, asked = fake_sources()
        lib = Path(tempfile.mkdtemp())
        steps = []
        info = climate.build(lib, "US-WA", get, report=lambda step, done, total: steps.append(step))
        self.assertEqual((info["stations"], info["towns"], info["region"]), (1, 1, "US-WA"))
        self.assertIn("station", steps)
        table = climate.read_table(lib / "climate")
        self.assertEqual((table[0]["id"], table[0]["last_frost"], table[0]["zone"]), ("USW00024233", "03-05", "9a"))
        towns = climate.read_towns(lib / "climate")
        self.assertEqual((towns[0].label, towns[0].elev_m), ("SeaTac, Washington, US", 110.0))
        kept = gzip.decompress((lib / "climate" / "sources" / "stations" / "USW00024233.dly.gz").read_bytes())
        self.assertNotIn(b"TMAX", kept, "only the lows are kept")
        # Again with no internet: the same table from what was kept.
        (lib / "climate" / climate.STATIONS).unlink()
        climate.build(lib, "US-WA", None, offline=True)
        self.assertEqual(climate.read_table(lib / "climate"), table)
        # A region with nothing in it says so.
        with self.assertRaisesRegex(climate.ClimateError, "No weather station in US-NE"):
            climate.build(lib, "US-NE", get)

    def test_an_area_around_a_point_adds_to_the_table(self):
        get, _ = fake_sources()
        lib = Path(tempfile.mkdtemp())
        climate.build(lib, "US-WA", get)
        info = climate.build(lib, "near:51.47,-0.40,50", get, label="within 50 km of Hounslow")
        self.assertEqual(info["region"], "US-WA; within 50 km of Hounslow")
        self.assertEqual(sorted(r["id"] for r in climate.read_table(lib / "climate")), ["UK000003772", "USW00024233"],
                         "the earlier region's stations stay")
        self.assertEqual(sorted(t.name for t in climate.read_towns(lib / "climate")), ["Hounslow", "SeaTac"])
        # Made again, the area is not named twice; without keep, it stands alone.
        self.assertEqual(climate.build(lib, "near:51.47,-0.40,50", get, label="within 50 km of Hounslow")["region"],
                         "US-WA; within 50 km of Hounslow")
        self.assertEqual(climate.build(lib, "near:51.47,-0.40,50", get, keep=False)["stations"], 1)
        e = climate.at_point(lib, 51.47, -0.36)
        self.assertEqual((e.elev_m, e.stations[0].row["id"]), (20.0, "UK000003772"), "the height is Hounslow's")
        with self.assertRaisesRegex(climate.ClimateError, "km from the nearest station"):
            climate.at_point(lib, 47.44, -122.29)
        with self.assertRaisesRegex(climate.ClimateError, "No weather station within 5 km of Lincoln"):
            climate.build(lib, "near:40.81,-96.70,5", get, label="within 5 km of Lincoln")

    def test_stop_keeps_what_was_downloaded(self):
        import threading
        get, asked = fake_sources()
        lib = Path(tempfile.mkdtemp())
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(climate.ClimateError, "Stopped"):
            climate.build(lib, "US-WA", get, stop=stop)
        self.assertFalse((lib / "climate" / climate.STATIONS).exists())
        self.assertTrue((lib / "climate" / "sources" / "ghcnd-stations.txt.gz").is_file(), "kept, gzipped")
        asked.clear()
        climate.build(lib, "US-WA", get)
        self.assertNotIn(f"{climate.NOAA}/ghcnd-stations.txt", asked, "carried on from what was kept")


class AreaTest(unittest.TestCase):
    def test_a_circle_and_thinning_a_city_s_cluster(self):
        stations = climate.read_stations(STATIONS.splitlines())
        region = climate.parse_region("near:47.6,-122.2,50")
        self.assertIn(stations["USW00024233"], region)
        self.assertNotIn(stations["UK000003772"], region)
        for bad in ("near:47.6,-122.2", "near:95,0,10", "near:47,-122,0"):
            with self.subTest(bad), self.assertRaises(climate.ClimateError):
                climate.parse_region(bad)
        # Twenty stations crowded in a city, and five spread around it.
        city = [climate.Station(f"C{i:02d}", 47.6 + i * 0.001, -122.2, 50.0, "WA", "CITY") for i in range(20)]
        spread = [climate.Station(f"S{i}", 47.6 + d, -122.2 + d, 50.0, "WA", "OUT")
                  for i, d in enumerate((0.3, -0.3, 0.6, -0.6, 0.9))]
        taken = climate.thin(city + spread, 47.6, -122.2, 10)
        self.assertEqual(len(taken), 10)
        self.assertEqual([s.id for s in taken[:5]], ["C00", "C01", "C02", "C03", "C04"], "the nearest half")
        self.assertEqual({s.id for s in taken[5:]}, {"S0", "S1", "S2", "S3", "S4"}, "then spread out")


if __name__ == "__main__":
    unittest.main()
