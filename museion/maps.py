"""Offline maps: MBTiles files in the library's maps folder, and the places
the person marks on them.

An .mbtiles file is an SQLite database of map tiles (the MBTiles format:
a metadata table of name/value pairs and a tiles table keyed by zoom, column
and row). Python reads it with nothing but sqlite3, and the page draws the
tiles itself (ui/map.js), so a map needs no server, no internet and no
library. Raster tiles (PNG, JPEG or WebP pictures) are shown as they are;
vector tiles (pbf: roads, water and names as shapes, mvt.py) are drawn by
the page (ui/mapdraw.js). A map is made before it is needed: in the Map tab,
which downloads OpenStreetMap data for an area and makes a vector map of it
(mapmaker.py), or with QGIS and copied into <library>/maps/ (docs/maps.md).

Places are a record in the person's notes, places.csv (name, kind, lat, lon,
notes), so they are searched and cited like any other note and the agents
can read them.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from museion import notes

FOLDER = "maps"
PLACES = "places.csv"
PLACE_COLUMNS = ["name", "kind", "lat", "lon", "notes"]

# What a tile is, from its first bytes: the page is told its type.
SIGNATURES = [(b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"), (b"RIFF", "image/webp")]

_connections: dict[Path, sqlite3.Connection] = {}
_lock = threading.Lock()


class MapError(ValueError):
    """A problem with a map or a place, worded for the person."""


@dataclass
class MapInfo:
    id: str  # the file's name without .mbtiles: how the page asks for its tiles
    name: str
    file: str
    size: int
    format: str
    minzoom: int
    maxzoom: int
    bounds: list[float] | None  # west, south, east, north
    center: list[float] | None  # lon, lat, zoom
    attribution: str
    problem: str = ""  # why it cannot be shown, if it cannot
    vector: bool = False  # vector tiles (mvt.py), drawn by the page (ui/mapdraw.js)


def folder_of(library_dir: Path | None) -> Path | None:
    return library_dir / FOLDER if library_dir else None


def map_id(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", path.stem)


def map_files(folders: list[Path]) -> list[Path]:
    out = []
    for folder in folders:
        try:
            out += sorted(p for p in folder.iterdir() if p.suffix.lower() == ".mbtiles" and p.is_file())
        except OSError:
            continue
    return out


def connect(path: Path) -> sqlite3.Connection:
    """The map, open for reading only, kept open between tiles: the page asks
    for dozens of tiles a second while it is dragged."""
    with _lock:
        conn = _connections.get(path)
        if conn is None:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
            _connections[path] = conn
        return conn


def info(path: Path) -> MapInfo:
    """What the page needs to show a map: its zoom levels and where it is."""
    out = MapInfo(map_id(path), path.stem, path.name, 0, "", 0, 0, None, None, "")
    try:
        out.size = path.stat().st_size
        conn = connect(path)
        with _lock:
            meta = dict(conn.execute("SELECT name, value FROM metadata").fetchall())
            lo, hi = conn.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
        out.name = meta.get("name") or path.stem
        out.attribution = re.sub(r"<[^>]*>", "", meta.get("attribution", ""))
        out.format = meta.get("format", "")
        out.vector = out.format.lower() == "pbf"
        if lo is None:
            out.problem = out.problem or "it holds no tiles"
        else:
            out.minzoom, out.maxzoom = int(lo), int(hi)
        if meta.get("bounds"):
            out.bounds = [float(v) for v in meta["bounds"].split(",")][:4]
        if meta.get("center"):
            out.center = [float(v) for v in meta["center"].split(",")][:3]
        elif out.bounds:
            w, s, e, n = out.bounds
            out.center = [(w + e) / 2, (s + n) / 2, out.minzoom]
    except (OSError, sqlite3.Error, ValueError) as e:
        out.problem = f"it could not be read: {e}"
    return out


def replace(new: Path, path: Path) -> None:
    """Put a newly made map in place of an old one. The old one is closed
    first (Windows will not replace a file that is open), and no tile is
    read until the new one is there."""
    with _lock:
        for key in [k for k in _connections if k.resolve() == path.resolve()]:
            _connections.pop(key).close()
        os.replace(new, path)


def tile(path: Path, z: int, x: int, y: int) -> tuple[bytes, str, str] | None:
    """One tile, as the page counts tiles (y from the top), its type, and
    "gzip" for a vector tile kept compressed (as they usually are), which the
    browser unpacks itself. MBTiles counts rows from the bottom."""
    row = (1 << z) - 1 - y
    conn = connect(path)
    # One connection serves every request thread; SQLite's Python module
    # does not let two of them use it at once.
    with _lock:
        found = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (z, x, row)).fetchone()
    if not found:
        return None
    data = bytes(found[0])
    if data.startswith(b"\x1f\x8b"):
        return data, "application/x-protobuf", "gzip"
    kind = next((t for sig, t in SIGNATURES if data.startswith(sig)), "application/x-protobuf")
    return data, kind, ""


# ── places ───────────────────────────────────────────────────────────────────

def places(notes_dir: Path | None) -> list[dict]:
    """The places in places.csv that have a position."""
    if notes_dir is None or not (notes_dir / PLACES).is_file():
        return []
    header, rows = notes.rows_of(notes.read(notes_dir, PLACES))
    out = []
    for i, r in enumerate(rows):
        row = {h: (r[j].strip() if j < len(r) else "") for j, h in enumerate(header)}
        try:
            lat, lon = float(row.get("lat", "")), float(row.get("lon", ""))
        except ValueError:
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            out.append({"row": i + 1, "name": row.get("name", ""), "kind": row.get("kind", ""),
                        "lat": lat, "lon": lon, "notes": row.get("notes", "")})
    return out


HOME = "home"  # the kind of the place that is the person's home area


def home(notes_dir: Path | None) -> dict | None:
    """The person's home area: the place of kind "home" in places.csv."""
    return next((p for p in places(notes_dir) if p["kind"].strip().lower() == HOME), None)


def set_home(notes_dir: Path, name: str, lat: float, lon: float, about: str = "") -> None:
    """Make this the home area: the one place of kind "home" in places.csv.
    An earlier home row is moved here (its notes kept unless new ones are
    given); places.csv keeps its previous version, as every note does."""
    name = name.strip()
    if not name:
        raise MapError("Give the place a name.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise MapError("That position is not on the map.")
    if (notes_dir / PLACES).is_file():
        import csv
        import io
        header, rows = notes.rows_of(notes.read(notes_dir, PLACES))
        cols = [h.strip().lower() for h in header]
        if all(c in cols for c in ("name", "kind", "lat", "lon")):
            at = {c: cols.index(c) for c in ("name", "kind", "lat", "lon", "notes") if c in cols}
            for r in rows:
                if at["kind"] < len(r) and r[at["kind"]].strip().lower() == HOME:
                    r.extend([""] * (len(header) - len(r)))
                    r[at["name"]], r[at["lat"]], r[at["lon"]] = name, f"{lat:.6f}", f"{lon:.6f}"
                    if about.strip() and "notes" in at:
                        r[at["notes"]] = about.strip()
                    out = io.StringIO()
                    csv.writer(out, lineterminator="\n").writerows([header, *rows])
                    notes.write(notes_dir, PLACES, out.getvalue())
                    return
    add_place(notes_dir, name, HOME, lat, lon, about)


def add_place(notes_dir: Path, name: str, kind: str, lat: float, lon: float, about: str = "") -> None:
    """Add a place to places.csv, starting the record if need be."""
    name = name.strip()
    if not name:
        raise MapError("Give the place a name.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise MapError("That position is not on the map.")
    if not (notes_dir / PLACES).is_file():
        notes.start_record(notes_dir, PLACES)
    import csv
    import io
    line = io.StringIO()
    csv.writer(line, lineterminator="").writerow([name, kind.strip(), f"{lat:.6f}", f"{lon:.6f}", about.strip()])
    notes.write(notes_dir, PLACES, line.getvalue(), append=True)
