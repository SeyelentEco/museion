"""`python -m museion make-map`: the Map tab's Make a map, from the command
line. The first place the search finds is used; give a latitude and
longitude to be sure."""

from __future__ import annotations

from pathlib import Path

from museion import mapmaker, maps, osm


def add_commands(commands) -> None:
    c = commands.add_parser("make-map", help="download OpenStreetMap data for an area and make an offline map of it")
    c.add_argument("where", nargs="?", help='a place ("Lewes, England") or a latitude and longitude ("50.87, 0.01")')
    c.add_argument("--km", type=float, default=10, help=f"how far across, up to {osm.MAX_KM} (default 10)")
    c.add_argument("--no-buildings", action="store_true", help="leave the buildings out (a smaller download)")
    c.add_argument("--name", help="the map's name (default: the place and its size)")
    c.add_argument("--library", type=Path, help="the library: the map goes in its maps folder")
    c.add_argument("--dest", type=Path, help="the folder to put the map in, instead of <library>/maps")
    c.add_argument("--again", metavar="ID", help="make a map again from the data downloaded for it before, "
                                                 "with no internet (its folder name in maps/sources)")
    c.set_defaults(run=cmd_make_map)


def cmd_make_map(args) -> None:
    folder = args.dest or (maps.folder_of(args.library) if args.library else None)
    if folder is None:
        raise ValueError("need --library (or --dest), for where the map goes")

    def report(step: str, done: int, total: int) -> None:
        if step == "downloading":
            print(f"  downloading piece {done} of {total}", flush=True)
        elif step == "drawing" and done < total:
            print(f"  drawing zoom {mapmaker.MINZOOM + done}", flush=True)

    if args.again:
        path = mapmaker.remake(folder / "sources" / args.again, folder, args.name, report)
    else:
        if not args.where:
            raise ValueError("say where: a place, or a latitude and longitude")
        found = osm.find_place(args.where)
        if not found:
            raise ValueError(f"nothing found called {args.where!r}")
        place = found[0]
        print(f"{place['label']}  ({place['lat']:.5f}, {place['lon']:.5f})")
        name = args.name or f"{place['name']} ({args.km:g} km)"
        box = osm.box_around(place["lat"], place["lon"], args.km)
        print(f"{len(osm.cells(box))} piece(s) to download")
        path = mapmaker.make(name, box, folder, not args.no_buildings, report)
    info = maps.info(path)
    print(f"\nMade {path} ({info.size / 1e6:.1f} MB, zoom {info.minzoom} to {info.maxzoom})")
