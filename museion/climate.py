"""Frost dates and hardiness zones by place, with no internet: a table of
weather stations, built once while there is internet and kept in the
library (<library>/climate/), looked up by the stations nearest a place.

The stations' records are NOAA's GHCN-Daily, which are public domain:
https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily
For each station, from its daily lows (TMIN) over YEARS:

- last spring frost and first autumn frost: the median date the low fell
  to 0 °C or below, and a later (spring) or earlier (autumn) date that is
  passed only 1 year in 10;
- frost-free days: the median days between the two;
- how many years in ten had any frost;
- the average coldest night of the year, which is what a USDA hardiness
  zone is: zone 1 starts at -60 °F and each zone is 10 °F, in halves "a"
  and "b" of 5 °F. Worked out here from station data the way the USDA
  does, but not the official USDA map.

A place is found offline in the region's town list (GeoNames, CC BY 4.0,
kept beside the stations), the person's saved places, or as "lat, lon".
Its estimate is the stations near it, nearer and similar in height
counting for more.
"""

from __future__ import annotations

import csv
import datetime
import gzip
import io
import json
import math
import os
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

FOLDER = "climate"
STATIONS = "stations.csv"
TOWNS = "towns.csv"
ABOUT = "about.json"
YEARS = (1991, 2020)
MIN_YEARS = 20  # seasons with enough data, of the 30
COVERAGE = 0.8  # of a half-season's days that need a reading for it to count
FROST_C = 0.0
# Where a season's spring half ends: day 196 from its start (15 July in the
# north). In the south the season starts on 1 July, so the whole winter is
# in one season and "spring" runs July to January.
SPRING_DAYS = 196
SOURCE = ("NOAA NCEI, Global Historical Climatology Network - Daily (GHCN-Daily), public domain; "
          "towns from GeoNames (CC BY 4.0)")
COLUMNS = ["id", "name", "country", "state", "lat", "lon", "elev_m", "years", "last_frost", "last_frost_late",
           "first_frost", "first_frost_early", "frost_free_days", "frost_years", "coldest_c", "zone"]
NEAR_KM = 60  # stations further than this are only used when none is nearer
FAR_KM = 250  # with no station nearer than this, the place is outside the table's region
HEIGHT_KM_PER_M = 0.1  # 100 m of height counts like 10 km of distance


class ClimateError(RuntimeError):
    """A problem worded for the person."""


# ── zones ────────────────────────────────────────────────────────────────────

def zone(coldest_c: float) -> str:
    """The USDA hardiness zone for an average coldest night: 1a below
    -55 °F ... 13b from 65 °F."""
    f = coldest_c * 9 / 5 + 32
    steps = math.floor((f + 60) / 5)  # half-zones above -60 °F
    steps = max(0, min(steps, 25))
    return f"{steps // 2 + 1}{'ab'[steps % 2]}"


# ── one station's record ─────────────────────────────────────────────────────

def dly_lows(lines: Iterable[str]) -> Iterator[tuple[datetime.date, float]]:
    """The daily lows (°C) in a GHCN-Daily .dly file: fixed-width lines of
    one month and element each, 31 days of value (tenths of a degree) and
    flags. Readings that failed a quality check (QFLAG set) are left out."""
    for line in lines:
        if line[17:21] != "TMIN":
            continue
        year, month = int(line[11:15]), int(line[15:17])
        for i in range(31):
            at = 21 + 8 * i
            value, qflag = line[at:at + 5], line[at + 6:at + 7]
            if value.strip() in ("", "-9999") or qflag.strip():
                continue
            try:
                day = datetime.date(year, month, i + 1)
            except ValueError:
                continue
            yield day, int(value) / 10


def csv_lows(rows: Iterable[list[str]], station: str) -> Iterator[tuple[datetime.date, float]]:
    """The same from GHCN-Daily's by-year files: ID,YYYYMMDD,ELEMENT,VALUE,
    MFLAG,QFLAG,SFLAG,OBS-TIME."""
    for r in rows:
        if len(r) < 6 or r[0] != station or r[2] != "TMIN" or r[5].strip():
            continue
        d = r[1]
        yield datetime.date(int(d[:4]), int(d[4:6]), int(d[6:8])), int(r[3]) / 10


@dataclass
class Stats:
    years: int
    last_frost: str  # "04-20", or "" when most years have none
    last_frost_late: str  # passed 1 year in 10
    first_frost: str
    first_frost_early: str
    frost_free_days: int
    frost_years: float  # share of years with any frost
    coldest_c: float | None
    zone: str


def month_day(south: bool, day: int) -> str:
    """A day of a season as MM-DD, counted in a year with no 29 February."""
    d = datetime.date(2001, 7 if south else 1, 1) + datetime.timedelta(days=day)
    return f"{d.month:02d}-{d.day:02d}"


def rank(values: list[float], share: float) -> float:
    """The value at this share of the way up the sorted list (nearest rank)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def station_stats(lat: float, lows: Iterable[tuple[datetime.date, float]], years: tuple[int, int] = YEARS,
                  min_years: int = MIN_YEARS) -> Stats | None:
    """A station's frost dates and coldest night over `years`, or None if
    too few years have readings enough to tell."""
    south = lat < 0
    first, last = years
    by_season: dict[int, dict[int, float]] = {}
    by_year: dict[int, list[float]] = {}
    for day, low in lows:
        if first <= day.year <= last:
            by_year.setdefault(day.year, []).append(low)
        if day.month == 2 and day.day == 29:
            continue  # seasons are counted in years of 365 days
        y = day.year if not south or day.month >= 7 else day.year - 1
        if first <= y <= last:
            by_season.setdefault(y, {})[_season_day(f"{day.month:02d}-{day.day:02d}", south)] = low
    springs: list[float] = []
    autumns: list[float] = []
    free: list[float] = []
    frosty = 0
    counted = 0
    for y, days in by_season.items():
        spring = [n for n in days if n < SPRING_DAYS]
        autumn = [n for n in days if n >= SPRING_DAYS]
        if len(spring) < COVERAGE * SPRING_DAYS or len(autumn) < COVERAGE * (365 - SPRING_DAYS):
            continue
        counted += 1
        s = max((n for n in spring if days[n] <= FROST_C), default=None)
        a = min((n for n in autumn if days[n] <= FROST_C), default=None)
        springs.append(-1 if s is None else s)
        autumns.append(365 if a is None else a)
        free.append((365 if a is None else a) - (0 if s is None else s + 1))
        frosty += s is not None or a is not None
    if counted < min_years:
        return None
    lows_by_year = [min(v) for v in by_year.values() if len(v) >= 330]
    coldest = round(sum(lows_by_year) / len(lows_by_year), 1) if len(lows_by_year) >= min_years else None

    def spring_day(share: float) -> str:
        n = rank(springs, share)
        return "" if n < 0 else month_day(south, int(n))

    def autumn_day(share: float) -> str:
        n = rank(autumns, share)
        return "" if n >= 365 else month_day(south, int(n))

    return Stats(counted, spring_day(0.5), spring_day(0.9), autumn_day(0.5), autumn_day(0.1),
                 int(rank(free, 0.5)), round(frosty / counted, 2), coldest,
                 zone(coldest) if coldest is not None else "")


# ── the station list ─────────────────────────────────────────────────────────

@dataclass
class Station:
    id: str
    lat: float
    lon: float
    elev_m: float | None
    state: str
    name: str

    @property
    def country(self) -> str:
        return self.id[:2]


def read_stations(lines: Iterable[str]) -> dict[str, Station]:
    """ghcnd-stations.txt: ID, LATITUDE, LONGITUDE, ELEVATION, STATE, NAME,
    in fixed columns."""
    out = {}
    for line in lines:
        if len(line) < 41:
            continue
        elev = float(line[31:37])
        out[line[:11]] = Station(line[:11], float(line[12:20]), float(line[21:30]),
                                 None if elev <= -999 else elev, line[38:40].strip(), line[41:71].strip())
    return out


def tmin_years(lines: Iterable[str]) -> dict[str, tuple[int, int]]:
    """ghcnd-inventory.txt: each station's first and last year of TMIN."""
    out = {}
    for line in lines:
        if line[31:35] == "TMIN":
            out[line[:11]] = (int(line[36:40]), int(line[41:45]))
    return out


@dataclass
class Region:
    """Which stations a table covers: countries by GHCN's two-letter code
    (US, UK, CA ...), states or provinces (US-WA, CA-BC), and boxes
    (box:south,west,north,east)."""
    countries: set[str] = field(default_factory=set)
    states: set[tuple[str, str]] = field(default_factory=set)
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    text: str = ""

    def __contains__(self, s: Station) -> bool:
        return (s.country in self.countries or (s.country, s.state) in self.states
                or any(b[0] <= s.lat <= b[2] and b[1] <= s.lon <= b[3] for b in self.boxes))


def parse_region(text: str) -> Region:
    region = Region(text=text.strip())
    for part in text.replace(";", " ").split():
        part = part.strip().strip(",")
        if not part:
            continue
        if part.lower().startswith("box:"):
            try:
                s, w, n, e = (float(v) for v in part[4:].split(","))
            except ValueError:
                raise ClimateError(f"{part!r}: a box is box:south,west,north,east, like box:45.5,-124.8,49,-116.9") from None
            region.boxes.append((min(s, n), min(w, e), max(s, n), max(w, e)))
        elif len(part) == 2 and part.isalpha():
            region.countries.add(part.upper())
        elif len(part) == 5 and part[2] == "-" and part.replace("-", "").isalpha():
            region.states.add((part[:2].upper(), part[3:].upper()))
        else:
            raise ClimateError(f"{part!r} is not a region: use a country code like US or UK, a state like US-WA, "
                               "or box:south,west,north,east.")
    if not (region.countries or region.states or region.boxes):
        raise ClimateError("Say which region: a country code like US or UK, a state like US-WA, "
                           "or box:south,west,north,east.")
    return region


def candidates(stations: dict[str, Station], inventory: dict[str, tuple[int, int]], region: Region,
               years: tuple[int, int] = YEARS, min_years: int = MIN_YEARS) -> list[Station]:
    """The region's stations whose lows could cover enough of `years`."""
    out = []
    for sid, s in stations.items():
        span = inventory.get(sid)
        if span is None or s not in region:
            continue
        overlap = min(span[1], years[1]) - max(span[0], years[0]) + 1
        if overlap >= min_years:
            out.append(s)
    return out


# ── building the table ───────────────────────────────────────────────────────

NOAA = os.environ.get("MUSEION_GHCN", "https://www.ncei.noaa.gov/pub/data/ghcn/daily")
GEONAMES = os.environ.get("MUSEION_GEONAMES", "https://download.geonames.org/export/dump")
SOURCES = "sources"  # what was downloaded, kept so the table can be made again offline


def http_get(url: str) -> bytes:
    from museion.osm import USER_AGENT
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except (OSError, urllib.error.URLError) as e:
            last = e
        time.sleep(2 ** attempt)
    assert last is not None
    raise last


class Fetcher:
    """Files from NOAA and GeoNames, each downloaded once into sources/ and
    read from there after; offline, only what is there."""

    def __init__(self, folder: Path, get: Callable[[str], bytes] | None = http_get, offline: bool = False):
        self.folder, self.get, self.offline = folder, get, offline
        self.downloaded = 0

    def file(self, name: str, url: str, keep: Callable[[bytes], bytes] = lambda b: b) -> Path:
        path = self.folder / name
        if path.is_file():
            return path
        if self.offline or self.get is None:
            raise ClimateError(f"{name} was never downloaded, and this is building with no internet.")
        try:
            data = keep(self.get(url))
        except (OSError, urllib.error.URLError) as e:
            from museion.osm import reason
            raise ClimateError(f"Could not download {url} (this needs the internet): {reason(e)}") from None
        self.downloaded += len(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_bytes(data)
        part.replace(path)
        return path

    def text(self, name: str, url: str) -> list[str]:
        return self.file(name, url).read_text(encoding="utf-8", errors="replace").splitlines()

    def lows(self, station: str) -> list[str]:
        """A station's TMIN lines; only those are kept, gzipped."""
        def keep(data: bytes) -> bytes:
            lines = [ln for ln in data.decode("ascii", errors="replace").splitlines() if ln[17:21] == "TMIN"]
            return gzip.compress(("\n".join(lines) + "\n").encode("ascii"))
        try:
            path = self.file(f"stations/{station}.dly.gz", f"{NOAA}/all/{station}.dly", keep)
        except urllib.error.HTTPError:
            return []
        return gzip.decompress(path.read_bytes()).decode("ascii").splitlines()


def build(library_dir: Path, region_text: str, get: Callable[[str], bytes] | None = http_get,
          offline: bool = False, years: tuple[int, int] = YEARS, min_years: int = MIN_YEARS,
          report: Callable[[str, int, int], None] = lambda step, done, total: None) -> dict:
    """Make <library>/climate/stations.csv (and towns.csv) for a region:
    download the station list and each of the region's stations' daily
    lows from NOAA and the towns from GeoNames, once, and work out each
    station's frost dates and zone. What was downloaded stays in sources/,
    so it can be made again with no internet (offline=True)."""
    region = parse_region(region_text)
    where = Path(library_dir) / FOLDER
    fetch = Fetcher(where / SOURCES, get, offline)
    report("stations", 0, 1)
    stations = read_stations(fetch.text("ghcnd-stations.txt", f"{NOAA}/ghcnd-stations.txt"))
    inventory = tmin_years(fetch.text("ghcnd-inventory.txt", f"{NOAA}/ghcnd-inventory.txt"))
    picked = candidates(stations, inventory, region, years, min_years)
    if not picked:
        raise ClimateError(f"No station in {region.text} has daily lows for {min_years} of the years "
                           f"{years[0]}-{years[1]}.")
    rows = []
    for i, s in enumerate(picked):
        report("station", i, len(picked))
        st = station_stats(s.lat, dly_lows(fetch.lows(s.id)), years, min_years)
        if st is not None:
            rows.append(row_of(s, st))
    report("station", len(picked), len(picked))
    if not rows:
        raise ClimateError(f"None of the {len(picked)} stations in {region.text} had enough readings.")
    report("towns", 0, 1)
    towns = near_towns(fetch, rows)
    write_towns(where, towns)
    info = {"region": region.text, "years": list(years), "min_years": min_years, "stations": len(rows),
            "towns": len(towns), "built": datetime.date.today().isoformat(), "source": SOURCE}
    write_table(where, rows, info)
    report("towns", 1, 1)
    return {**info, "downloaded_bytes": fetch.downloaded, "folder": str(where)}


def near_towns(fetch: Fetcher, rows: list[dict[str, str]]) -> list[Place]:
    """GeoNames' towns of 1,000 people or more that lie within NEAR_KM of a
    station in the table, named with their state or province."""
    admin1 = {}
    for line in fetch.text("admin1CodesASCII.txt", f"{GEONAMES}/admin1CodesASCII.txt"):
        parts = line.split("\t")
        if len(parts) >= 2:
            admin1[parts[0]] = parts[1]
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for r in rows:
        lat, lon = float(r["lat"]), float(r["lon"])
        grid.setdefault((math.floor(lat), math.floor(lon)), []).append((lat, lon))
    out = []
    with zipfile.ZipFile(fetch.file("cities1000.zip", f"{GEONAMES}/cities1000.zip")) as z, \
            z.open("cities1000.txt") as f:
        for raw in io.TextIOWrapper(f, encoding="utf-8"):
            c = raw.rstrip("\n").split("\t")
            if len(c) < 17:
                continue
            lat, lon = float(c[4]), float(c[5])
            cell = (math.floor(lat), math.floor(lon))
            near = any(km(lat, lon, a, b) <= NEAR_KM for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                       for a, b in grid.get((cell[0] + dy, cell[1] + dx), ()))
            if not near:
                continue
            state = admin1.get(f"{c[8]}.{c[10]}", "")
            elev = c[15] or (c[16] if c[16] not in ("", "-9999") else "")
            out.append(Place(c[1], ", ".join(x for x in (c[1], state, c[8]) if x), lat, lon,
                             float(elev) if elev else None, int(c[14] or 0)))
    return out


def write_towns(folder: Path, towns: list[Place]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["name", "label", "lat", "lon", "elev_m", "population"])
    for t in sorted(towns, key=lambda t: (-t.population, t.label)):
        w.writerow([t.name, t.label, f"{t.lat:.4f}", f"{t.lon:.4f}", "" if t.elev_m is None else f"{t.elev_m:.0f}",
                    t.population])
    (folder / TOWNS).write_text(out.getvalue(), encoding="utf-8")


# ── the table ────────────────────────────────────────────────────────────────

def row_of(s: Station, st: Stats) -> dict[str, str]:
    return {"id": s.id, "name": s.name, "country": s.country, "state": s.state, "lat": f"{s.lat:.4f}",
            "lon": f"{s.lon:.4f}", "elev_m": "" if s.elev_m is None else f"{s.elev_m:.0f}", "years": str(st.years),
            "last_frost": st.last_frost, "last_frost_late": st.last_frost_late, "first_frost": st.first_frost,
            "first_frost_early": st.first_frost_early, "frost_free_days": str(st.frost_free_days),
            "frost_years": f"{st.frost_years:.2f}", "coldest_c": "" if st.coldest_c is None else f"{st.coldest_c:.1f}",
            "zone": st.zone}


def write_table(folder: Path, rows: list[dict[str, str]], about: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    w = csv.DictWriter(out, COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(sorted(rows, key=lambda r: r["id"]))
    path = folder / STATIONS
    tmp = path.with_suffix(".part")
    tmp.write_text(out.getvalue(), encoding="utf-8")
    tmp.replace(path)
    (folder / ABOUT).write_text(json.dumps(about, indent=2) + "\n", encoding="utf-8")
    return path


def read_table(folder: Path) -> list[dict[str, str]]:
    path = folder / STATIONS
    if not path.is_file():
        raise ClimateError("There is no climate table in the library yet: it is built once, with the internet "
                           "(museion climate build).")
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def about(folder: Path) -> dict:
    try:
        return json.loads((folder / ABOUT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ── looking a place up ───────────────────────────────────────────────────────

def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class Near:
    row: dict[str, str]
    km: float
    weight: float


@dataclass
class Estimate:
    lat: float
    lon: float
    elev_m: float | None
    last_frost: str
    last_frost_late: str
    first_frost: str
    first_frost_early: str
    frost_free_days: int | None
    coldest_c: float | None
    zone: str
    stations: list[Near]
    far: bool  # the nearest station is further than NEAR_KM

    def as_dict(self) -> dict:
        return {"lat": self.lat, "lon": self.lon, "elev_m": self.elev_m, "last_frost": self.last_frost,
                "last_frost_late": self.last_frost_late, "first_frost": self.first_frost,
                "first_frost_early": self.first_frost_early, "frost_free_days": self.frost_free_days,
                "coldest_c": self.coldest_c, "zone": self.zone, "far": self.far,
                "stations": [{"name": n.row["name"], "id": n.row["id"], "km": round(n.km, 1),
                              "elev_m": n.row["elev_m"], "years": n.row["years"]} for n in self.stations]}


def estimate(table: list[dict[str, str]], lat: float, lon: float, elev_m: float | None = None,
             count: int = 3) -> Estimate:
    """The frost dates and zone at a point, from the nearest stations: each
    weighs 1 / distance², where 100 m of height difference (when the place's
    height is known) counts like 10 km."""
    if not table:
        raise ClimateError("The climate table has no stations.")
    scored = []
    for r in table:
        d = km(lat, lon, float(r["lat"]), float(r["lon"]))
        if elev_m is not None and r["elev_m"]:
            d_eff = math.hypot(d, abs(elev_m - float(r["elev_m"])) * HEIGHT_KM_PER_M)
        else:
            d_eff = d
        scored.append((d_eff, d, r))
    scored.sort(key=lambda t: t[0])
    near = [t for t in scored[:count] if t[0] <= NEAR_KM] or scored[:1]
    picked = [Near(r, d, 1 / max(d_eff, 1.0) ** 2) for d_eff, d, r in near]
    total = sum(n.weight for n in picked)
    for n in picked:
        n.weight /= total

    def date(column: str, south: bool) -> str:
        have = [(n, n.row[column]) for n in picked]
        frost_weight = sum(n.weight for n, v in have if v)
        if frost_weight < 0.5:
            return ""
        days = [(n.weight, _season_day(v, south)) for n, v in have if v]
        mean = sum(w * d for w, d in days) / sum(w for w, _ in days)
        return month_day(south, round(mean))

    south = lat < 0
    frost_free = [(n.weight, int(n.row["frost_free_days"])) for n in picked if n.row["frost_free_days"]]
    coldest = [(n.weight, float(n.row["coldest_c"])) for n in picked if n.row["coldest_c"]]
    cold = round(sum(w * c for w, c in coldest) / sum(w for w, _ in coldest), 1) if coldest else None
    return Estimate(lat, lon, elev_m, date("last_frost", south), date("last_frost_late", south),
                    date("first_frost", south), date("first_frost_early", south),
                    round(sum(w * f for w, f in frost_free) / sum(w for w, _ in frost_free)) if frost_free else None,
                    cold, zone(cold) if cold is not None else "", picked, near[0][0] > NEAR_KM)


def _season_day(md: str, south: bool) -> int:
    month, day = int(md[:2]), int(md[3:5])
    d = (datetime.date(2001, month, day) - datetime.date(2001, 7 if south else 1, 1)).days
    return d % 365


# ── places ───────────────────────────────────────────────────────────────────

@dataclass
class Place:
    name: str
    label: str
    lat: float
    lon: float
    elev_m: float | None = None
    population: int = 0


def read_towns(folder: Path) -> list[Place]:
    path = folder / TOWNS
    if not path.is_file():
        return []
    out = []
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                out.append(Place(r["name"], r.get("label") or r["name"], float(r["lat"]), float(r["lon"]),
                                 float(r["elev_m"]) if r.get("elev_m") else None, int(r.get("population") or 0)))
            except (KeyError, ValueError):
                continue
    return out


def find_place(text: str, folder: Path, saved: list[dict] = ()) -> list[Place]:
    """Places matching what the person typed, best first, with no internet:
    "lat, lon", their saved places (places.csv), or the region's towns."""
    from museion.osm import parse_coords
    text = text.strip()
    if not text:
        raise ClimateError("Type a town, or a latitude and longitude like 47.61, -122.33.")
    point = parse_coords(text)
    if point:
        return [Place(f"{point[0]:.4f}, {point[1]:.4f}", "The point you typed", *point)]
    want = _plain(text.split(",")[0])
    rest = _plain(" ".join(text.split(",")[1:]))
    found: list[tuple[int, int, Place]] = []
    for p in saved:
        if _plain(p.get("name", "")) == want:
            found.append((0, 0, Place(p["name"], "Your saved place", p["lat"], p["lon"])))
    for p in read_towns(folder):
        name = _plain(p.name)
        if name == want or name.startswith(want + " "):
            if rest and rest not in _plain(p.label):
                continue
            found.append((1 if name == want else 2, -p.population, p))
    found.sort(key=lambda t: (t[0], t[1]))
    return [p for _, _, p in found[:8]]


def _plain(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(text.replace("-", " ").replace(".", " ").split())


def describe(e: Estimate, place: str = "") -> str:
    """The estimate in words, for the page and the agents."""
    where = f" for {place}" if place else ""
    parts = []
    if e.last_frost:
        parts.append(f"last spring frost about {e.last_frost} (1 year in 10 as late as {e.last_frost_late})")
    else:
        parts.append("no spring frost in most years")
    if e.first_frost:
        parts.append(f"first autumn frost about {e.first_frost} (1 year in 10 as early as {e.first_frost_early})")
    else:
        parts.append("no autumn frost in most years")
    if e.frost_free_days is not None and e.last_frost and e.first_frost:
        parts.append(f"about {e.frost_free_days} frost-free days")
    if e.zone:
        parts.append(f"hardiness zone {e.zone} (average coldest night {e.coldest_c} °C)")
    stations = "; ".join(f"{n.row['name'].title()} {n.km:.0f} km"
                         + (f", {float(n.row['elev_m']):.0f} m up" if n.row["elev_m"] else "")
                         for n in e.stations)
    text = f"Station averages{where}: " + "; ".join(parts) + f". From {len(e.stations)} weather station" \
        + ("s" if len(e.stations) != 1 else "") + f" ({stations}), 1991-2020, NOAA GHCN-Daily."
    if e.far:
        text += f" The nearest station is over {NEAR_KM} km away, so this is only a rough guide."
    text += (" Averages: a frost pocket or a sheltered garden can be a week or two either side, and the "
             "zone is worked out the USDA's way from station data, not the official USDA map.")
    return text


def look_up(library_dir: Path, text: str, saved: list[dict] = (), pick: int = 0) -> tuple[list[Place], Place, Estimate]:
    """The places matching `text`, the one picked, and its estimate."""
    where = Path(library_dir) / FOLDER
    table = read_table(where)
    found = find_place(text, where, saved)
    if not found:
        raise ClimateError(f"No town called {text.strip()!r} in the climate table's region "
                           f"({about(where).get('region', 'unknown')}). Try a bigger town nearby, "
                           "or a latitude and longitude like 47.61, -122.33.")
    chosen = found[min(max(pick, 0), len(found) - 1)]
    e = estimate(table, chosen.lat, chosen.lon, chosen.elev_m)
    if e.stations[0].km > FAR_KM:
        raise ClimateError(f"{chosen.label} is {e.stations[0].km:,.0f} km from the nearest station in the climate "
                           f"table, which covers {about(where).get('region', 'another region')}: a table for that "
                           "region would be needed (museion climate build).")
    return found, chosen, e
