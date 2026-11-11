"""`python -m museion climate`: make the library's table of frost dates and
hardiness zones for a region (needs the internet once), and look a place up
in it."""

from __future__ import annotations

from pathlib import Path

from museion import climate


def add_commands(commands) -> None:
    c = commands.add_parser("climate", help="frost dates and hardiness zones by place, from weather stations")
    sub = c.add_subparsers(dest="climate_command", required=True)
    b = sub.add_parser("build", help="download a region's weather-station records and make the table "
                                     "(needs the internet once)")
    b.add_argument("--region", required=True,
                   help="countries by GHCN code (US, UK, CA ...), states or provinces (US-WA CA-BC), or "
                        "box:south,west,north,east; several separated by spaces")
    b.add_argument("--library", type=Path, required=True, help="the library: the table goes in its climate folder")
    b.add_argument("--offline", action="store_true", help="make it again from what was downloaded before")
    b.add_argument("--years", default=f"{climate.YEARS[0]}-{climate.YEARS[1]}", help="the years to average")
    b.set_defaults(run=cmd_build)
    look = sub.add_parser("lookup", help="the frost dates and zone of a place")
    look.add_argument("place", help='a town ("Bellevue, Washington") or "lat, lon"')
    look.add_argument("--library", type=Path, required=True)
    look.set_defaults(run=cmd_lookup)


def cmd_build(args) -> None:
    first, _, last = args.years.partition("-")
    years = (int(first), int(last))
    shown = {"stations": -1}

    def report(step: str, done: int, total: int) -> None:
        if step == "station" and (done == total or done - shown["stations"] >= max(1, total // 20)):
            shown["stations"] = done
            print(f"  stations {done} of {total}", flush=True)
        elif step == "towns" and done == 0:
            print("  towns", flush=True)

    info = climate.build(args.library, args.region, offline=args.offline, years=years, report=report)
    print(f"\nMade {info['folder']}: {info['stations']} stations, {info['towns']} towns, "
          f"{info['downloaded_bytes'] / 1e6:.1f} MB downloaded")


def cmd_lookup(args) -> None:
    found, chosen, e = climate.look_up(args.library, args.place)
    print(climate.describe(e, chosen.label))
    others = [p.label for p in found if p is not chosen]
    if others:
        print("\nAlso called that: " + "; ".join(others))
