"""Your home area (Settings, and the end of first-run set-up): where the
person lives, set once while there is internet, so the almanac, the agents
and the map know it with none.

Setting it up keeps the place as the one of kind "home" in places.csv,
downloads the weather records of the stations around it into the library's
climate table (climate.py), puts its average frost dates and hardiness zone
in almanac.csv (unless the person typed their own), and can make a map of
it (map_commands.make_map). Each part keeps what it downloaded, so doing it
again, or after a Stop, carries on rather than starting over."""

from __future__ import annotations

import threading
from pathlib import Path

from museion import almanac, climate, maps, notes
from museion.appstate import AppState, UiError

RADIUS_KM = 150  # the stations used come from this far around home ...
MOST_STATIONS = 40  # ... at most this many of them: about 1-4 MB each to download

# One set-up at a time; Stop asks it to stop between stations.
_busy = threading.Lock()
_stop = threading.Event()


def _folders(state: AppState) -> tuple[Path, Path]:
    cfg = state.config()
    where = state.notes_dir(cfg)
    if where is None or cfg.library_dir is None:
        raise UiError("No library is set up yet, so there is nowhere to keep your home area.")
    return Path(cfg.library_dir), where


def home_area(state: AppState) -> dict:
    """The home area as set: the place, the climate table it has (or none),
    and the frost dates and zone the table gives for it."""
    cfg = state.config()
    where = state.notes_dir(cfg)
    out = {"home": None, "climate": None, "estimate": None, "text": "", "radius_km": RADIUS_KM}
    if where is None or cfg.library_dir is None:
        return out
    try:
        out["home"] = maps.home(where)
    except notes.NoteError:
        pass
    folder = Path(cfg.library_dir) / climate.FOLDER
    if (folder / climate.STATIONS).is_file():
        out["climate"] = climate.about(folder) or {"region": "your region"}
        if out["home"]:
            try:
                e = climate.at_point(cfg.library_dir, out["home"]["lat"], out["home"]["lon"])
            except climate.ClimateError:
                pass
            else:
                out["estimate"], out["text"] = e.as_dict(), climate.describe(e, out["home"]["name"])
    return out


def set_up_home(state: AppState, name: str, lat: float, lon: float, km: float = RADIUS_KM,
                map_km: float = 0) -> dict:
    """Make this the home area and get what Museion needs to know it with
    no internet, reporting as it goes ("home-progress"; a map reports as
    "map-progress"). Returns home_area, with what was done."""
    name = " ".join(str(name).split())[:80]
    if not name:
        raise UiError("Give your home area a name, like the town.")
    try:
        lat, lon, km = float(lat), float(lon), float(km)
    except (TypeError, ValueError):
        raise UiError("That place has no position.") from None
    library, where = _folders(state)
    if not _busy.acquire(blocking=False):
        raise UiError("Your home area is being set up already; wait for it, or press Stop.")
    _stop.clear()
    done: dict = {}
    try:
        def report(step: str, at: int, total: int) -> None:
            state.emit("home-progress", {"step": step, "done": at, "total": total})

        try:
            maps.set_home(where, name, lat, lon)
        except (maps.MapError, notes.NoteError) as e:
            raise UiError(str(e)) from None
        try:
            built = climate.build(library, f"near:{lat:.4f},{lon:.4f},{km:g}", climate.http_get, report=report,
                                  label=f"within {km:g} km of {name}", most=MOST_STATIONS, stop=_stop)
        except climate.ClimateError as e:
            raise UiError(str(e)) from None
        except OSError as e:
            raise UiError(f"Could not write the climate table into the library: {e}") from None
        done["downloaded_bytes"] = built["downloaded_bytes"]
        done["stations"] = built["stations"]
        report("almanac", 0, 1)
        done["frosts"] = fill_almanac(state, library, where, name, lat, lon)
        if map_km and not _stop.is_set():
            report("map", 0, 1)
            from museion import map_commands
            done["map"] = map_commands.make_map(state, name, lat, lon, float(map_km))["name"]
        report("done", 1, 1)
    finally:
        _busy.release()
    return {**home_area(state), "done": done}


def fill_almanac(state: AppState, library: Path, where: Path, name: str, lat: float, lon: float) -> str:
    """Put home's frost dates and zone in almanac.csv (starting it if there
    is none), unless it has dates the person typed. Says which it did."""
    try:
        e = climate.at_point(library, lat, lon)
    except climate.ClimateError as err:
        return f"not filled: {err}"
    had = almanac.coming_up(where)
    if had.frosts and not had.frost_note.startswith(almanac.LOOKED_UP):
        return "kept: almanac.csv has frost dates you typed"
    try:
        almanac.set_frosts(where, e.last_frost, e.first_frost, e.zone, climate.describe(e, name))
    except (almanac.WhenError, notes.NoteError) as err:
        return f"not filled: {err}"
    return "filled"


def stop_home(state: AppState) -> None:
    _stop.set()
    from museion import map_commands
    if _busy.locked():
        map_commands.stop_map(state)
