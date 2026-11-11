"""OpenStreetMap data for the maps Museion makes: finding a place by name,
and downloading what is mapped in an area -- roads, water, woods, buildings,
place names, and the places that matter in an emergency (water points,
pharmacies, clinics, fuel, shelters).

Both need the internet, once: the data is kept in the library
(<library>/maps/sources/<map>/), so the map can be made again from it with
none. mapmaker.py turns it into a map.

- Finding a place: Nominatim, OpenStreetMap's own search
  (https://nominatim.org/release-docs/latest/api/Search/). Its rules: say
  who is asking (USER_AGENT), at most one search a second, only when the
  person presses Find.
- The data: the Overpass API (https://wiki.openstreetmap.org/wiki/Overpass_API),
  which answers a query for an area with its elements and their geometry
  as JSON. A big area is asked for in pieces (CELL degrees square), one
  after another, so no single answer is too large for the server; each
  piece is saved as it arrives, so a stopped download goes on from the
  next one.

OpenStreetMap data is © OpenStreetMap contributors, under the Open Database
Licence: a map made from it must say so (mapmaker.ATTRIBUTION).
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "Museion (offline library; https://github.com/SeyelentEco/museion)"
NOMINATIM = os.environ.get("MUSEION_NOMINATIM", "https://nominatim.openstreetmap.org/search")
# The main server first, then a mirror run by others, for when it is busy.
OVERPASS = [u for u in os.environ.get("MUSEION_OVERPASS", "").split() if u] or [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
CELL = 0.1  # degrees: about 11 km north to south
TIMEOUT = 180  # seconds the server may take over one piece
PAUSE = 1.0  # between pieces, to be a polite guest
SIZES_KM = [5, 10, 25, 50]
MAX_KM = 50


class OsmError(RuntimeError):
    """A problem worded for the person."""


@dataclass
class Box:
    south: float
    west: float
    north: float
    east: float

    def as_list(self) -> list[float]:
        return [self.south, self.west, self.north, self.east]

    def km(self) -> tuple[float, float]:
        """Its width and height, east to west and north to south."""
        mid = math.radians((self.south + self.north) / 2)
        return (self.east - self.west) * 111.32 * math.cos(mid), (self.north - self.south) * 110.57


def box_around(lat: float, lon: float, km: float) -> Box:
    """A square km across, centred on a point."""
    if not (-85 <= lat <= 85 and -180 <= lon <= 180):
        raise OsmError("That position is not on the map.")
    if not 0 < km <= MAX_KM:
        raise OsmError(f"Choose an area up to {MAX_KM} km across.")
    dlat = km / 2 / 110.57
    dlon = km / 2 / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return Box(round(lat - dlat, 6), round(max(lon - dlon, -180), 6),
               round(lat + dlat, 6), round(min(lon + dlon, 180), 6))


def cells(box: Box, step: float = CELL) -> list[Box]:
    """The pieces an area is downloaded in, west to east, south to north."""
    rows = max(math.ceil((box.north - box.south) / step - 1e-9), 1)
    cols = max(math.ceil((box.east - box.west) / step - 1e-9), 1)
    dy, dx = (box.north - box.south) / rows, (box.east - box.west) / cols
    return [Box(round(box.south + r * dy, 6), round(box.west + c * dx, 6),
                round(box.south + (r + 1) * dy, 6), round(box.west + (c + 1) * dx, 6))
            for r in range(rows) for c in range(cols)]


# ── finding a place ──────────────────────────────────────────────────────────

COORDS = re.compile(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*")
_last_search = 0.0
_search_lock = threading.Lock()


def parse_coords(text: str) -> tuple[float, float] | None:
    """ "51.5072, -0.1276" as latitude and longitude, or None."""
    m = COORDS.fullmatch(text)
    if not m:
        return None
    lat, lon = float(m[1]), float(m[2])
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def get_json(url: str, data: bytes | None = None, timeout: float = 30):
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def find_place(query: str) -> list[dict]:
    """Places matching a name ("Hebden Bridge", "Lewes, England"), best
    first: their name, a longer label, the point and the area they cover.
    Coordinates are taken as they are, with no search."""
    global _last_search
    query = query.strip()
    if not query:
        raise OsmError("Type a place: a town, a village, an address, or a latitude and longitude.")
    point = parse_coords(query)
    if point:
        lat, lon = point
        return [{"name": f"{lat:.4f}, {lon:.4f}", "label": "The point you typed", "lat": lat, "lon": lon,
                 "box": None}]
    with _search_lock:
        wait = _last_search + 1.0 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_search = time.monotonic()
        url = NOMINATIM + "?" + urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 6})
        try:
            found = get_json(url)
        except (OSError, ValueError) as e:
            raise OsmError(f"Could not search for the place (this needs the internet): {reason(e)}") from None
    out = []
    for r in found:
        try:
            s, n, w, e = (float(v) for v in r["boundingbox"])
            out.append({"name": r.get("name") or r["display_name"].split(",")[0], "label": r["display_name"],
                        "lat": float(r["lat"]), "lon": float(r["lon"]), "box": [s, w, n, e]})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def reason(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"the server answered {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)
    return str(e)


# ── the data ─────────────────────────────────────────────────────────────────

# What is asked for. Roads, rail, water, land cover and land use, buildings
# (when wanted), place names, and the points that matter when help is far
# away. Trees, benches and the like are left out: there are many of them and
# the map has no use for them.
POINTS = {
    "amenity": "hospital|clinic|doctors|pharmacy|drinking_water|water_point|fuel|police|fire_station|shelter"
               "|school|college|university|townhall|community_centre|marketplace|place_of_worship|toilets"
               "|dentist|veterinary|bank|post_office|library",
    "shop": "supermarket|convenience|greengrocer|butcher|bakery|hardware|doityourself|chemist|farm",
    "healthcare": ".",
    "emergency": "defibrillator|assembly_point|water_tank|fire_hydrant|phone",
    "man_made": "water_well|water_tap|water_tower|watermill|windmill",
    "natural": "spring|peak",
    "tourism": "camp_site|alpine_hut|wilderness_hut",
}
AREAS = {
    "natural": "water|wood|wetland|scrub|heath|grassland|beach|sand|bare_rock|scree|glacier|coastline",
    "landuse": ".",
    "leisure": "park|garden|nature_reserve|pitch|playground|common|golf_course",
    "waterway": ".",
    "water": ".",
}


def query(box: Box, buildings: bool = True) -> str:
    """The Overpass query for one piece of the area."""
    parts = ['way["highway"];', 'way["railway"~"^(rail|light_rail|narrow_gauge|subway|tram|monorail)$"];']
    for key, values in AREAS.items():
        parts.append(f'way["{key}"~"^({values})$"];' if values != "." else f'way["{key}"];')
        parts.append(f'relation["type"="multipolygon"]["{key}"' + (f'~"^({values})$"];' if values != "." else '];'))
    for key, values in POINTS.items():
        sel = f'["{key}"~"^({values})$"]' if values != "." else f'["{key}"]'
        parts.append(f"node{sel};")
        parts.append(f"way{sel};")
    parts.append('node["place"];')
    if buildings:
        parts.append('way["building"];')
    head = f"[out:json][timeout:{TIMEOUT}][maxsize:536870912][bbox:{box.south},{box.west},{box.north},{box.east}];"
    return head + "(" + "".join(parts) + ");out geom qt;"


def part_name(i: int) -> str:
    return f"part-{i:03d}.json.gz"


def fetch(box: Box, folder: Path, buildings: bool = True,
          report: Callable[[int, int, int], None] = lambda done, total, size: None,
          stop: threading.Event | None = None, name: str = "") -> list[Path]:
    """Download the area into `folder`, piece by piece; pieces already there
    (an earlier download that was stopped) are kept. report(pieces done,
    pieces, bytes so far)."""
    folder.mkdir(parents=True, exist_ok=True)
    pieces = cells(box)
    (folder / "area.json").write_text(json.dumps({
        "name": name, "box": box.as_list(), "buildings": buildings, "pieces": len(pieces),
        "fetched": time.strftime("%Y-%m-%d"), "source": "OpenStreetMap, by the Overpass API",
    }, indent=1), encoding="utf-8")
    paths = []
    size = 0
    for i, piece in enumerate(pieces):
        target = folder / part_name(i)
        if not target.is_file():
            if stop is not None and stop.is_set():
                raise OsmError("Stopped. What was downloaded is kept; make the map again to go on.")
            if i:
                time.sleep(PAUSE)
            data = overpass(query(piece, buildings), stop)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(data, compresslevel=6))
            os.replace(tmp, target)
        size += target.stat().st_size
        paths.append(target)
        report(i + 1, len(pieces), size)
    return paths


def overpass(q: str, stop: threading.Event | None = None) -> bytes:
    """One answer from Overpass, trying again when the server is busy (429,
    504) and then the next server. The answer's own "remark" says when the
    query itself failed (too big, timed out) even with a 200."""
    body = urllib.parse.urlencode({"data": q}).encode("utf-8")
    last = ""
    for attempt in range(6):
        url = OVERPASS[attempt % len(OVERPASS)]
        request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT + 30) as response:
                data = response.read()
            # The remark comes after the elements, at the end of the answer.
            ends = (data[:2000] + data[-4000:]).decode("utf-8", "replace")
            remark = re.search(r'"remark":\s*"([^"]*)"', ends)
            if remark and ("error" in remark[1].lower() or "timed out" in remark[1].lower()):
                raise OsmError(f"The map server could not answer: {remark[1]}. Try a smaller area, or without buildings.")
            json.loads(data)  # a whole answer, not one cut off
            return data
        except urllib.error.HTTPError as e:
            last = reason(e)
            if e.code not in (429, 502, 503, 504):
                raise OsmError(f"Could not download the map data: {last}.") from None
            wait = int(e.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
        except urllib.error.URLError as e:
            # No way through at all (no internet, no such server): one try
            # at each server is enough to say so.
            last = reason(e)
            if attempt + 1 >= len(OVERPASS) and not isinstance(e.reason, TimeoutError):
                break
            wait = 1 if attempt < 2 else 5 * (attempt + 1)
        except (OSError, ValueError) as e:
            last = reason(e)
            wait = 5 * (attempt + 1)
        if stop is not None and stop.wait(min(wait, 60)):
            raise OsmError("Stopped. What was downloaded is kept; make the map again to go on.")
        elif stop is None:
            time.sleep(min(wait, 60))
    raise OsmError(f"Could not download the map data (this needs the internet): {last}.")


def read_area(folder: Path) -> dict:
    try:
        return json.loads((folder / "area.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OsmError(f"{folder} holds no downloaded map data.") from None


def elements(folder: Path) -> Iterator[dict]:
    """Every element downloaded for the area, each once: a road that crosses
    from one piece into the next comes back in both."""
    seen: set[tuple[str, int]] = set()
    for path in sorted(folder.glob("part-*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        for e in data.get("elements", []):
            key = (e.get("type", ""), e.get("id", 0))
            if key in seen:
                continue
            seen.add(key)
            yield e


# ── geometry ─────────────────────────────────────────────────────────────────

LonLat = tuple[float, float]


def way_points(e: dict) -> list[LonLat]:
    return [(p["lon"], p["lat"]) for p in e.get("geometry") or [] if p]


def join_rings(pieces: list[list[LonLat]]) -> list[list[LonLat]]:
    """Closed rings from the ways of a multipolygon, which may each be only
    part of a ring: joined end to end. Pieces that never close are dropped."""
    rings = []
    open_ = [p for p in pieces if len(p) >= 2]
    while open_:
        ring = list(open_.pop())
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, p in enumerate(open_):
                if p[0] == ring[-1]:
                    ring += p[1:]
                elif p[-1] == ring[-1]:
                    ring += p[-2::-1]
                elif p[-1] == ring[0]:
                    ring = p[:-1] + ring
                elif p[0] == ring[0]:
                    ring = p[:0:-1] + ring
                else:
                    continue
                open_.pop(i)
                changed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def inside(point: LonLat, ring: list[LonLat]) -> bool:
    x, y = point
    hit = False
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i], ring[i + 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def polygons(e: dict) -> list[list[list[LonLat]]]:
    """An area's polygons, each an outer ring and its holes: a closed way,
    or a multipolygon relation's outer and inner ways joined into rings."""
    if e.get("type") == "way":
        pts = way_points(e)
        return [[pts]] if len(pts) >= 4 and pts[0] == pts[-1] else []
    members = [m for m in e.get("members", []) if m.get("type") == "way" and m.get("geometry")]
    outers = join_rings([way_points(m) for m in members if m.get("role") != "inner"])
    inners = join_rings([way_points(m) for m in members if m.get("role") == "inner"])
    out = [[o] for o in outers]
    for hole in inners:
        for poly in out:
            if inside(hole[0], poly[0]):
                poly.append(hole)
                break
    return out


def centre_of(e: dict) -> LonLat | None:
    """Where to put the name or the sign of a node or a small area."""
    if e.get("type") == "node":
        return (e["lon"], e["lat"]) if "lon" in e else None
    pts = way_points(e)
    if not pts and e.get("bounds"):
        b = e["bounds"]
        return ((b["minlon"] + b["maxlon"]) / 2, (b["minlat"] + b["maxlat"]) / 2)
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
