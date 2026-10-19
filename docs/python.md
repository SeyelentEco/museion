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
| 1 | Port the core as a command line: index, search, ask, evals | Done: `python -m museion` (see below); asks exactly as the Rust version does |
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

The command line does what the Rust `museion` program did, with the same
commands and options:

```bash
python -m museion index  --library E:/museion-library --pdfium bin
python -m museion status --library E:/museion-library
python -m museion search --library E:/museion-library how do I purify water
python -m museion gather --library E:/museion-library how do I purify water
python -m museion ask    --library E:/museion-library how much bleach for 55 gallons
python -m museion chat   --library E:/museion-library
python -m museion eval   --library E:/museion-library --label "qwen3.6 q4, reranker"
python -m museion hardware
python -m museion tools  --dest bin
```

`index` needs kiwix-serve running over the library; `ask`, `chat` and `eval`
need llama-server running the chat model (and, if it is running, use the
reranker). Each command's `--help` lists its options. `prepare` (building a
drive) comes in phase 3.

Run the tests (offline, a few seconds):

```bash
python -m unittest discover -s tests -v
```

Tests that need pdfium or llama-server run when `MUSEION_PDFIUM` (the folder
holding the pdfium library) and `MUSEION_LLAMA_SERVER` (the program) are set,
and are skipped otherwise. CI runs everything on Windows, macOS and Linux with
the portable Python (`.github/workflows/python.yml`).

## The data files

The catalogue (`catalog/`, `models/`, `tools/`), the benchmark
(`evals/benchmark.toml`) and the prompts and limits (`museion/tuning.toml`)
are TOML, which Python reads with no library. Until the Rust app is retired
they are generated from the YAML files beside them, which the Rust app still
reads: edit the YAML, then run `node scripts/yaml-to-toml.mjs` (CI runs it with
`--check` and fails if the two disagree). In phase 5 the TOML becomes the
source and the YAML goes.

## Keeping it the same as the Rust version

Until the Rust code is retired, two checks hold the Python port to it:

- `tests/test_parity_rust.py` asks the same question with both command lines,
  against the same index and the same scripted model, and requires the
  requests sent to the model and the printed answers to be identical. It runs
  when `MUSEION_RUST` names the Rust program, and in CI.
- `tests/test_index.py` opens an index the Rust indexer built
  (`tests/fixtures/rust-index.db`) and requires the same search results, in the
  same order; `tests/index_corpus.py` rebuilds that fixture.

Where the Python version deliberately differs:

- **Citations.** A passage was identified by its book and page, so two PDFs in
  one archive that both had a page 2 shared a citation number, and a citation
  of the second named the first. The Rust version is fixed in the same change,
  so the two still match.
- **Windows GPU detection** reads the display adapters from the registry
  rather than `wmic`, which current Windows 11 no longer has. Cards over 4 GB
  now report their real memory.
- **Unpacking** uses Python's own `zipfile` and `tarfile` rather than the
  system's `tar`, and refuses archive entries that would land outside the
  folder.
- **A misspelt benchmark check** (`expect_al = [...]`) is an error rather than
  silently ignored.
- **Helper programs** get a timeout when Museion only asks them a question
  (`nvidia-smi`, `sysctl`), so a hung driver cannot hang the hardware check.

Not done yet:

- **Linux helpers.** The catalogue has no Linux builds of llama.cpp or
  kiwix-tools, so `tools` installs nothing on Linux, as with the Rust version.
- **`museion/tuning.toml`'s header** still describes the Rust app's
  `tuning.yaml` override, because it is generated from that file. It is
  rewritten when the TOML becomes the source in phase 5.

## What phase 0 found

- The portable Python's `sqlite3` has FTS5 (SQLite 3.50.4).
- pdfium works through ctypes with no compiled glue; a few dozen lines declare
  the eleven functions Museion uses.
- A helper dies when Museion is killed outright on Windows (a Job Object, as
  the Rust version did) and on Linux (`PR_SET_PDEATHSIG`, which the Rust
  version did not have). On Linux the signal follows the *thread* that started
  the helper, so every helper is started from one long-lived thread. macOS has
  no equivalent; a normal quit still stops the helpers there.
