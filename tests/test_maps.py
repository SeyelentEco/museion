"""Offline maps: reading an .mbtiles file, serving its tiles, and places."""

import json
import sqlite3
import struct
import tempfile
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from museion import maps, notes


def png(r: int, g: int, b: int, size: int = 8) -> bytes:
    """A small solid-colour PNG, made with the standard library."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes([r, g, b]) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_mbtiles(path: Path, zooms=range(0, 3), fmt="png", bounds="-10,40,10,55") -> None:
    """A map of coloured tiles: each tile's colour tells where it is."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    conn.executemany("INSERT INTO metadata VALUES (?, ?)", [
        ("name", "Test valley"), ("format", fmt), ("bounds", bounds),
        ("attribution", '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>')])
    for z in zooms:
        n = 1 << z
        for x in range(n):
            for y in range(n):
                conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)",
                             (z, x, n - 1 - y, png(min(40 + 60 * z, 255), (x * 255) // n, (y * 255) // n)))
    conn.commit()
    conn.close()


class MapsTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_info_and_tiles_counted_from_the_top(self):
        path = self.dir / "valley.mbtiles"
        make_mbtiles(path)
        got = maps.info(path)
        self.assertEqual((got.id, got.name, got.minzoom, got.maxzoom, got.problem), ("valley", "Test valley", 0, 2, ""))
        self.assertEqual(got.bounds, [-10.0, 40.0, 10.0, 55.0])
        self.assertEqual(got.center, [0.0, 47.5, 0])
        self.assertEqual(got.attribution, "© OpenStreetMap contributors")
        data, kind, encoding = maps.tile(path, 2, 1, 0)
        self.assertEqual((kind, encoding), ("image/png", ""))
        self.assertEqual(data, png(160, 63, 0))  # y=0 is the top row
        self.assertIsNone(maps.tile(path, 5, 0, 0))

    def test_vector_maps_and_broken_ones(self):
        make_mbtiles(self.dir / "vector.mbtiles", fmt="pbf")
        got = maps.info(self.dir / "vector.mbtiles")
        self.assertEqual((got.vector, got.problem), (True, ""))
        (self.dir / "junk.mbtiles").write_bytes(b"not a database")
        self.assertIn("could not be read", maps.info(self.dir / "junk.mbtiles").problem)
        self.assertEqual([p.name for p in maps.map_files([self.dir, self.dir / "none"])],
                         ["junk.mbtiles", "vector.mbtiles"])

    def test_places(self):
        self.assertEqual(maps.places(self.dir), [])
        maps.add_place(self.dir, "Spring, by the mill", "water", 51.5, -0.12, "clear all summer")
        maps.add_place(self.dir, "Clinic", "medical", 51.51, -0.1)
        notes.write(self.dir, "places.csv", "nowhere,other,,,", append=True)
        got = maps.places(self.dir)
        self.assertEqual([(p["name"], p["kind"], p["lat"]) for p in got],
                         [("Spring, by the mill", "water", 51.5), ("Clinic", "medical", 51.51)])
        with self.assertRaises(maps.MapError):
            maps.add_place(self.dir, "", "water", 1, 1)
        with self.assertRaises(maps.MapError):
            maps.add_place(self.dir, "Moon", "other", 91, 0)


class TileServerTest(unittest.TestCase):
    def test_tiles_are_served_by_map_name_only(self):
        import threading
        from museion.app import Events, Server
        from museion.appstate import AppState
        root = Path(tempfile.mkdtemp())
        (root / "library" / "maps").mkdir(parents=True)
        make_mbtiles(root / "library" / "maps" / "valley.mbtiles")
        (root / "config.json").write_text(json.dumps({"library_dir": str(root / "library")}))
        state = AppState(root / "config.json", root / "tuning.toml")
        server = Server(state, Events(), 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.port}"
        with urllib.request.urlopen(f"{base}/tiles/valley/1/0/1") as r:
            self.assertEqual(r.headers["Content-Type"], "image/png")
            self.assertEqual(r.read(), png(100, 0, 127))
        for missing in ("/tiles/valley/9/0/0", "/tiles/other/1/0/0", "/tiles/../../x/1/0/0", "/tiles/valley/1/0"):
            with self.subTest(missing), urllib.request.urlopen(base + missing) as r:
                self.assertEqual(r.status, 204)
        req = urllib.request.Request(f"{base}/api/list_maps", data=b"{}", method="POST",
                                     headers={"X-Museion": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            listed = json.load(r)["ok"]
        self.assertEqual([m["id"] for m in listed["maps"]], ["valley"])


if __name__ == "__main__":
    unittest.main()
