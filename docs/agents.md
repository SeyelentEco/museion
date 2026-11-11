# Agents

An agent is a goal, a prompt and a set of tools, run by one harness
(`museion/harness.py`) on the chat model Museion already runs. The librarian
that answers questions on the Ask tab is one of them. Each one is a folder of
plain files, so it can be read, changed and tested with no internet, by a
person or by a coding agent on the local model.

## Where they are

| What | Where |
| --- | --- |
| The agents Museion ships | `agents/<id>/`: `agent.toml` (settings), `prompt.md` (what it is told), `cases.toml` (its tests) |
| Rules every agent is told first | `agents/_rules.md` |
| The person's own agents, and their changes to shipped ones | `agents/<id>/` beside `config.json` (the Agents tab writes there; the shipped folder is never changed) |
| Tools | `museion/agent_tools/<name>.py`, one tool per file; files starting with `_` are helpers, not tools |
| An agent's own folder (its files, `notes.md`, run logs, test results) | `agent-work/<id>/` in the library folder |
| The person's notes and records, which agents read and (behind Allow / Deny) write | `notes/` in the library folder ([notes.md](notes.md)) |
| Chats | `chats/<chat id>.json` in the library folder |

## The agents Museion ships

| Agent | What it does | Smallest tier |
| --- | --- | --- |
| `librarian` | Answers a question from the library, citing the pages (the Ask tab) | Modest |
| `first-steps` | Says what to do first after a disaster for this household and what it has: reads the records, works out what runs out first, asks the agents below for their part, saves a plan by today / this week / this month | Capable |
| `water-planner` | How much water a household needs and how to make it safe | Capable |
| `stores-keeper` | Keeps `stores.csv` in the notes; how long things last, what expires soon | Capable |
| `clinic` | The sick and injured, from the library's medical books (danger signs first, doses only from a cited passage); keeps the health log and medicine list | Capable |
| `power-keeper` | What the power you have can run, how long fuel lasts, what servicing is due (`power.csv`, `maintenance.csv`) | Capable |
| `garden-planner` | A food garden: what, when, spacing and seed; keeps `garden-plan.md` | Capable |
| `repair-helper` | Fixing a tool or machine step by step from the repair guides | Capable |
| `ration-scaler` | Scales a recipe or ration to a number of people | Modest |
| `study-guide` | Turns a topic into a numbered lesson with sources | Capable |
| `library-curator` | Adds archives found in the library folder to the catalogue | Capable |
| `radio-operator` | Reads radio messages and sends replies you allow; needs the optional radio to send ([radio.md](radio.md)) | Capable |

## The settings (`agent.toml`)

| Setting | What it does |
| --- | --- |
| `name`, `goal` | Shown in the page; the goal is one sentence |
| `min_tier` | The smallest computer it is offered on: `Modest`, `Capable`, `Generous`, `Ample` (see `docs/hardware.md`) |
| `tools` | Tool names it may use |
| `guards` | Checks on its answers: `calculate_first`, `call_again`, `search_first` (an answer given without searching or reading any records is sent back to search), `cite_or_stand` (`museion/guards.py`). An agent with `ask_person` also gets `ask_by_card`, which sends a question written in the answer back to be asked with the tool |
| `max_rounds` | Most tool rounds before it must answer |
| `max_calls` | How many times one answer may call a tool, like `{ search_library = 4 }`; then the tool is taken away. `ask_person` allows 2 unless this says otherwise |
| `answer_tokens` | Most tokens in one model turn |
| `max_history`, `history_chars` | How many earlier exchanges of the chat it sees, and how long each may be |
| `compact`, `keep_rounds` | Fold old tool results into one-line stubs when the context fills, keeping the last rounds whole (`museion/compaction.py`) |
| `memory` | `agent`: one folder for all its chats; `chat`: a folder per chat; `none`: no folder |
| `confirm` | `ask`: the person allows anything that changes the world outside its folder; `all`: also its own files |
| `enabled` | Offered when starting a chat |
| `examples` | Shown in an empty chat |
| `cases` | Its test file, `cases.toml` |

### Different computers and models

Settings are layered, each layer replacing what it names:

1. the top of `agent.toml`;
2. `[tier.<Tier>]` for the tier this computer is;
3. `[model."<model>"]` for the chat model that is running: the key is the
   catalogue's model id or part of its file name, ignoring case (the longest
   match wins);
4. the person's copy beside `config.json`, which is layered the same way.

A `prompt.<Tier>.md` beside `prompt.md` replaces it on that tier, for a
smaller model that needs shorter or plainer instructions.

`python -m museion agent check <id> --tier Modest` prints what an agent
resolves to on another computer.

## Tools

A tool is a file in `museion/agent_tools/` that defines `TOOL`, with a name,
a description the model reads, its parameters (JSON schema) and a `risk`:

- `read`: reads only (the library, its own files);
- `workspace`: writes only inside the agent's own folder (with `.history/`
  copies of what it replaced);
- `ask`: changes something else, so the person is shown an Allow / Deny card
  first, every time.

`python -m museion agent new-tool <name> --risk read` writes a tool and its
test from the template. A tool gets `run` (the agent, its folder, the
library) and the model's arguments, and returns text; it raises `ToolError`
with a message the model can act on.

## Sub-agents

An agent with the `delegate` tool hands one task to another agent, which runs
with a fresh, empty context and returns a short answer with its sources; the
sources are renumbered into the caller's. Only one level: a sub-agent cannot
delegate again.

## Testing an agent

Each case in `cases.toml` is a prompt and what must be true of the run.
Beyond the benchmark's checks (`expect_any`, `must_not_contain`,
`expect_number`, `expect_basis`, see `docs/evals.md`) an agent case can use:

| Check | Meaning |
| --- | --- |
| `expect_tool = ["calculate"]` | these tools ran |
| `max_rounds_used = 4` | it answered within this many rounds |
| `expect_file = "stores.csv"` | it wrote this file in its folder (`notes/stores.csv`: in the notes) |
| `given_files = { "stores.csv" = "..." }` | files put in its folder before the case runs (`notes/<name>`: in the notes) |
| `person_says = ["We are 3."]` | answers to its questions, in order |
| `approve = "allow"` | press Allow on its Allow / Deny cards (Deny otherwise) |

Cases run in a scratch folder, with a scratch notes folder, so they never touch the agent's real files or the person's notes.
Run them with the agent's Test button, or
`python -m museion agent test <id> --library <library folder>` while the
services are running (open the page to start them); results are kept in
`agent-work/<id>/tests/`, with each case's run log (every tool call, what it
was given and what came back) in `tests/logs/`, and the Agents tab shows what
changed since the last run. Any run in the Agents tab can be saved as a new case. To test against a
library that must not be written to (a copy on a read-only drive, someone
else's), add `--work <folder>`: what agents write, test results included, goes
there instead.

## Making an agent

From the page: Agents › New agent, or Duplicate on an existing one. From the
command line: `python -m museion agent new <id> --tools search_library,calculate`.
Then write its `prompt.md`, add cases, and run `agent check` and `agent test`.
