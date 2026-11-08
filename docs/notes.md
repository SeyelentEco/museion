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
  the changed ones). Deny, and nothing is written.

Reading is free: `list_notes`, `read_note` and `check_dates` (what expires or
falls due soon, from a date column) need no permission.

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
- **Garden planner** keeps `garden-plan.md`.

An agent's test cases put their files in a scratch notes folder
(`given_files = { "notes/stores.csv" = "..." }`), so a test never touches yours.
