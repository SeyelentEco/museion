# Working on Museion

Notes for whoever changes this program, person or coding agent. Read this
first; it is short so it fits in a small model's context.

Museion is an offline reference library with an assistant. It searches a
library of archives and PDFs and answers questions with citations, using a
language model running on this computer. It is written in plain Python so it
can be changed with no internet, years after the last download.

## Rules

- **There is no internet.** Never fetch a web page or URL (`curl`,
  `Invoke-WebRequest`) to look something up: it fails or hangs. What Museion
  knows is in this folder and the library.
- **Standard library only.** Never add `import` of anything outside Python's
  standard library, and never run `pip`.
- **Keep files short.** Most are under 400 lines so each can be read whole;
  split a file rather than let it grow much past that.
- **Comments say why, not what.** Many explain a mistake that was made once;
  keep them when you change the code around them.
- **Run the tests after every change** (below), a `.toml` change too. If a
  test fails, fix the code or explain why the test is wrong. Never change a
  test's stand-in objects to make it pass: check the real class first.
- **Finish by saying what the person must do** for the change to show: reload
  the page, restart Museion, or rebuild the index (below).
- **Change only files under this folder (`app/` on a drive).** Never delete
  or move anything in `library/` or `models/`: those are the content and the
  models, gigabytes that cannot be downloaded again.

## Running and checking

Run these from this folder. `python` is the Python Museion runs on; on a drive
it is the one in `../python/`, which "Edit Museion" puts first on the PATH. On
Windows the shell is PowerShell 5.1: run commands one at a time, or join them
with `;`, since `&&` does not work there.

```
python -m museion                       open Museion in the browser
python -m unittest discover -s tests    run the tests (offline, about a minute)
python -m museion --help                every command
python -m museion.doctor --help         check the helper programs work
```

A test that needs a helper program skips itself when the program is not
configured; that is not a failure.

**After changing how the index is built** (`indexer.py`, `index.py`,
`pdf.py`, `kiwix.py`), the index on disk still holds the old rows. Tell the
person to open Museion and press Settings › Rebuild index (Build index skips
what is already indexed).

## Adding an archive to the catalogue

Do not write the file by hand: this command measures the archive and writes
the entry, with nothing in it that cannot be known offline.

```
python -m museion add-source ../library/<file>.zim --title "<title>" --category <shelf>
```

The title is what the Library tab shows: the archive's own title, not an
invented one (if the file name does not make it plain, ask). The shelf is one
of `survival`, `medicine`, `making`, `reference`. It shows when the Library
tab is reloaded; no bundle or index change is needed.

## Changing an agent

Agents (the librarian, the stores keeper, first steps...) are folders
in `agents/`: `agent.toml` (settings), `prompt.md` (what it is told),
`cases.toml` (its tests). `docs/agents.md` explains every setting. To change
what an agent does, change its `prompt.md` or `agent.toml`; to add one, run
`python -m museion agent new <id>`; to add a tool, run
`python -m museion agent new-tool <name>`. Then run:

```
python -m museion agent check            every agent and tool loads
python -m museion agent test <id>        its cases (needs the services running)
```

and the unit tests. A change shows in a new chat; no restart is needed.

## Where things are

| You want to change | Look in |
| --- | --- |
| What the assistant is told, how many passages it reads, answer length | `museion/tuning.toml` (defaults); a `tuning.toml` beside `config.json` overrides it, no restart needed |
| How a question is answered: searching, citing, the calculator | `museion/ask.py`, `model_text.py`, `calc.py` |
| The search index | `museion/index.py` (search), `indexer.py` (building it), `pdf.py`, `kiwix.py` |
| The page in the browser | `museion/ui/` (`index.html`, `styles.css`, one `.js` file per tab); reload the page to see a change |
| What the page can ask of the program | `museion/app.py` (the list `COMMANDS`), `commands.py`, `services.py`, `library.py`, `drives.py` |
| First-run set-up (the wizard) and the model chooser | `museion/ui/welcome.js` (the steps), `ui/models.js` (the chooser, also in Settings), `museion/model_choice.py` (what fits, what each model runs), `own_programs.py` (the Programs step, where the catalogue has no helper builds for a system); the words about each model are in `models/*.toml` |
| Settings and where they are saved | `museion/config.py`, `appstate.py` |
| Starting and stopping the helper programs | `museion/sidecar.py`, `services.py` |
| What can be downloaded, with checksums | `catalog/`, `models/`, `tools/`, `drive/` (`.toml` files), read by `museion/catalog.py`; `catalog_add.py` adds an archive (`catalog_sync.py` does it from a Kiwix link, with internet) |
| Running from a drive, preparing one | `museion/portable.py`, `prepare.py`, `manifest.py`, `launchers.py` |
| Upgrading a copy from a newer one, and carrying the person's code changes across | `museion/upgrade.py`, `switch.py`, `appmerge.py`, `merge3.py`, `upgrade_cli.py`, `upgrade_commands.py`, `newer.py` (asking for the latest release), `ui/upgrade.js`; see `docs/upgrading.md` |
| This editing set-up | `museion/edit.py` |
| The benchmark of questions and expected answers | `evals/benchmark.toml`, `museion/evals.py`; retrieval alone: `evals/retrieval.toml`, `retrieval_eval.py` |
| Agents: the harness, its tools, chats | `agents/` (one folder each), `museion/harness.py`, `agent_tools/` (one tool per file), `agents.py` (settings), `chat_commands.py`, `agent_commands.py`; see `docs/agents.md` |
| The command line | `museion/cli.py`, `catalog_cli.py`, `drive_cli.py`, `map_cli.py` |
| The person's notes and records (the Notes tab), and their kinds | `museion/notes.py`, `records.toml`, `notes_commands.py`, `agent_tools/write_note.py` and the other note tools; see `docs/notes.md` |
| Coming up and the almanac (the Notes tab's first page) | `museion/almanac.py` (reading `when`, what is due), `almanac-start.csv` (the starter tasks), `agent_tools/almanac.py`, `ui/notes.js`; see `docs/notes.md` |
| Frost dates and zones by place (the climate table) | `museion/climate.py` (station statistics, the build, finding a place, the estimate), `climate_cli.py`, `agent_tools/climate.py`, the look-up in `ui/notes.js`; see `docs/notes.md` |
| Your home area (Settings and first-run set-up) | `museion/home_commands.py` (set up: home in places.csv, the climate table around it, the almanac's frost dates, a map), `maps.home`/`set_home`, `ui/home.js`; see `docs/notes.md` |
| Offline maps and places (the Map tab) | `museion/maps.py`, `map_commands.py`, `ui/map.js`; see `docs/maps.md` |
| Making a map from OpenStreetMap, and drawing it | `museion/osm.py` (download), `mapmaker.py` (tiles), `mvt.py` and `protobuf.py` (the tile format), `map_cli.py` (`make-map`); in the page `ui/mvt.js` (reading tiles), `ui/mapdraw.js` (the map's style and labels) |
| The optional Meshtastic radio (the Radio tab) | `museion/radio.py`, `radio_commands.py`, `ui/radio.js`; see `docs/radio.md` |

`docs/` explains the design: `retrieval.md` (how search works and why),
`python.md` (how this Python version works), `portable.md` (drives),
`upgrading.md` (putting a newer version into this copy),
`evals.md`, `adding-content.md`, `agents.md`. Comments that say what "the Rust version" did
describe the program Museion was before it was Python, kept on purpose: the
indexes and settings it wrote still work.

## How it fits together

`python -m museion` runs `museion/app.py`, a web server on 127.0.0.1 only,
and opens the page. The page calls `POST /api/<command>`; each command is a
Python function taking the app state and the page's arguments. Progress comes
back on one event stream.

Three helper programs do the heavy work, started and stopped by
`sidecar.py`: kiwix-serve serves the archives, one llama-server runs the chat
model and another the reranker. A question goes through `ask.py`: search the
index, rerank the passages, give the best to the chat model with tools for
searching more and calculating, then check the answer's citations. The model
and its tools are driven by `harness.py`, the same loop every agent runs on.

## After an upgrade

If `UPGRADE-NOTES.md` is in this folder, an upgrade could not carry some of
the person's changes into the new version. It lists each file, why, and where
their version and a diff of their change were kept (`../my-changes/`). To put
a change back: read the diff, make the same change to the new file (do not
copy their old file over the new one: that would undo the upgrade there), run
the tests, and remove that entry from the notes. Delete the notes file when no
entries are left. `python -m museion changes` lists everything that differs
from the code as shipped.

## Undoing changes

On a drive, `support/museion-app-original.zip` holds this folder as it was
when the drive was prepared. Its entries start with `app/`, so unzip it into
the drive's top folder (not into `app/`), replacing what is there; that undoes
every change to a file it holds. Files added since stay, and can be deleted.
