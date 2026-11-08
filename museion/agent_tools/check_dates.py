"""check_dates: which rows of a record are due or out of date, by the
calendar, worked out by Museion rather than the model.

An expiry date (medicines.csv's expires) or, with every, a date last done
plus a number of days (maintenance.csv's last_done and every_days). Date
arithmetic is the kind of thing a model gets wrong quietly.
"""

import datetime

from museion import notes
from museion.agent_tools import READ, Tool, ToolError
from museion.agent_tools._notes import folder


def run(args: dict, run) -> str:
    within = args.get("within_days", 30)
    try:
        text = notes.read(folder(run), args["name"])
        dated, undated = notes.dates_due(text, args["column"], args.get("every") or None)
    except notes.NoteError as e:
        raise ToolError(str(e)) from None
    today = datetime.date.today()
    lines = [f"Today is {today.isoformat()}."]
    shown = [d for d in dated if d.days <= within]
    for d in shown:
        what = "; ".join(f"{k}: {v}" for k, v in d.row.items() if v)
        if d.days < 0:
            when = f"OVERDUE by {-d.days} day{'s' if d.days != -1 else ''} ({d.due.isoformat()})"
        elif d.days == 0:
            when = f"due TODAY ({d.due.isoformat()})"
        else:
            when = f"due in {d.days} day{'s' if d.days != 1 else ''} ({d.due.isoformat()})"
        lines.append(f"- {when}: {what}")
    later = len(dated) - len(shown)
    if not shown:
        lines.append(f"Nothing is due in the next {within} days.")
    if later:
        lines.append(f"{later} more row(s) fall due after {within} days.")
    if undated:
        lines.append(f"{len(undated)} row(s) have no date in {args['column']}: "
                     + ", ".join(next(iter(r.values()), "?") or "?" for r in undated[:10]))
    return "\n".join(lines)


TOOL = Tool(
    name="check_dates",
    description=("Which rows of a record are due, overdue or about to expire, counted from today. "
                 "For an expiry date give its column (medicines.csv: expires). For a task done "
                 "every so many days give the last-done column and the every column "
                 "(maintenance.csv: last_done and every_days)."),
    parameters={
        "name": {"type": "string", "description": "The record, like medicines.csv"},
        "column": {"type": "string", "description": "The date column: expires, or last_done"},
        "every": {"type": "string", "description": "Optional: the column with the number of days between tasks"},
        "within_days": {"type": "integer", "description": "How far ahead to look, in days (default 30)"},
    },
    required=["name", "column"],
    risk=READ,
    run=run,
    describe=lambda a: f"check the dates in {a['name']}",
)
