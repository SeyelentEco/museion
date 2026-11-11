"""almanac: what is due now and in the next weeks, from the person's
almanac.csv (timed from their frost dates), maintenance.csv and
medicines.csv. Museion works the dates out (museion/almanac.py); the model
only reads them."""

import datetime

from museion import almanac
from museion.agent_tools import READ, Tool, ToolError
from museion.agent_tools._notes import folder


def starting_day(text: str, today: datetime.date) -> datetime.date:
    """A date (2026-03-01) or a month (March): the next time it comes,
    counting this month as come."""
    text = (text or "").strip().lower()
    if not text:
        return today
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        pass
    month = almanac.MONTHS.get(text)
    if month is None:
        raise ToolError(f"{text!r} is not a date like 2026-03-01 or a month like March.")
    if month == today.month:
        return today
    year = today.year if month > today.month else today.year + 1
    return datetime.date(year, month, 1)


def describe(item: almanac.Item, today: datetime.date) -> str:
    where = f"{item.kind}, {item.source}"
    if item.status == "overdue":
        when = f"OVERDUE by {-item.days} day{'s' if item.days != -1 else ''} ({item.start.isoformat()})"
    elif item.status == "now":
        when = "due now" + (f", until {item.end.isoformat()}" if item.end > today else "")
    else:
        when = f"in {item.days} day{'s' if item.days != 1 else ''} ({item.start.isoformat()} to {item.end.isoformat()})"
    told = f"; the record says: {item.when}" if item.when else ""
    extra = f". Note: {item.notes}" if item.notes else ""
    return f"- {item.task} ({where}): {when}{told}{extra}"


def run(args: dict, run) -> str:
    weeks = max(1, min(int(args.get("weeks") or almanac.WEEKS), 26))
    start = starting_day(args.get("starting", ""), datetime.date.today())
    found = almanac.coming_up(folder(run), start, weeks)
    lines = [f"Looking from {start.isoformat()} for {weeks} weeks."]
    if not found.has_almanac:
        lines.append("The person has no almanac.csv yet; they can start one in the Notes tab (New record, Almanac). "
                     "Only maintenance and medicines are shown.")
    if found.frosts:
        lines.append("Frost dates in almanac.csv: " + ", ".join(f"{k} frost {v}" for k, v in found.frosts.items()) + ".")
    for which, count in found.needs_frost.items():
        lines.append(f"{count} task(s) are timed from the {which} frost, which almanac.csv does not give yet. "
                     f"Ask the person for their average {which} frost date (month-day, like "
                     f"{'04-20' if which == 'last' else '10-15'}) and write it in the {which} frost row.")
    if found.items:
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
        "starting": {"type": "string", "description": "Optional: look from another time, a date like 2026-03-01 "
                     "or a month like March (default today)"},
        "weeks": {"type": "integer", "description": "How many weeks ahead to look (default 6)"},
    },
    required=[],
    risk=READ,
    run=run,
    describe=lambda a: "look at the almanac" + (f" from {a['starting']}" if a.get("starting") else ""),
)
