"""Making a map: what OpenStreetMap elements become, cutting them into
tiles, and the whole thing from a download, against a stand-in for the
Overpass and Nominatim servers (no internet)."""

import gzip
import http.server
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from museion import mapmaker, maps, mvt, osm
from map_fixture import LAT, LON, node, rect, town, way


class FakeOsm:
    """Overpass and Nominatim on a local port: every query gets the town."""

    def __init__(self):
        self.queries: list[str] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fake.queries.append(urllib.parse.parse_qs(body.decode())["data"][0])
                self.answer(json.dumps(town()).encode())

            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["q"][0]
                found = [{"name": "Millbrook", "display_name": f"Millbrook, East Sussex ({q})", "lat": str(LAT),
                          "lon": str(LON), "boundingbox": [str(LAT - 0.01), str(LAT + 0.01), str(LON - 0.02), str(LON + 0.02)]}]
                self.answer(json.dumps(found).encode())

            def answer(self, data):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ShapesTest(unittest.TestCase):
    def test_what_elements_become(self):
        def one(e):
            return [(s.layer, s.type, s.minzoom, s.properties.get("class")) for s in mapmaker.shapes_of(e)]

        self.assertEqual(one(way(1, [(0, 0), (500, 0)], {"highway": "primary", "name": "London Road"})),
                         [("transportation", mvt.LINESTRING, 9, "primary")])
        self.assertEqual(one(way(2, [(0, 0), (500, 0)], {"highway": "footway"})), [("transportation", mvt.LINESTRING, 14, "path")])
        self.assertEqual(one(node(3, 0, 0, {"amenity": "pharmacy", "name": "Boots"})), [("poi", mvt.POINT, 13, "pharmacy")])
        self.assertEqual(one(node(4, 0, 0, {"natural": "spring"})), [("poi", mvt.POINT, 13, "drinking_water")])
        self.assertEqual(one(node(5, 0, 0, {"amenity": "bench"})), [])
        self.assertEqual(one(node(6, 0, 0, {"place": "village"})), [], "a place with no name is not written")
        # A supermarket in a building is a building and a sign.
        shop = one(way(7, rect(0, 0, 40, 30), {"building": "retail", "shop": "supermarket", "name": "Co-op"}))
        self.assertEqual([(l, c) for l, _, _, c in shop], [("building", None), ("poi", "food")])
        # A big lake shows from further out than a pond.
        lake = one(way(8, rect(0, 0, 3000, 2000), {"natural": "water"}))[0]
        pond = one(way(9, rect(0, 0, 20, 20), {"natural": "water"}))[0]
        self.assertLess(lake[2], pond[2])
        self.assertEqual(pond[2], mapmaker.MAXZOOM, "small, but still kept for the closest zooms")

    def test_a_multipolygon_is_joined_into_rings_with_its_hole(self):
        wood = next(e for e in town()["elements"] if e["type"] == "relation")
        (poly,) = osm.polygons(wood)
        self.assertEqual(len(poly), 2)  # the outer ring from two pieces, and the clearing
        self.assertEqual(poly[0][0], poly[0][-1])
        self.assertEqual(len(poly[0]), 5)


class TilingTest(unittest.TestCase):
    def test_simplify_keeps_the_ends_and_the_corners(self):
        line = [(0, 0), (50, 0.5), (100, 0), (100, 100)]
        self.assertEqual(mapmaker.simplify(line, 8), [(0, 0), (100, 0), (100, 100)])

    def test_clipping(self):
        parts = mapmaker.clip_line([(-10, 5), (5, 5), (5, 20), (20, 20), (20, 5), (30, 5)], 0, 0, 10, 10)
        self.assertEqual(parts, [[(0.0, 5.0), (5, 5), (5.0, 10.0)]])
        ring = mapmaker.clip_ring([(-5, -5), (15, -5), (15, 15), (-5, 15), (-5, -5)], 0, 0, 10, 10)
        self.assertEqual(sorted(ring), [(0, 0), (0, 10), (10, 0), (10, 10)])  # covers the whole box


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tiles(self, path):
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return {(z, x, (1 << z) - 1 - r): mvt.decode_tile(gzip.decompress(d))
                for z, x, r, d in conn.execute("SELECT * FROM tiles")}

    def test_the_town(self):
        box = osm.box_around(LAT, LON, 5)
        path = mapmaker.build(town()["elements"], box, self.dir / "millbrook.mbtiles", "Millbrook (5 km)")
        info = maps.info(path)
        self.assertEqual((info.name, info.vector, info.problem, info.minzoom, info.maxzoom),
                         ("Millbrook (5 km)", True, "", 8, 14))
        self.assertEqual(info.attribution, "© OpenStreetMap contributors")
        data, kind, encoding = maps.tile(path, 14, 8192, 5494)
        self.assertEqual((kind, encoding), ("application/x-protobuf", "gzip"))
        tiles = self.tiles(path)
        centre = tiles[(14, 8192, 5494)]
        self.assertEqual(set(centre), {"water", "landuse", "park", "waterway", "building", "transportation", "water_name",
                                       "place", "poi"})
        self.assertEqual(centre["water_name"][0].properties["name"], "Mill Pond")
        self.assertEqual(sorted(f.properties["class"] for f in centre["poi"]),
                         ["clinic", "drinking_water", "drinking_water", "food", "fuel", "hospital", "pharmacy"])
        self.assertEqual(centre["place"][0].properties, {"class": "town", "name": "Millbrook", "rank": 2})
        # Further out: no houses, no side streets, but the main road and the town's name.
        far = tiles[(10, 512, 343)]
        self.assertNotIn("building", far)
        self.assertEqual({f.properties["class"] for f in far["transportation"]}, {"primary", "rail"})
        # Every polygon is well formed: outer rings clockwise, holes not, all
        # inside the tile and its margin.
        holes = 0
        for layers in tiles.values():
            for feats in layers.values():
                for f in feats:
                    for part in f.parts:
                        self.assertTrue(all(-65 <= x <= 4161 and -65 <= y <= 4161 for x, y in part))
                    if f.type == mvt.POLYGON:
                        self.assertGreater(mvt.ring_area(f.parts[0]), 0)
                        holes += sum(mvt.ring_area(p) < 0 for p in f.parts[1:])
        self.assertGreater(holes, 0, "the clearing in the wood")

    def test_made_from_a_download_kept_and_made_again_offline(self):
        fake = FakeOsm()
        self.addCleanup(fake.close)
        old = osm.OVERPASS, osm.NOMINATIM, osm.PAUSE
        osm.OVERPASS, osm.NOMINATIM, osm.PAUSE = [fake.url + "/api/interpreter"], fake.url + "/search", 0
        self.addCleanup(lambda: setattr(osm, "OVERPASS", old[0]) or setattr(osm, "NOMINATIM", old[1])
                        or setattr(osm, "PAUSE", old[2]))
        (place,) = osm.find_place("Millbrook")
        self.assertEqual((place["name"], place["lat"]), ("Millbrook", LAT))
        self.assertEqual(osm.find_place("50.5, -1.25")[0]["lat"], 50.5)  # no search for a position
        steps = []
        box = osm.box_around(place["lat"], place["lon"], 10)
        path = mapmaker.make("Millbrook (10 km)", box, self.dir, True, lambda s, d, t: steps.append((s, d, t)))
        self.assertEqual(path, self.dir / "millbrook-10-km.mbtiles")
        self.assertEqual(len(fake.queries), 2)  # 10 km is two pieces here
        self.assertIn('way["building"]', fake.queries[0])
        self.assertIn(("downloading", 2, 2), steps)
        source = self.dir / "sources" / "millbrook-10-km"
        self.assertEqual(osm.read_area(source)["name"], "Millbrook (10 km)")
        # A second time downloads nothing: the pieces are kept.
        mapmaker.make("Millbrook (10 km)", box, self.dir)
        self.assertEqual(len(fake.queries), 2)
        fake.close()
        again = mapmaker.remake(source, self.dir)
        self.assertEqual((again, maps.info(again).name), (path, "Millbrook (10 km)"))
        renamed = mapmaker.remake(source, self.dir, "Millbrook before the flood")
        self.assertEqual(renamed.name, "millbrook-before-the-flood.mbtiles")
        self.assertTrue(path.exists(), "the first one is kept")
        # Stopped before a piece: what was there is kept, and it says so.
        stop = threading.Event()
        stop.set()
        with self.assertRaises(osm.OsmError):
            mapmaker.make("Elsewhere (5 km)", osm.box_around(10, 10, 5), self.dir, stop=stop)


if __name__ == "__main__":
    unittest.main()
