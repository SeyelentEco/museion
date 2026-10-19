# The Python rewrite

Museion is being rewritten from Rust and TypeScript into plain Python, so that
someone who has been offline for a long time can change it themselves with a
local coding agent (opencode on the same local model Museion runs). The Rust
app keeps working until the Python one matches it on the benchmark.

The rules the Python code keeps:

- **Standard library only.** Nothing to `pip install`, so it runs on the
  portable Python carried on the drive with no network.
- **The heavy work stays native.** llama-server, kiwix-serve, pdfium and SQLite
  FTS5 are the same pinned programs as before; Python only drives them.
- **One concern per file, each short enough for a local model to read whole.**
  Each module keeps the name of the Rust module it replaces (`pdf.py` for
  `pdf.rs`), and the comments that explain *why* come across with the code.

## Status

| Phase | What | State |
| --- | --- | --- |
| 0 | Prove the portable runtime: FTS5, pdfium through ctypes, starting and stopping llama-server, helpers dying with Museion | Done: `museion/pdf.py`, `sidecar.py`, `llama.py`, `doctor.py` |
| 1 | Port the core as a command line: index, search, ask, evals | Not started |
| 2 | Browser UI in plain HTML and JavaScript | Not started |
| 3 | The drive: portable mode, prepare, `AGENTS.md`, opencode | Not started |
| 4 | Rehearse offline edits with a local coding agent | Not started |
| 5 | Retire the Rust code | Not started |

## Running it

Museion needs Python 3.11 or newer. On the drive it is the portable build from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
(`install_only_stripped`, 16 to 33 MB to download depending on the platform).

Check that everything works on this machine:

```bash
python -m museion.doctor --pdfium bin --llama-server bin/llama-server
```

Without `--model` the llama-server checks use a tiny model written on the spot
by `museion/tiny_gguf.py`, so they take seconds. Add `--model` with a real chat
model, and `--reranker` with the reranker, to check those load too.

Run the tests (offline, a few seconds):

```bash
python -m unittest discover -s tests -v
```

Tests that need pdfium or llama-server run when `MUSEION_PDFIUM` (the folder
holding the pdfium library) and `MUSEION_LLAMA_SERVER` (the program) are set,
and are skipped otherwise. CI runs everything on Windows, macOS and Linux with
the portable Python (`.github/workflows/python.yml`).

## What phase 0 found

- The portable Python's `sqlite3` has FTS5 (SQLite 3.50.4).
- pdfium works through ctypes with no compiled glue; a few dozen lines declare
  the eleven functions Museion uses.
- A helper dies when Museion is killed outright on Windows (a Job Object, as
  the Rust version did) and on Linux (`PR_SET_PDEATHSIG`, which the Rust
  version did not have). On Linux the signal follows the *thread* that started
  the helper, so every helper is started from one long-lived thread. macOS has
  no equivalent; a normal quit still stops the helpers there.
