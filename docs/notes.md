# Your notes and records

The Notes tab holds what you write down yourself: what you learn, what you
have and what worked. Museion searches your notes alongside the library, and
the agents read and keep the records for you. Nothing here is shared with
anyone; it is one person's (or one household's) notebook.

## Where they are

Plain files in the `notes` folder of the library (`<library>/notes/`), so any
text editor opens them too and they survive Museion itself:

- **Notes** are Markdown (`.md`) or text (`.txt`): a repair you worked out, a
  remedy, which seeds grew, a neighbour's skills.
- **Records** are tables (`.csv`) with fixed columns. New record… in the Notes
  tab starts one with the right header:

| Record | Columns | Kept by |
| --- | --- | --- |
| `stores.csv` | item, quantity, unit, per_person_per_day, expires, where, notes | Stores keeper, First steps |
| `medicines.csv` | name, strength, form, quantity, unit, expires, for, notes | Clinic |
| `health-log.csv` | date, person, problem, signs, treatment, outcome, notes | Clinic |
| `maintenance.csv` | machine, task, every_days, last_done, notes | Power keeper |
| `power.csv` | thing, kind, watts, hours_per_day, capacity_wh, fuel_per_hour, notes | Power keeper |
| `places.csv` | name, kind, lat, lon, notes | the Map tab ([maps.md](maps.md)) |
| `people.csv` | name, skills, where, how_to_reach, notes | you |
| `household.csv` | name, age, needs, daily_medicines, notes | First steps reads it |
| `radio-messages.csv` | time, direction, from, to, channel, text | the radio ([radio.md](radio.md)) |
| `almanac.csv` | task, kind, when, notes | you; the Garden planner reads it (see below) |

The kinds are defined in `museion/records.toml`; add a line there for a new one.
Dates are written `YYYY-MM-DD`.

Every change Museion makes keeps the version before in `notes/.history/`, so
nothing an agent writes can lose what was there. The radio log only grows, so
it keeps no copies. Museion never deletes a note.

## Who writes them

Both you and the agents:

- **You**, in the Notes tab (Save, or Ctrl+S) or in any editor.
- **An agent**, with `write_note`, but only after you press **Allow** on a
  card that shows exactly what it will add or change (new lines, or a diff of
  the changed ones). Deny, and nothing is written. A note (not a record)
  that cites what the agent found, like `[2]`, gets a **Sources** list at its
  end naming those books and pages, so the numbers still mean something
  later.

Reading is free: `list_notes`, `read_note`, `check_dates` (what expires or
falls due soon, from a date column) and `almanac` (what is coming up, below)
need no permission.

## Coming up, and the almanac

The Notes tab opens on **Coming up**: what to do now and in the next six
weeks. It lists the almanac's tasks, maintenance that is due
(`maintenance.csv`: last done plus every so many days) and medicines that
run out (`medicines.csv`), with whatever is overdue first. Museion works the
dates out itself (`museion/almanac.py`), not the model.

The **almanac** (`almanac.csv`) is the year's work: planting, preserving,
animals, the house. Each task says *when* the way growers say it:

| When | Means |
| --- | --- |
| `6-8 weeks before last frost` | from 8 to 6 weeks before your average last spring frost |
| `2 weeks after last frost`, `at first frost` | the week from that day |
| `March`, `early March`, `mid March`, `late March`, `March to May` | that part of the year (early is the 1st to 10th, mid the 11th to 20th) |
| `04-15`, `15 April`, `04-15 to 05-10` | a date (the week from it) or dates |

Two rows of kind `frost` hold your average frost dates as month-day
(`last frost,frost,04-20,`). Timing from them is what makes one list fit any
climate, in either hemisphere; Coming up has a form to set them.

Starting the almanac (Coming up's button, or New record… Almanac) gives you
common rules of thumb for the garden, preserving, animals and the house
(`museion/almanac-start.csv`), each marked as such. They are general
practice, not from your library: keep them, change them to fit, or delete
them, and check timings against your seed packets and the library. Add your
own rows the same way.

### Frost dates and hardiness zones by place

If you don't know your frost dates, Museion can look them up from weather
stations near you, with no internet, once the library has a **climate
table** for your region (`<library>/climate/`). Type your town (or
`lat, lon`, or a saved place) under the frost form and press **Look up**:

> Station averages for Bellevue, Washington, US: last spring frost about
> 03-28 (1 year in 10 as late as 04-20); first autumn frost about 11-02 (1
> year in 10 as early as 10-20); about 218 frost-free days; hardiness zone
> 8b (average coldest night -7.9 °C). From 3 weather stations (...)

**Use these dates** writes them into `almanac.csv`, with the zone in a row
of kind `zone` and where they came from in the rows' notes. Dates typed by
hand afterwards replace them. The garden planner and the `almanac` tool look
your town up the same way when `almanac.csv` has no frost dates, and the
`climate` tool answers "what zone am I in?".

How it is worked out (`museion/climate.py`):

- The records are NOAA's daily weather-station records, **GHCN-Daily**,
  which are public domain, for 1991-2020. Each station needs 20 of those
  years with readings.
- A *frost* is a daily low at or below 0 °C. The frost dates are the median
  of the station's years. The "1 year in 10" dates are passed only one year
  in ten: later in spring, earlier in autumn.
- The **zone** is the USDA's: the average of each year's coldest night (zone
  1 starts at -60 °F, each zone is 10 °F wide, with halves *a* and *b*).
  It's worked out from station data the USDA's way. It is not the official
  USDA map.
- A place's estimate weighs the nearest stations (up to 3 within 60 km) by
  1/distance². When the town's height is known, 100 m of height difference
  counts like 10 km. If no station is within 60 km, the nearest one is
  used and the answer says it's only a rough guide. A place 250 km or more
  from every station is outside the table's region, and is refused.
- These are averages: a frost pocket or a sheltered garden can be a week or
  two off. Correct them once you know your own.

**Making the table** needs the internet once, on any computer:

    python -m museion climate build --region US-WA --library <library>
    python -m museion climate lookup "Bellevue, Washington" --library <library>

`--region` takes countries by their GHCN code (`US`, `UK`, `CA`, ...),
states or provinces (`US-WA CA-BC`), or `box:south,west,north,east`, several
at once. The build downloads NOAA's station list and each of the region's
stations' records, plus GeoNames' towns of 1,000 people or more (CC BY 4.0)
for finding a place by name. It keeps them in `climate/sources/`, keeping
only the daily lows, so `--offline` can make the table again with no
internet. The table and town list are a few MB at most.

## How they are searched and cited

Notes are indexed on their own (`notes/.index.db`, rebuilt by itself when a
file changes) and searched with every question, next to the library.
A Markdown note is split at its headings; a CSV at every 25 rows, each row
written out as "column: value". A passage from a note is cited as **your own
note**, and an answer that rests only on your notes is labelled *From your own
notes — not checked against the library*, because nobody checked what you
wrote. Open the citation and it opens the note.

## The agents that use them

- **First steps** reads the household, stores, medicines and power records,
  works out what runs out first (days left = quantity ÷ (people × per day)),
  asks the water planner, clinic and power keeper for their part, and saves
  a checklist plan in `first-steps-plan.md`: today, this week, this month,
  before winter. Ask it "the power is on but the phones and internet are
  down, what first?" or "the grid is down".
- **Stores keeper** keeps `stores.csv` and says how long things last and what
  expires soon.
- **Clinic** answers from the library's medical books (danger signs first,
  doses only from a passage it cites) and keeps `health-log.csv` and
  `medicines.csv`.
- **Power keeper** keeps `power.csv` and `maintenance.csv`: what the power you
  have can run, how long the fuel lasts, what servicing is due.
- **Garden planner** keeps `garden-plan.md`, and answers "what should I be
  doing in the garden now?" from the almanac (its `almanac` tool), adding
  the how-to from the library.

An agent's test cases put their files in a scratch notes folder
(`given_files = { "notes/stores.csv" = "..." }`), so a test never touches yours.
