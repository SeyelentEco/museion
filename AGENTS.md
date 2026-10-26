# Working on Museion

Notes for whoever changes this program, person or coding agent. Read this
first; it is short so it fits in a small model's context.

Museion is an offline reference library with an assistant. It searches a
library of archives and PDFs and answers questions with citations, using a
language model running on this computer. It is written in plain Python so it
can be changed with no internet, years after the last download.

## Rules

- **Standard library only.** There is no internet to install packages from.
  Never add `import` of anything outside Python's standard library, and never
  run `pip`.
- **Keep files short.** Most are under 400 lines so each can be read whole;
  split a file rather than let it grow much past that.
- **Comments say why, not what.** Many explain a mistake that was made once;
  keep them when you change the code around them.
- **Run the tests after every change** (below). If a test fails, fix the code
  or explain why the test is wrong before changing the test.
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

## Where things are

| You want to change | Look in |
| --- | --- |
| What the assistant is told, how many passages it reads, answer length | `museion/tuning.toml` (defaults); a `tuning.toml` beside `config.json` overrides it, no restart needed |
| How a question is answered: searching, citing, the calculator | `museion/ask.py`, `model_text.py`, `calc.py` |
| The search index | `museion/index.py` (search), `indexer.py` (building it), `pdf.py`, `kiwix.py` |
| The page in the browser | `museion/ui/` (`index.html`, `styles.css`, one `.js` file per tab); reload the page to see a change |
| What the page can ask of the program | `museion/app.py` (the list `COMMANDS`), `commands.py`, `services.py`, `library.py`, `drives.py` |
| Settings and where they are saved | `museion/config.py`, `appstate.py` |
| Starting and stopping the helper programs | `museion/sidecar.py`, `services.py` |
| What can be downloaded, with checksums | `catalog/`, `models/`, `tools/`, `drive/` (`.toml` files), read by `museion/catalog.py` |
| Running from a drive, preparing one | `museion/portable.py`, `prepare.py`, `manifest.py`, `launchers.py` |
| This editing set-up | `museion/edit.py` |
| The benchmark of questions and expected answers | `evals/benchmark.toml`, `museion/evals.py` |
| The command line | `museion/cli.py`, `drive_cli.py` |

The `.toml` files are what this program reads. In the source repository they
are generated from the `.yaml` beside them, for the older Rust app; on a
drive, change the `.toml`.

`docs/` explains the design: `retrieval.md` (how search works and why),
`python.md` (this Python version), `portable.md` (drives), `evals.md`.

## How it fits together

`python -m museion` runs `museion/app.py`, a web server on 127.0.0.1 only,
and opens the page. The page calls `POST /api/<command>`; each command is a
Python function taking the app state and the page's arguments. Progress comes
back on one event stream.

Three helper programs do the heavy work, started and stopped by
`sidecar.py`: kiwix-serve serves the archives, one llama-server runs the chat
model and another the reranker. A question goes through `ask.py`: search the
index, rerank the passages, give the best to the chat model with tools for
searching more and calculating, then check the answer's citations.

## Undoing changes

On a drive, `support/museion-app-original.zip` holds this folder as it was
when the drive was prepared. Unzipping it over `app/` undoes every change.
