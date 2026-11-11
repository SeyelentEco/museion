"""The seasonal almanac: what to do in the garden, the kitchen, with animals
and to the house, at the right time of year for where the person lives.

The almanac is a record in the person's notes, almanac.csv (task, kind,
when, notes). A task's `when` is said the way growers say it:

    6-8 weeks before last frost     2 weeks after last frost
    at first frost                  4 weeks before first frost
    March    early March    mid March    late March    March to May
    04-15    04-15 to 05-10    15 April

and two rows of kind "frost" say this place's average frost dates:

    last frost,frost,04-20,average last spring frost
    first frost,frost,10-15,average first autumn frost

Timing from frost dates is what makes one list work in any climate and in
either hemisphere; a task can still be pinned to a month or a date. A single
date means the week that starts on it.

coming_up() gives what is due now and in the next weeks, with the
maintenance that is due (maintenance.csv) and the medicines that run out
(medicines.csv), worked out here rather than by a model: date arithmetic is
the kind of thing a model gets wrong quietly. The Notes tab shows it, and
agents get it from the almanac tool.
"""

from __future__ import annotations

import calendar
import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from museion import notes

FILE = "almanac.csv"
WEEKS = 6  # how far ahead "coming up" looks
LOOKED_UP = "Station averages"  # how climate.describe begins

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9
PART = {"early": (1, 10), "mid": (11, 20), "late": (21, 31)}

Window = tuple[datetime.date, datetime.date]


class WhenError(ValueError):
    """A `when` the almanac cannot read, worded for the person."""


@dataclass
class When:
    """A `when`, read: either fixed days of the year, or days from a frost."""
    frost: str = ""  # "last" or "first" for a time counted from a frost date
    days: tuple[int, int] = (0, 0)  # from that frost date: earliest, latest
    start: tuple[int, int] = (1, 1)  # otherwise: month and day it starts
    end: tuple[int, int] = (1, 1)  # and ends (it may run into the next year)
    extra: int = 0  # days after `end` (a single date is the week from it)

    def windows(self, year: int, frosts: dict[str, tuple[int, int]]) -> list[Window]:
        """When it falls in `year`, and in the years either side (a window
        can begin in one year and end in the next)."""
        out = []
        for y in (year - 1, year, year + 1):
            if self.frost:
                if self.frost not in frosts:
                    return []
                f = day_of(y, frosts[self.frost])
                out.append((f + datetime.timedelta(days=self.days[0]), f + datetime.timedelta(days=self.days[1])))
            else:
                start, end = day_of(y, self.start), day_of(y, self.end)
                if end < start:
                    end = day_of(y + 1, self.end)
                out.append((start, end + datetime.timedelta(days=self.extra)))
        return out


def day_of(year: int, month_day: tuple[int, int]) -> datetime.date:
    month, day = month_day
    return datetime.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def month_day(text: str) -> tuple[int, int] | None:
    """04-20, 2026-04-20, April 20 or 20 April; None if it is none of them."""
    text = text.strip().lower().rstrip(".")
    m = re.fullmatch(r"(?:\d{4}-)?(\d{1,2})-(\d{1,2})", text)
    if m:
        out = int(m[1]), int(m[2])
    else:
        m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?|(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)", text)
        if not m or (m[1] or m[4]) not in MONTHS:
            return None
        out = MONTHS[m[1] or m[4]], int(m[2] or m[3])
    if not (1 <= out[0] <= 12 and 1 <= out[1] <= calendar.monthrange(2024, out[0])[1]):
        return None
    return out


def month_part(text: str, ending: bool) -> tuple[int, int] | None:
    """March, early March, mid March, late March: its first day, or with
    `ending` its last."""
    words = text.split()
    if len(words) == 1:
        part = (1, 31)
    elif len(words) == 2 and words[0] in PART:
        part = PART[words[0]]
    else:
        return None
    if words[-1] not in MONTHS:
        return None
    # The end may say the 31st of a shorter month: day_of() makes it the
    # month's last day in the year it falls (late February ends on the 29th
    # in a leap year).
    return MONTHS[words[-1]], part[1] if ending else part[0]


FROST = re.compile(
    r"(?:(?P<n>\d+)(?:\s*(?:-|–|to)\s*(?P<m>\d+))?\s*(?P<unit>weeks?|days?)\s+(?P<side>before|after)\s+"
    r"|(?:at|around|by)\s+)?(?:the\s+)?(?P<which>last|first)\s+(?:spring\s+|autumn\s+|fall\s+)?frost")


def parse_when(text: str) -> When | None:
    """A task's `when`; None when it has none. Raises WhenError when it
    cannot be read."""
    text = re.sub(r"\s+", " ", (text or "").strip().lower().replace("mid-", "mid "))
    if not text:
        return None
    m = FROST.fullmatch(text)
    if m:
        if not m["n"]:
            return When(frost=m["which"], days=(0, 6))
        unit = 7 if m["unit"].startswith("week") else 1
        a, b = sorted((int(m["n"]), int(m["m"] or m["n"])))
        if m["side"] == "before":
            days = (-b * unit, -a * unit)
        else:
            days = (a * unit, b * unit)
        return When(frost=m["which"], days=days if days[0] != days[1] else (days[0], days[0] + 6))
    first, _, last = text.partition(" to ")
    one = month_day(first)
    if one:
        other = month_day(last) if last else None
        if last and not other:
            raise WhenError(f"{text!r}: the end of the range is not a date like 05-10 or 10 May.")
        if other is None:
            return When(start=one, end=one, extra=6)
        return When(start=one, end=other)
    start = month_part(first, ending=False)
    end = month_part(last or first, ending=True)
    if start and end:
        return When(start=start, end=end)
    raise WhenError(f"{text!r} is not a time the almanac can read. Write it like "
                    "'6-8 weeks before last frost', 'at first frost', 'mid March', "
                    "'March to May' or '04-15'.")


@dataclass
class Item:
    """One thing that is due: from the almanac, maintenance or medicines."""
    task: str
    kind: str
    status: str  # "overdue", "now" or "soon"
    days: int  # until it starts (0 now; negative: overdue by that many)
    start: datetime.date
    end: datetime.date
    when: str  # as the record says it
    notes: str
    source: str  # the record it came from

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d


@dataclass
class Almanac:
    today: datetime.date
    weeks: int
    items: list[Item]
    frosts: dict[str, str]  # "last" / "first": "04-20", as set
    needs_frost: dict[str, int]  # tasks timed from a frost date that is not set
    problems: list[str]
    has_almanac: bool
    zone: str = ""  # the hardiness zone row, if any: "8b"
    frost_note: str = ""  # where the frost dates came from, from the frost rows' notes

    def as_dict(self) -> dict:
        return {"today": self.today.isoformat(), "weeks": self.weeks, "items": [i.as_dict() for i in self.items],
                "frosts": self.frosts, "needs_frost": self.needs_frost, "problems": self.problems,
                "has_almanac": self.has_almanac, "zone": self.zone, "frost_note": self.frost_note}


def rows(folder: Path, name: str) -> list[dict[str, str]] | None:
    """A record's rows by column name; None if there is no such record."""
    try:
        text = notes.read(folder, name)
    except notes.NoteError:
        return None
    header, body = notes.rows_of(text)
    header = [h.strip().lower() for h in header]
    return [{h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(header)} for r in body if any(r)]


def frost_dates(table: list[dict[str, str]], problems: list[str]) -> dict[str, tuple[int, int]]:
    out = {}
    for r in table:
        if r.get("kind", "").lower() != "frost":
            continue
        which = "last" if "last" in r.get("task", "").lower() else "first" if "first" in r.get("task", "").lower() else ""
        if not r.get("when"):
            continue  # not set yet: the tasks timed from it say so
        md = month_day(r["when"])
        if not which or md is None:
            problems.append(f"{FILE}: the frost row {r.get('task')!r} needs a date like 04-20 in its when column.")
        else:
            out[which] = md
    return out


def coming_up(folder: Path, today: datetime.date | None = None, weeks: int = WEEKS,
              fill: dict[str, str] | None = None) -> Almanac:
    """What is due now and in the next `weeks` weeks, soonest first: the
    almanac's tasks, maintenance that is due and medicines running out.
    `fill` gives frost dates ("last": "04-20") for those almanac.csv lacks,
    such as a look-up from the climate table."""
    today = today or datetime.date.today()
    horizon = today + datetime.timedelta(weeks=weeks)
    problems: list[str] = []
    items: list[Item] = []
    table = rows(folder, FILE)
    frosts = frost_dates(table or [], problems)
    for which, md in (fill or {}).items():
        if which not in frosts and md and month_day(md):
            frosts[which] = month_day(md)
    needs: dict[str, int] = {}
    for r in table or []:
        if r.get("kind", "").lower() in ("frost", "zone") or not r.get("task"):
            continue
        try:
            when = parse_when(r.get("when", ""))
        except WhenError as e:
            problems.append(f"{FILE}, {r['task']}: {e}")
            continue
        if when is None:
            continue
        if when.frost and when.frost not in frosts:
            needs[when.frost] = needs.get(when.frost, 0) + 1
            continue
        for start, end in when.windows(today.year, frosts):
            if end < today or start > horizon:
                continue
            status = "now" if start <= today else "soon"
            items.append(Item(r["task"], r.get("kind", "") or "other", status, max((start - today).days, 0),
                              start, end, r.get("when", ""), r.get("notes", ""), FILE))
            break
    items += dated(folder, "maintenance.csv", "last_done", "every_days", "maintenance", today, horizon, problems,
                   lambda r: f"{r.get('machine', '')}: {r.get('task', '')}".strip(": "))
    items += dated(folder, "medicines.csv", "expires", None, "medicines", today, horizon, problems,
                   lambda r: f"{r.get('name', '')} {r.get('strength', '')}".strip() + " expires")
    order = {"overdue": 0, "now": 1, "soon": 2}
    items.sort(key=lambda i: (order[i.status], i.days, i.task.lower()))
    shown = {k: f"{v[0]:02d}-{v[1]:02d}" for k, v in frosts.items()}
    zone = next((r.get("when", "") for r in table or [] if r.get("kind", "").lower() == "zone"), "")
    note = next((r.get("notes", "") for r in table or [] if r.get("kind", "").lower() == "frost" and r.get("notes")), "")
    return Almanac(today, weeks, items, shown, needs, problems, table is not None, zone, note)


def dated(folder: Path, name: str, column: str, every: str | None, kind: str, today, horizon, problems,
          title) -> list[Item]:
    """The rows of maintenance.csv or medicines.csv that fall due by the
    horizon, or are overdue."""
    try:
        text = notes.read(folder, name)
    except notes.NoteError:
        return []
    try:
        due, _ = notes.dates_due(text, column, every, today)
    except notes.NoteError as e:
        problems.append(f"{name}: {e}")
        return []
    out = []
    for d in due:
        if d.due > horizon:
            break
        status = "overdue" if d.days < 0 else "now" if d.days == 0 else "soon"
        when = (f"every {d.row.get(every)} days, last {d.row.get(column)}" if every else d.row.get(column, ""))
        out.append(Item(title(d.row), kind, status, d.days, d.due, d.due, when, d.row.get("notes", ""), name))
    return out


def set_frosts(folder: Path, last: str, first: str, zone: str = "", note: str = "") -> None:
    """Write this place's average frost dates into almanac.csv (starting it
    if there is none), as month-day. Either may be left empty. A hardiness
    zone ("8b") goes in a row of kind zone, and `note` (where the dates came
    from) in the frost rows' notes. Dates typed by hand clear a note from an
    earlier look-up, which no longer says where they came from."""
    import csv
    import io
    told = {}
    for which, text in (("last", last), ("first", first)):
        text = (text or "").strip()
        if not text:
            told[which] = ""
            continue
        md = month_day(text)
        if md is None:
            raise WhenError(f"{text!r} is not a date like 04-20 or 20 April.")
        told[which] = f"{md[0]:02d}-{md[1]:02d}"
    notes.start_record(folder, FILE)
    header, body = notes.rows_of(notes.read(folder, FILE))
    cols = [h.strip().lower() for h in header]
    if not {"task", "kind", "when"} <= set(cols):
        raise WhenError(f"{FILE} needs the columns task, kind and when.")
    t, k, w = cols.index("task"), cols.index("kind"), cols.index("when")
    n = cols.index("notes") if "notes" in cols else None
    zone = zone.strip()
    if zone and not re.fullmatch(r"\d{1,2}[ab]?", zone):
        raise WhenError(f"{zone!r} is not a hardiness zone like 8b.")
    wanted = [(f"{which} frost", "frost", which, value) for which, value in told.items()]
    if zone:
        wanted.append(("hardiness zone", "zone", "zone", zone))
    for at, (task, kind, word, value) in enumerate(wanted):
        for r in body:
            r += [""] * (len(cols) - len(r))
            if r[k].strip().lower() == kind and word in r[t].lower():
                break
        else:
            r = [""] * len(cols)
            r[t], r[k] = task, kind
            body.insert(at, r)
        r[w] = value
        if n is not None and (note.strip() or r[n].startswith(LOOKED_UP)):
            r[n] = note.strip()
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows([header, *body])
    notes.write(folder, FILE, out.getvalue())
