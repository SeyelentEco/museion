"""The Map tab's commands: the maps in the library and the person's places.
The tiles themselves are served by app.py at /tiles/<map>/<z>/<x>/<y>."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from museion import maps, notes
from museion.appstate import AppState, UiError


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
