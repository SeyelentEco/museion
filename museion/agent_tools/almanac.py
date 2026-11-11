"""almanac: what is due now and in the next weeks, from the person's
almanac.csv (timed from their frost dates), maintenance.csv and
medicines.csv. Museion works the dates out (museion/almanac.py); the model
only reads them."""

import datetime

from museion import almanac
from museion.agent_tools import READ, Tool, ToolError
from museion.agent_tools._notes import folder


NOW = {"", "today", "now", "this month", "this week", "current", "currently"}


def starting_day(text: str, today: datetime.date) -> datetime.date:
    """A date (2026-03-01) or a month (March): the next time it comes,
    counting this month as come. Nothing, "today" or "now" is today: a model
    does not know the date, and on the 35B guessed 2023-10-01."""
    text = (text or "").strip().lower()
    if text in NOW:
        return today
    try:
        day = datetime.date.fromisoformat(text[:10])
    except ValueError:
        pass
    else:
        if (today - day).days > 31:
            raise ToolError(f"{day.isoformat()} is in the past: today is {today.isoformat()}. "
                            "Leave starting out to look from today.")
        return day
    month = almanac.MONTHS.get(text)
    if month is None:
        raise ToolError(f"{text!r} is not a date like {today.isoformat()} or a month like March. "
                        f"Leave starting out to look from today ({today.isoformat()}).")
    if month == today.month:
        return today
    year = today.year if month > today.month else today.year + 1
    return datetime.date(year, month, 1)


def has_climate(run) -> bool:
    from museion import climate
    lib = getattr(run.setting, "library_dir", None)
    return lib is not None and (lib / climate.FOLDER / climate.STATIONS).is_file()


def frosts_for(place: str, run) -> tuple[dict[str, str], str]:
    """Frost dates for a place from the library's climate table, for the
    almanac to use where almanac.csv gives none, and what to say of them."""
    from museion import climate, maps
    lib = getattr(run.setting, "library_dir", None)
    if lib is None:
        raise ToolError("There is no library here, so no climate table to look the place up in.")
    try:
        _, chosen, e = climate.look_up(lib, place, maps.places(getattr(run.setting, "notes_dir", None)))
    except climate.ClimateError as err:
        raise ToolError(str(err)) from None
    said = (climate.describe(e, chosen.label) + " Used where almanac.csv has no frost dates; the person can "
            "save them with Look up in the Notes tab's Coming up.")
    return {"last": e.last_frost, "first": e.first_frost}, said


def frosts_at_home(run) -> tuple[dict[str, str], str]:
    """Frost dates for the person's home area (Settings), when it is set
    and the climate table covers it; else nothing."""
    from museion import climate, maps
    lib = getattr(run.setting, "library_dir", None)
    try:
        home = maps.home(getattr(run.setting, "notes_dir", None))
    except Exception:  # noqa: BLE001 - an unreadable places.csv is no home
        home = None
    if lib is None or home is None or not has_climate(run):
        return {}, ""
    try:
        e = climate.at_point(lib, home["lat"], home["lon"])
    except climate.ClimateError:
        return {}, ""
    said = (climate.describe(e, f"the person's home area, {home['name']}") + " Used where almanac.csv has no "
            "frost dates.")
    return {"last": e.last_frost, "first": e.first_frost}, said


MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]


def day(d: datetime.date) -> str:
    """13 February 2027: the words a model copies rather than works out."""
    return f"{d.day} {MONTH_NAMES[d.month - 1]} {d.year}"


def describe(item: almanac.Item, today: datetime.date) -> str:
    where = f"{item.kind}, {item.source}"
    if item.status == "overdue":
        when = f"OVERDUE by {-item.days} day{'s' if item.days != -1 else ''} (since {day(item.start)})"
    elif item.status == "now":
        when = (f"due now, from {day(item.start)} to {day(item.end)}" if item.end > item.start
                else f"due now, on {day(item.start)}")
    else:
        when = f"in {item.days} day{'s' if item.days != 1 else ''}, from {day(item.start)} to {day(item.end)}"
    # The 35B, given only the rule, worked the dates out again, and wrongly
    # (peas "6 to 21 March" for 3-6 weeks before a last frost of 27 March).
    told = f" (worked out from \"{item.when}\")" if item.when else ""
    extra = f". Note: {item.notes}" if item.notes else ""
    return f"- {item.task} ({where}): {when}{told}{extra}"


def run(args: dict, run) -> str:
    weeks = max(1, min(int(args.get("weeks") or almanac.WEEKS), 26))
    today = datetime.date.today()
    start = starting_day(args.get("starting", ""), today)
    place = (args.get("place") or "").strip()
    fill, looked = {}, ""
    if place:
        fill, looked = frosts_for(place, run)
    found = almanac.coming_up(folder(run), start, weeks, fill)
    if found.needs_frost and not place:
        fill, looked = frosts_at_home(run)
        if fill:
            found = almanac.coming_up(folder(run), start, weeks, fill)
    lines = [f"Today is {today.isoformat()}. Looking from {start.isoformat()} for {weeks} weeks."]
    if looked:
        lines.append(looked)
    if not found.has_almanac:
        lines.append("The person has no almanac.csv yet; they can start one in the Notes tab (New record, Almanac). "
                     "Only maintenance and medicines are shown.")
    if found.frosts:
        lines.append(("Frost dates used: " if looked else "Frost dates in almanac.csv: ")
                     + ", ".join(f"{k} frost {v}" for k, v in found.frosts.items()) + ".")
    for which, count in found.needs_frost.items():
        lines.append(f"{count} task(s) are timed from the {which} frost, which almanac.csv does not give yet. "
                     + ("Call almanac again with place: the person's town, to use weather-station averages; "
                        "if you do not know their town, ask for it (not for the dates)."
                        if has_climate(run) and not place else
                        f"Ask the person for their average {which} frost date (month-day, like "
                        f"{'04-20' if which == 'last' else '10-15'})."))
    if found.items:
        lines.append("Each task's dates are worked out already: give them as they are, and do not work dates out "
                     "again from the rules. A task not listed is not due in this time.")
        lines += [describe(i, start) for i in found.items]
    else:
        lines.append("Nothing is due in that time.")
    lines += [f"Problem: {p}" for p in found.problems]
    return "\n".join(lines)


TOOL = Tool(
    name="almanac",
    description=("What is due now and in the coming weeks: the person's almanac.csv (garden, preserving, "
                 "animal and house tasks timed from their frost dates), maintenance that is due and medicines "
                 "running out. The dates are worked out for you. Use it for 'what should I be doing now?'"),
    parameters={
        "starting": {"type": "string", "description": "Leave out to look from today (Museion knows the date). "
                     "Only for another time the person names: a month like March, or a date"},
        "weeks": {"type": "integer", "description": "How many weeks ahead to look (default 6)"},
        "place": {"type": "string", "description": "The person's town, only when almanac.csv has no frost "
                  "dates: they are then taken from weather-station averages"},
    },
    required=[],
    risk=READ,
    run=run,
    describe=lambda a: "look at the almanac" + (f" from {a['starting']}" if a.get("starting") else ""),
)
