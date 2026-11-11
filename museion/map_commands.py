"""The Map tab's commands: the maps in the library, the person's places,
and making a map of an area from OpenStreetMap data (mapmaker.py). The
tiles themselves are served by app.py at /tiles/<map>/<z>/<x>/<y>."""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path

from museion import mapmaker, maps, notes, osm
from museion.appstate import AppState, UiError

# One map is made at a time; Stop asks it to stop between pieces.
_making = threading.Lock()
_stop = threading.Event()


def map_folders(state: AppState) -> list[Path]:
    cfg = state.config()
    found = maps.folder_of(cfg.library_dir)
    return ([found] if found else []) + list(cfg.extra_dirs)


def find_map(state: AppState, map_id: str) -> Path | None:
    return next((p for p in maps.map_files(map_folders(state)) if maps.map_id(p) == map_id), None)


def list_maps(state: AppState) -> dict:
    folder = maps.folder_of(state.config().library_dir)
    return {
        "folder": str(folder) if folder else "",
        "maps": [dataclasses.asdict(maps.info(p)) for p in maps.map_files(map_folders(state))],
    }


def list_places(state: AppState) -> list[dict]:
    try:
        return maps.places(state.notes_dir(state.config()))
    except notes.NoteError as e:
        raise UiError(str(e)) from None


def add_place(state: AppState, name: str, lat: float, lon: float, kind: str = "", notes_text: str = "") -> list[dict]:
    where = state.notes_dir(state.config())
    if where is None:
        raise UiError("No library is set up yet, so there is nowhere to keep places.")
    try:
        maps.add_place(where, name, kind, float(lat), float(lon), notes_text)
    except (maps.MapError, notes.NoteError) as e:
        raise UiError(str(e)) from None
    return list_places(state)


def open_maps_folder(state: AppState) -> None:
    from museion.commands import open_path
    folder = maps.folder_of(state.config().library_dir)
    if folder is None:
        raise UiError("No library is set up yet.")
    folder.mkdir(parents=True, exist_ok=True)
    open_path(state, str(folder))


# ── making a map ─────────────────────────────────────────────────────────────

def maps_dir(state: AppState) -> Path:
    folder = maps.folder_of(state.config().library_dir)
    if folder is None:
        raise UiError("No library is set up yet, so there is nowhere to keep a map.")
    return folder


def find_place(state: AppState, query: str) -> list[dict]:
    try:
        return osm.find_place(query)
    except osm.OsmError as e:
        raise UiError(str(e)) from None


def map_plan(state: AppState, name: str, lat: float, lon: float, km: float, buildings: bool = True) -> dict:
    """What making this map would do, before anything is downloaded: the
    area, how many pieces, and whether part of it is downloaded already."""
    try:
        box = osm.box_around(float(lat), float(lon), float(km))
    except osm.OsmError as e:
        raise UiError(str(e)) from None
    folder = maps_dir(state)
    mid = mapmaker.map_id(name)
    source = folder / "sources" / mid
    pieces = len(osm.cells(box))
    have = len(list(source.glob("part-*.json.gz"))) if source.is_dir() else 0
    w, h = box.km()
    return {"id": mid, "box": box.as_list(), "km2": round(w * h), "pieces": pieces,
            "downloaded": min(have, pieces), "exists": (folder / f"{mid}.mbtiles").is_file()}


def make_map(state: AppState, name: str, lat: float, lon: float, km: float, buildings: bool = True) -> dict:
    """Download the area and make its map, reporting as it goes
    ("map-progress"). Returns the new map, as list_maps gives it."""
    name = " ".join(str(name).split())[:80]
    if not name:
        raise UiError("Give the map a name.")
    try:
        box = osm.box_around(float(lat), float(lon), float(km))
    except osm.OsmError as e:
        raise UiError(str(e)) from None
    folder = maps_dir(state)
    if not _making.acquire(blocking=False):
        raise UiError("A map is being made already; wait for it, or press Stop.")
    _stop.clear()
    try:
        def report(step: str, done: int, total: int) -> None:
            state.emit("map-progress", {"step": step, "done": done, "total": total})

        path = mapmaker.make(name, box, folder, bool(buildings), report, _stop)
    except osm.OsmError as e:
        raise UiError(str(e)) from None
    finally:
        _making.release()
    return dataclasses.asdict(maps.info(path))


def stop_map(state: AppState) -> None:
    _stop.set()
