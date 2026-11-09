"""Making a map: OpenStreetMap data for an area (osm.py) turned into a
vector .mbtiles file in the library's maps folder, which the Map tab draws
(ui/mapdraw.js) and other map programs open too.

The map holds vector tiles (mvt.py) from zoom MINZOOM to MAXZOOM; the page
draws them sharp up to four levels closer than that. The layers and their
`class` values follow the OpenMapTiles schema (https://openmaptiles.org/schema/),
so one way of drawing serves both the maps made here and OpenMapTiles maps
made elsewhere:

    water        lakes, rivers, reservoirs (areas)
    waterway     river, canal, stream, ditch, coastline (lines)
    landcover    wood, grass, farmland, wetland, scrub, sand, rock, ice
    landuse      residential, industrial, commercial, retail, cemetery, ...
    park         park, nature_reserve, pitch, ...
    building     every building, from zoom 13
    water_name   the names of lakes and ponds (points)
    transportation  motorway ... minor, service, track, path, rail; name, ref
    place        city, town, village, suburb, hamlet ...; name, rank
    poi          the places that matter when help is far away: hospital,
                 clinic, pharmacy, drinking_water, fuel, police, shelter, ...

Each tile is made by projecting every feature to Web Mercator, leaving out
what is too small to see at that zoom, simplifying lines to half a pixel
(Douglas-Peucker), and cutting what crosses the tile's edge to the tile and
a small margin. A district takes a minute or two.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from museion import mvt, osm

MINZOOM, MAXZOOM = 8, 14
EXTENT = mvt.EXTENT
BUFFER = 64  # tile units past each edge: 4 pixels
SIMPLIFY = 8.0  # tile units: half a pixel
MIN_AREA = 4 * 256.0  # a polygon smaller than four pixels at a zoom is left out of it
ATTRIBUTION = "© OpenStreetMap contributors"
WORLD = EXTENT * 2 ** MAXZOOM  # the whole world, in tile units at MAXZOOM
LAYERS = ["water", "landcover", "landuse", "park", "waterway", "building", "transportation", "water_name", "place",
          "poi"]

Point = tuple[float, float]


@dataclass
class Shape:
    layer: str
    type: int  # mvt.POINT, LINESTRING or POLYGON
    minzoom: int
    properties: dict
    # In tile units at MAXZOOM. POINT: [[p]]. LINESTRING: [line]. POLYGON:
    # polygons, each [outer ring, holes...], rings closed.
    geometry: list = field(default_factory=list)


def project(lon: float, lat: float) -> Point:
    lat = max(min(lat, 85.0511), -85.0511)
    x = (lon + 180) / 360 * WORLD
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * WORLD
    return x, y


def unproject(x: float, y: float) -> tuple[float, float]:
    lon = x / WORLD * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / WORLD))))
    return lon, lat


# ── what each OpenStreetMap element becomes ──────────────────────────────────

ROADS = {
    "motorway": ("motorway", 8), "motorway_link": ("motorway", 10), "trunk": ("trunk", 8),
    "trunk_link": ("trunk", 10), "primary": ("primary", 9), "primary_link": ("primary", 11),
    "secondary": ("secondary", 10), "secondary_link": ("secondary", 12), "tertiary": ("tertiary", 11),
    "tertiary_link": ("tertiary", 12), "unclassified": ("minor", 12), "residential": ("minor", 12),
    "living_street": ("minor", 13), "road": ("minor", 13), "pedestrian": ("path", 13),
    "service": ("service", 13), "track": ("track", 13), "footway": ("path", 14), "path": ("path", 14),
    "cycleway": ("path", 14), "bridleway": ("path", 14), "steps": ("path", 14),
}
WATERWAYS = {"river": 9, "canal": 10, "stream": 12, "brook": 12, "ditch": 13, "drain": 13}
LANDCOVER = {
    ("natural", "wood"): "wood", ("landuse", "forest"): "wood", ("natural", "wetland"): "wetland",
    ("natural", "scrub"): "scrub", ("natural", "heath"): "scrub", ("natural", "grassland"): "grass",
    ("landuse", "grass"): "grass", ("landuse", "meadow"): "grass", ("landuse", "village_green"): "grass",
    ("landuse", "farmland"): "farmland", ("landuse", "orchard"): "farmland", ("landuse", "vineyard"): "farmland",
    ("landuse", "allotments"): "farmland", ("landuse", "greenhouse_horticulture"): "farmland",
    ("landuse", "farmyard"): "farmland", ("natural", "beach"): "sand", ("natural", "sand"): "sand",
    ("natural", "bare_rock"): "rock", ("natural", "scree"): "rock", ("natural", "glacier"): "ice",
}
LANDUSE = {"residential": "residential", "industrial": "industrial", "commercial": "commercial",
           "retail": "retail", "cemetery": "cemetery", "military": "military", "railway": "railway",
           "quarry": "quarry", "construction": "construction", "brownfield": "industrial"}
PARKS = {"park": "park", "garden": "park", "common": "park", "golf_course": "park", "nature_reserve": "nature_reserve",
         "pitch": "pitch", "playground": "pitch"}
PLACES = {"city": (8, 1), "town": (9, 2), "village": (11, 3), "suburb": (12, 4), "quarter": (13, 5),
          "hamlet": (12, 5), "neighbourhood": (13, 6), "locality": (14, 7), "isolated_dwelling": (14, 7),
          "farm": (14, 7)}
# The POI class each tag gives, and the zoom it appears from.
POIS = [
    ("amenity", "hospital", "hospital", 11), ("amenity", "clinic", "clinic", 13), ("amenity", "doctors", "clinic", 13),
    ("healthcare", "hospital", "hospital", 11), ("healthcare", "pharmacy", "pharmacy", 13),
    ("healthcare", "*", "clinic", 13), ("amenity", "dentist", "clinic", 14),
    ("amenity", "pharmacy", "pharmacy", 13), ("shop", "chemist", "pharmacy", 14),
    ("amenity", "drinking_water", "drinking_water", 13), ("amenity", "water_point", "drinking_water", 13),
    ("man_made", "water_well", "drinking_water", 13), ("man_made", "water_tap", "drinking_water", 13),
    ("natural", "spring", "drinking_water", 13), ("emergency", "water_tank", "water_tank", 14),
    ("man_made", "water_tower", "water_tower", 14), ("amenity", "fuel", "fuel", 13),
    ("amenity", "police", "police", 13), ("amenity", "fire_station", "fire_station", 13),
    ("amenity", "shelter", "shelter", 14), ("emergency", "assembly_point", "shelter", 14),
    ("tourism", "alpine_hut", "shelter", 14), ("tourism", "wilderness_hut", "shelter", 14),
    ("tourism", "camp_site", "campsite", 14), ("emergency", "defibrillator", "defibrillator", 14),
    ("emergency", "phone", "emergency_phone", 14), ("emergency", "fire_hydrant", "fire_hydrant", 14),
    ("amenity", "school", "school", 14), ("amenity", "college", "school", 14), ("amenity", "university", "school", 13),
    ("amenity", "townhall", "town_hall", 13), ("amenity", "community_centre", "community_centre", 14),
    ("amenity", "place_of_worship", "place_of_worship", 14), ("amenity", "marketplace", "food", 14),
    ("shop", "supermarket", "food", 14), ("shop", "convenience", "food", 14), ("shop", "greengrocer", "food", 14),
    ("shop", "butcher", "food", 14), ("shop", "bakery", "food", 14), ("shop", "farm", "food", 14),
    ("shop", "hardware", "hardware", 14), ("shop", "doityourself", "hardware", 14),
    ("amenity", "veterinary", "veterinary", 14), ("amenity", "toilets", "toilets", 14),
    ("amenity", "bank", "bank", 14), ("amenity", "post_office", "post_office", 14),
    ("amenity", "library", "library", 14), ("man_made", "watermill", "mill", 14), ("man_made", "windmill", "mill", 14),
    ("natural", "peak", "peak", 12),
]


def poi_of(tags: dict) -> tuple[str, int] | None:
    for key, value, cls, z in POIS:
        if key in tags and (value == "*" or tags[key] == value):
            return cls, z
    return None


def area_zoom(rings: list[list[Point]], floor: int = MINZOOM) -> int:
    """The first zoom at which a polygon is big enough to see; MAXZOOM for
    any smaller, which is still kept there for the closer zooms."""
    area = abs(shoelace(rings[0])) if rings and len(rings[0]) > 2 else 0.0
    for z in range(floor, MAXZOOM):
        if area * 4.0 ** (z - MAXZOOM) >= MIN_AREA:
            return z
    return MAXZOOM


def shoelace(ring: list[Point]) -> float:
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return total / 2


def polys_of(e: dict) -> list[list[list[Point]]]:
    return [[[project(*p) for p in ring] for ring in poly] for poly in osm.polygons(e)]


def line_of(e: dict) -> list[Point]:
    return [project(*p) for p in osm.way_points(e)]


def shapes_of(e: dict) -> list[Shape]:
    """What one element is drawn as: nothing, or one or more shapes (a
    supermarket building is a building and a poi)."""
    tags = e.get("tags") or {}
    if not tags:
        return []
    out: list[Shape] = []
    name = tags.get("name", "")
    kind = e.get("type")
    is_area = kind == "relation" or (kind == "way" and e.get("geometry") and len(e["geometry"]) >= 4
                                     and e["geometry"][0] == e["geometry"][-1])

    def area(layer: str, props: dict, floor: int = MINZOOM) -> None:
        for poly in polys_of(e):
            out.append(Shape(layer, mvt.POLYGON, area_zoom(poly, floor), props, [poly]))

    if kind != "node":
        hw = tags.get("highway")
        if hw in ROADS and tags.get("area") != "yes" and kind == "way":
            cls, z = ROADS[hw]
            out.append(Shape("transportation", mvt.LINESTRING, z,
                             {"class": cls, "name": name, "ref": tags.get("ref", "")}, [line_of(e)]))
        rw = tags.get("railway")
        if rw in ("rail", "light_rail", "narrow_gauge", "subway", "tram", "monorail") and kind == "way":
            out.append(Shape("transportation", mvt.LINESTRING, 10 if rw == "rail" else 12,
                             {"class": "rail" if rw in ("rail", "narrow_gauge") else "transit", "name": name},
                             [line_of(e)]))
        ww = tags.get("waterway")
        if ww in WATERWAYS and kind == "way":
            out.append(Shape("waterway", mvt.LINESTRING, WATERWAYS[ww],
                             {"class": "stream" if ww == "brook" else ww, "name": name}, [line_of(e)]))
        if tags.get("natural") == "coastline" and kind == "way":
            out.append(Shape("waterway", mvt.LINESTRING, MINZOOM, {"class": "coastline"}, [line_of(e)]))
        if is_area:
            if tags.get("natural") == "water" or ww == "riverbank" or tags.get("landuse") in ("reservoir", "basin") \
                    or (tags.get("water") and tags.get("natural") in (None, "water")):
                area("water", {"class": tags.get("water", "lake"), "name": name})
                at = osm.centre_of(e)
                if name and at and out:
                    z = min(max(out[-1].minzoom + 2, 12), MAXZOOM)
                    out.append(Shape("water_name", mvt.POINT, z, {"class": "lake", "name": name}, [[project(*at)]]))
            cover = next((c for (k, v), c in LANDCOVER.items() if tags.get(k) == v), None)
            if cover:
                area("landcover", {"class": cover})
            if tags.get("landuse") in LANDUSE:
                area("landuse", {"class": LANDUSE[tags["landuse"]]})
            elif tags.get("amenity") == "grave_yard":
                area("landuse", {"class": "cemetery"})
            elif tags.get("amenity") in ("hospital", "school", "university", "college"):
                area("landuse", {"class": "hospital" if tags["amenity"] == "hospital" else "school"}, 12)
            if tags.get("leisure") in PARKS:
                area("park", {"class": PARKS[tags["leisure"]], "name": name})
            if tags.get("building") and tags["building"] != "no" and kind == "way":
                area("building", {}, 13)
    if kind == "node" and tags.get("place") in PLACES and name:
        z, rank = PLACES[tags["place"]]
        out.append(Shape("place", mvt.POINT, z, {"class": tags["place"], "name": name, "rank": rank},
                         [[project(e["lon"], e["lat"])]]))
    poi = poi_of(tags)
    if poi:
        at = osm.centre_of(e)
        if at:
            props = {"class": poi[0], "name": name}
            if poi[0] == "peak" and tags.get("ele"):
                props["ele"] = tags["ele"]
            out.append(Shape("poi", mvt.POINT, poi[1], props, [[project(*at)]]))
    return out


# ── cutting into tiles ───────────────────────────────────────────────────────

def simplify(points: list[Point], eps: float) -> list[Point]:
    """Douglas-Peucker: the points that matter to within eps."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    eps2 = eps * eps
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        worst, at = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            if length2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / length2
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > worst:
                worst, at = d, i
        if worst > eps2:
            keep[at] = True
            stack.append((a, at))
            stack.append((at, b))
    return [p for p, k in zip(points, keep) if k]


def clip_line(points: list[Point], x0: float, y0: float, x1: float, y1: float) -> list[list[Point]]:
    """The parts of a line inside a box (Liang-Barsky on each segment)."""
    out: list[list[Point]] = []
    current: list[Point] = []
    for i in range(len(points) - 1):
        (ax, ay), (bx, by) = points[i], points[i + 1]
        t0, t1 = 0.0, 1.0
        dx, dy = bx - ax, by - ay
        ok = True
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                r = q / p
                if p < 0:
                    if r > t1:
                        ok = False
                        break
                    t0 = max(t0, r)
                else:
                    if r < t0:
                        ok = False
                        break
                    t1 = min(t1, r)
        if not ok:
            if current:
                out.append(current)
                current = []
            continue
        start = (ax + t0 * dx, ay + t0 * dy)
        end = (ax + t1 * dx, ay + t1 * dy)
        if not current:
            current = [start]
        elif current[-1] != start:
            out.append(current)
            current = [start]
        current.append(end)
        if t1 < 1.0:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return [part for part in out if len(part) >= 2]


def _inside(p: Point, edge: int, box: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = box
    return (p[0] >= x0, p[0] <= x1, p[1] >= y0, p[1] <= y1)[edge]


def _cross(a: Point, b: Point, edge: int, box: tuple[float, float, float, float]) -> Point:
    """Where the line from a to b crosses one side of the box."""
    (ax, ay), (bx, by) = a, b
    if edge < 2:
        x = box[0] if edge == 0 else box[2]
        return x, ay + (by - ay) * (x - ax) / (bx - ax)
    y = box[1] if edge == 2 else box[3]
    return ax + (bx - ax) * (y - ay) / (by - ay), y


def clip_ring(ring: list[Point], x0: float, y0: float, x1: float, y1: float) -> list[Point]:
    """A ring cut to a box (Sutherland-Hodgman), open: no repeated end."""
    box = (x0, y0, x1, y1)
    pts = ring[:-1] if ring and ring[0] == ring[-1] else ring
    for edge in range(4):
        if not pts:
            break
        out = []
        prev = pts[-1]
        for p in pts:
            if _inside(p, edge, box):
                if not _inside(prev, edge, box):
                    out.append(_cross(prev, p, edge, box))
                out.append(p)
            elif _inside(prev, edge, box):
                out.append(_cross(prev, p, edge, box))
            prev = p
        pts = out
    return pts


def to_tile(points: Iterable[Point], ox: float, oy: float) -> mvt.Coords:
    out: mvt.Coords = []
    for x, y in points:
        q = (round(x - ox), round(y - oy))
        if not out or out[-1] != q:
            out.append(q)
    return out


def tile_range(points: Iterable[Point], z: int) -> tuple[int, int, int, int]:
    xs, ys = zip(*points)
    n = 2 ** z
    pad = BUFFER
    return (max(int((min(xs) - pad) // EXTENT), 0), max(int((min(ys) - pad) // EXTENT), 0),
            min(int((max(xs) + pad) // EXTENT), n - 1), min(int((max(ys) + pad) // EXTENT), n - 1))


class Tiles:
    """The features of every tile being made: (z, x, y) -> layer -> features."""

    def __init__(self) -> None:
        self.tiles: dict[tuple[int, int, int], dict[str, list[mvt.Feature]]] = {}

    def add(self, z: int, x: int, y: int, layer: str, feature: mvt.Feature) -> None:
        self.tiles.setdefault((z, x, y), {}).setdefault(layer, []).append(feature)

    def shape(self, s: Shape, z: int) -> None:
        f = 2.0 ** (z - MAXZOOM)
        if s.type == mvt.POINT:
            x, y = s.geometry[0][0]
            x, y = x * f, y * f
            tx, ty = int(x // EXTENT), int(y // EXTENT)
            self.add(z, tx, ty, s.layer, mvt.Feature(mvt.POINT, [[(round(x - tx * EXTENT), round(y - ty * EXTENT))]],
                                                     s.properties))
            return
        if s.type == mvt.LINESTRING:
            line = simplify([(x * f, y * f) for x, y in s.geometry[0]], SIMPLIFY)
            if len(line) < 2:
                return
            x0, y0, x1, y1 = tile_range(line, z)
            for tx in range(x0, x1 + 1):
                for ty in range(y0, y1 + 1):
                    ox, oy = tx * EXTENT, ty * EXTENT
                    parts = [to_tile(p, ox, oy) for p in clip_line(line, ox - BUFFER, oy - BUFFER,
                                                                    ox + EXTENT + BUFFER, oy + EXTENT + BUFFER)]
                    parts = [p for p in parts if len(p) >= 2]
                    if parts:
                        self.add(z, tx, ty, s.layer, mvt.Feature(mvt.LINESTRING, parts, s.properties))
            return
        for poly in s.geometry:
            rings = [simplify([(x * f, y * f) for x, y in ring], SIMPLIFY) for ring in poly]
            if len(rings[0]) < 4:
                continue
            x0, y0, x1, y1 = tile_range(rings[0], z)
            for tx in range(x0, x1 + 1):
                for ty in range(y0, y1 + 1):
                    ox, oy = tx * EXTENT, ty * EXTENT
                    box = (ox - BUFFER, oy - BUFFER, ox + EXTENT + BUFFER, oy + EXTENT + BUFFER)
                    parts = []
                    for i, ring in enumerate(rings):
                        cut = to_tile(clip_ring(ring, *box), ox, oy)
                        if len(cut) > 1 and cut[0] == cut[-1]:
                            cut.pop()
                        if len(cut) < 3 or mvt.ring_area(cut) == 0:
                            if i == 0:
                                break
                            continue
                        # Outer rings clockwise on screen, holes the other way.
                        if (mvt.ring_area(cut) > 0) != (i == 0):
                            cut.reverse()
                        parts.append(cut)
                    if parts:
                        self.add(z, tx, ty, s.layer, mvt.Feature(mvt.POLYGON, parts, s.properties))


# ── the whole map ────────────────────────────────────────────────────────────

Report = Callable[[str, int, int], None]


def build(elements: Iterable[dict], box: osm.Box, out: Path, name: str,
          report: Report = lambda step, done, total: None, stop: threading.Event | None = None) -> Path:
    """Write the map of these elements into `out` (an .mbtiles file)."""
    shapes: list[Shape] = []
    for i, e in enumerate(elements):
        shapes += shapes_of(e)
        if i % 5000 == 0:
            report("reading", i, 0)
    if not shapes:
        raise osm.OsmError("The download holds nothing to draw: is the area in the sea, or was it cut short?")
    tiles = Tiles()
    zooms = list(range(MINZOOM, MAXZOOM + 1))
    for n, z in enumerate(zooms):
        if stop is not None and stop.is_set():
            raise osm.OsmError("Stopped. Make the map again to go on: the download is kept.")
        report("drawing", n, len(zooms))
        for s in shapes:
            if s.minzoom <= z:
                tiles.shape(s, z)
    report("drawing", len(zooms), len(zooms))
    return write(tiles, box, out, name, report)


def write(tiles: Tiles, box: osm.Box, out: Path, name: str, report: Report) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    part.unlink(missing_ok=True)
    conn = sqlite3.connect(part)
    try:
        conn.executescript("""
            CREATE TABLE metadata (name TEXT, value TEXT);
            CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
            CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
        """)
        fields = {"class": "String", "name": "String"}
        meta = {
            "name": name, "format": "pbf", "type": "baselayer", "version": "1",
            "description": f"Made by Museion from OpenStreetMap data, {time.strftime('%Y-%m-%d')}",
            "attribution": ATTRIBUTION, "minzoom": str(MINZOOM), "maxzoom": str(MAXZOOM),
            "bounds": f"{box.west},{box.south},{box.east},{box.north}",
            "center": f"{(box.west + box.east) / 2},{(box.south + box.north) / 2},{MAXZOOM - 2}",
            "json": json.dumps({"vector_layers": [{"id": layer, "fields": fields, "minzoom": MINZOOM,
                                                   "maxzoom": MAXZOOM} for layer in LAYERS],
                                "museion": {"schema": "openmaptiles"}}),
        }
        conn.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
        total = len(tiles.tiles)
        for i, ((z, x, y), layers) in enumerate(sorted(tiles.tiles.items())):
            ordered = {layer: layers[layer] for layer in LAYERS if layer in layers}
            data = gzip.compress(mvt.encode_tile(ordered), compresslevel=6, mtime=0)
            conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, x, (1 << z) - 1 - y, data))
            if i % 500 == 0:
                report("saving", i, total)
        conn.commit()
    finally:
        conn.close()
    from museion import maps
    maps.replace(part, out)  # the page may have the old one open
    report("saving", 1, 1)
    return out


def map_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "map"


def make(name: str, box: osm.Box, maps_dir: Path, buildings: bool = True,
         report: Report = lambda step, done, total: None, stop: threading.Event | None = None) -> Path:
    """Download the area's data (or go on with a download that was stopped)
    and make its map: <maps_dir>/<id>.mbtiles, the data kept in
    <maps_dir>/sources/<id>/ to make it again offline."""
    mid = map_id(name)
    source = maps_dir / "sources" / mid

    def fetched(done: int, total: int, size: int) -> None:
        report("downloading", done, total)
        report("bytes", size, 0)

    osm.fetch(box, source, buildings, fetched, stop, name)
    return build(osm.elements(source), box, maps_dir / f"{mid}.mbtiles", name, report, stop)


def remake(source: Path, maps_dir: Path, name: str | None = None,
           report: Report = lambda step, done, total: None) -> Path:
    """Make a map again from data downloaded before, with no internet. With a
    new name it is a new map file beside the old one."""
    area = osm.read_area(source)
    box = osm.Box(*area["box"])
    out = maps_dir / f"{map_id(name) if name else source.name}.mbtiles"
    return build(osm.elements(source), box, out, name or area.get("name") or source.name, report)
