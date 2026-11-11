"""climate: a place's average frost dates and hardiness zone, from the
library's table of weather stations (museion/climate.py). Museion finds the
place and works the averages out; the model only reads them."""

from museion import climate, maps
from museion.agent_tools import READ, Tool, ToolError


def library(run):
    found = getattr(run.setting, "library_dir", None)
    if found is None:
        raise ToolError("There is no library here, so no climate table.")
    return found


def run(args: dict, run) -> str:
    try:
        found, chosen, e = climate.look_up(library(run), args.get("place", ""),
                                           maps.places(getattr(run.setting, "notes_dir", None)))
    except climate.ClimateError as err:
        raise ToolError(str(err)) from None
    lines = [climate.describe(e, chosen.label)]
    others = [p.label for p in found if p is not chosen]
    if others:
        lines.append("Other places with that name: " + "; ".join(others[:5])
                     + ". If the person meant one of these, call again with its full name.")
    lines.append("To use these dates in the almanac, the person can press Look up in the Notes tab's "
                 "Coming up, or you can pass the place to the almanac tool.")
    return "\n".join(lines)


TOOL = Tool(
    name="climate",
    description=("A place's average last spring and first autumn frost dates, frost-free days and "
                 "hardiness zone, from weather-station records in the library. For when the person "
                 "names their town and their frost dates or zone are not known."),
    parameters={
        "place": {"type": "string", "description": "A town (\"Bellevue, Washington\"), a saved place, "
                  "or a latitude and longitude"},
    },
    required=["place"],
    risk=READ,
    run=run,
    describe=lambda a: f"look up the climate of {a.get('place', '')}",
)
