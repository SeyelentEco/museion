# How the code works

Museion is plain Python, so that someone who has been offline for a long time
can change it themselves with a local coding agent (opencode on the same local
model Museion runs). This page says how the code works, and what testing it on
real machines found.

The rules the code keeps:

- **Standard library only.** Nothing to `pip install`, so it runs on the
  portable Python carried on the drive with no network.
- **The heavy work stays native.** llama-server, kiwix-serve, pdfium and SQLite
  FTS5 are pinned programs; Python only drives them.
- **One concern per file, each short enough for a local model to read whole,**
  with comments that explain *why*.

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

Open Museion:

```bash
python -m museion
```

That starts Museion and opens it in the default browser at
`http://127.0.0.1:8070/`: Ask, Agents, Notes, Map, Library, Evals, Settings,
and the first-run set-up. See [The browser page](#the-browser-page) for how it
works.

The command line does the same jobs without the page:

```bash
python -m museion index  --library E:/museion-library --pdfium bin
python -m museion status --library E:/museion-library
python -m museion search --library E:/museion-library how do I purify water
python -m museion gather --library E:/museion-library how do I purify water
python -m museion ask    --library E:/museion-library how much bleach for 55 gallons
python -m museion chat   --library E:/museion-library
python -m museion eval   --library E:/museion-library --label "qwen3.6 q4, reranker"
python -m museion hardware
python -m museion retrieval-eval --library E:/museion-library
python -m museion tools  --dest bin
python -m museion fetch  go-bag --dest E:/museion-library
python -m museion verify --library E:/museion-library
python -m museion add-source E:/museion-library/<file>.zim --title "..." --category reference
python -m museion sync-source <kiwix url> --category survival
python -m museion check-sources
python -m museion prepare --dest E:/ --models qwen3-4b-instruct-q4
python -m museion verify --drive E:/
python -m museion edit
```

`index` needs kiwix-serve running over the library; `ask`, `chat` and `eval`
need llama-server running the chat model (and, if it is running, use the
reranker). `sync-source` and `check-sources` need the internet; everything
else that downloads says so. Each command's `--help` lists its options.
`prepare`, `verify --drive` and `edit` are described in
[The drive](#the-drive-and-changing-museion); the catalogue commands in
[adding-content.md](adding-content.md).

Run the tests (offline, a few seconds):

```bash
python -m unittest discover -s tests -v
```

Tests that need pdfium or llama-server run when `MUSEION_PDFIUM` (the folder
holding the pdfium library) and `MUSEION_LLAMA_SERVER` (the program) are set,
and are skipped otherwise. CI runs everything on Windows, macOS and Linux with
the portable Python (`.github/workflows/tests.yml`).

## The drive and changing Museion

`python -m museion prepare` (and Settings › Prepare a drive) builds a drive that
runs Museion on a computer with nothing installed and no internet:

```
<drive>/
  museion-portable, config.json, README.txt, MANIFEST.yaml
  Museion.cmd, Edit Museion.cmd     or .command on macOS, museion.sh and edit-museion.sh on Linux
  app/        Museion's code, catalogue, tests, docs and AGENTS.md
  python/     the portable Python
  library/ models/
  bin/        kiwix-serve, pdfium, opencode; bin/llama/<cuda|vulkan|cpu|metal>/
  support/    LICENSES/README.txt, museion-app-original.zip
```

Paths in `config.json` are relative to the drive, and what belongs to each
computer (the llama.cpp build, the model its tier allows, the GPU split) goes in
a profile under that computer's own app data, as `docs/portable.md` describes.
`verify` checks every archive and model against the hashes in `MANIFEST.yaml`.

`Edit Museion` runs `python -m museion edit`: it starts the chat model with a
32,768-token context (opencode's own instructions take some 12,000 tokens) and
opens [opencode](https://opencode.ai) on `app/`, set up to use only that model.
opencode reaches for the network in several places, and each is turned off,
because on a drive with no internet every one of them is a wait or a failure:

- `OPENCODE_PURE=1`: no plugins. Without it opencode tries to install its
  default plugins from npm at every start and hangs offline.
- `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_DISABLE_MODELS_FETCH`,
  `OPENCODE_DISABLE_LSP_DOWNLOAD`: no update check, no model list from
  models.dev, no language servers downloaded.
- `OPENCODE_CONFIG_CONTENT`: one provider, `museion/local`, an
  OpenAI-compatible endpoint at the chat model's port. The adapter it names is
  built into opencode, so nothing is installed for it.
- `XDG_DATA_HOME` and friends: opencode's conversations and logs go with the
  computer's Museion state, not onto the drive. `XDG_CONFIG_HOME` too, so an
  opencode installed on the computer does not lend its own settings (its MCP
  servers, say) to the drive's.
- Its explore and general sub-agents are turned off: each starts a second
  conversation with long instructions of its own, and in 32k it overflowed.
- ripgrep: opencode's search runs `rg`, and with none on the PATH it downloads
  ripgrep from GitHub at the first search, whatever the switches say. The drive
  carries `rg` in `bin/` beside opencode, and that folder goes on its PATH.

The drive's Python is put first on opencode's PATH, so the tests it runs use the
same Python as Museion. `AGENTS.md` at the top of the code is what opencode (or
a person) reads first: the rules (standard library only, short files, why-comments,
run the tests) and a map of where each kind of change is made.

What testing on a real PC (Windows, RTX 3060 12 GB) showed:

- With Qwen3.6-35B-A3B, asked to add a `--version` option, opencode edited the
  right two files, found and fixed the argument handling that swallowed the new
  option, and ran the tests green, in about four minutes. The 32k context fitted
  in about 9.5 GB of the card.
- With the small Qwen3 4B it could not: its edits named text that was not in
  the file, it rewrote a file whole, and then said the work was done. `edit`
  says so when the small model is the one configured.
- opencode's shell on Windows is PowerShell 5.1, so `AGENTS.md` warns that
  `&&` does not work there.
- A USB disk whose flushes fail made SQLite fail every write; the index is
  opened with `synchronous=OFF` in such a folder (it can always be rebuilt).
- The machine profile holds absolute paths, so a drive
  under a new letter looks for each one again under its new root rather than
  dropping it and running on the CPU.

## The browser page

`python -m museion` (or `python -m museion app`) runs `museion/app.py`, a small
web server on `127.0.0.1` only, and opens the page in the browser. The page is
plain HTML, CSS and JavaScript in `museion/ui/`, with no build step: change a
file, reload the page, and the change is there.

- **Settings** are `config.json`, in `%APPDATA%\org.museion.desktop`
  on Windows, `~/Library/Application Support/org.museion.desktop` on macOS,
  `~/.config/org.museion.desktop` on Linux. `--config-dir` points elsewhere.
- **How the page asks for things.** `POST /api/<command>` runs the Python
  function of that name (the list is `COMMANDS` in `app.py`) and answers `{"ok": result}` or `{"error": "a sentence"}`.
  Progress (the answer as it is written, downloads, indexing) arrives on one
  event stream, `GET /api/events`. `museion/ui/api.js` wraps both.
- **Only this page can use it.** Commands need a header another site's page
  cannot send, and requests must name `127.0.0.1` or `localhost` as the host,
  so a web page cannot reach Museion even through a rebound DNS name.
- **Files.** A browser page cannot see the computer's files, so the Choose…
  buttons open a folder window drawn by the page from what Museion lists. PDFs
  from extra folders are served to the reader (`/file`), and only PDFs inside
  the library's folders.
- **Stopping.** Museion stops, with its helpers, about a minute after the last
  Museion tab closes (a reload does not count), or with the Quit button, or
  Ctrl+C in its window. `--stay` keeps it running with no tab open. Starting it
  again while it runs opens the running one.
- **Options:** `--port` (default 8070; a busy port means another free one),
  `--no-browser`, `--stay`, `--config-dir`.

## The data files

The catalogue (`catalog/`, `models/`, `tools/`, `drive/`), the benchmark and
the retrieval check (`evals/`) and the prompts and limits
(`museion/tuning.toml`) are TOML, which Python reads with no library. They are
the source: edit them directly.

## What the tests hold it to

Two records in `tests/fixtures/` come from the first version of Museion, so
that what a person built with it keeps working:

- `tests/test_index.py` opens an index that version built (`old-index.db`) and
  requires the search results it gave (`old-search.json`), in the same order,
  and `tests/test_indexer.py` requires the indexer to build the same rows.
- `tests/test_calc.py` holds the calculator to recorded results, to the last
  bit.

Choices worth knowing about:

- **Citations.** A passage is identified by its file as well as its book and
  page. By book and page alone, two PDFs in one archive that both had a page 2
  shared a citation number, and a citation of the second named the first.
- **Windows GPU detection** reads the display adapters from the registry
  rather than `wmic`, which current Windows 11 no longer has. Cards over 4 GB
  report their real memory.
- **Unpacking** uses Python's own `zipfile` and `tarfile` rather than the
  system's `tar`, and refuses archive entries that would land outside the
  folder.
- **A misspelt benchmark check** (`expect_al = [...]`) is an error rather than
  silently ignored.
- **Helper programs** get a timeout when Museion only asks them a question
  (`nvidia-smi`, `sysctl`), so a hung driver cannot hang the hardware check.
- **The page** renumbers an answer's citations to match its source list once
  the answer is complete (the model's own numbers need not match the list),
  opens PDFs from extra folders in the reader at the cited page, and shows
  commands' failures as sentences in the page rather than as console errors.
- **Set-up** keeps helper programs already configured when the catalogue has
  none for this system, rather than clearing their paths.

Linux came later than the other two: the catalogue has Linux builds of llama.cpp
(Vulkan and CPU; upstream publishes no CUDA build), kiwix-tools and pdfium, and
the release has a Linux package. The graphics-card paths on Linux are tested
with stand-ins only; nobody has yet run them on a real Linux machine with a card.

## What rehearsing offline edits found

On a prepared drive on a Windows PC (RTX 3060 12 GB), with Qwen3.6-35B-A3B
chosen in Settings, opencode was given three tasks through `Edit Museion`,
each in a fresh session and asked for the way someone who does not program
would ask. The launchers ran with the proxy variables pointing at a dead port,
so anything honouring them failed at once.

| Task | First try | After the fixes below |
| --- | --- | --- |
| Add an archive copied into the library to the catalogue | Right size and checksum, but an invented title and a category the Library tab has no heading for; tests not run; 156 s | With `add-source`: right entry under Reference, nothing invented; 69 s |
| Make answers with a dose end with "check this against a second source" | Done in one rule beside the safety rules, with a test for it, tests run; 399 s | (not re-run) |
| A planted bug (author and description swapped when indexing), described only by its symptom | Found and fixed in 414 s, tests run, but never said the index must be rebuilt. Asked again, it "fixed" `ask.py` to read a field that does not exist and changed the test's stand-in to match | Fixed, tests run, and it told the person to press Rebuild index, which put the authors back; 346 s |

The benchmark on the drive was 25/29 before the tasks, 27/29 after them, and
28/29 after the re-runs (the chemistry archive from the first task answers the
steel question; the one left is the model's unit arithmetic).

The first benchmark took 3874 s and the others about 1000: the fitted split
filled the 12 GB card, and what else the desktop had open decided how much of
it spilled into shared memory. Timed directly at 32k (prompt of 3,600 tokens):

| Split | GPU | Shared | Prompt tok/s | Output tok/s |
| --- | --- | --- | --- | --- |
| Fitted with the default 1 GiB margin | 11.8 GB | 553 MB | 106 | 23 |
| Fitted with a 2 GiB margin | 11.5 GB | 261 MB | 689 | 42 |
| Fitted with a 3 GiB margin | 10.4 GB | 260 MB | 631 | 40 |
| Chosen by hand over several evenings | 10.2 GB | 267 MB | 599 | 40 |

So the fit now keeps 2 GiB free, and is measured at opencode's 32k whatever
context the model answers with, since `edit` raises it without measuring again.

What changed because of it:

- `AGENTS.md`: there is no internet; finish by saying what the person must do;
  never change a test's stand-ins to make it pass; after changing indexing,
  Rebuild index; how to add an archive; unzip the undo archive into the
  drive's top folder (its entries start with `app/`).
- `python -m museion add-source`: written by hand, a catalogue entry came back
  with comments copied from a generated file, invented download links, and a
  magnet whose hash was the start of the SHA-256. The command measures the file
  and writes only what can be known offline.
- Settings › Rebuild index: Build index skips what is indexed, so a fix to the
  indexer showed nothing and the page had no way to redo it.
- `edit`: opencode's explore and general sub-agents are off (they overflowed
  the 32k context in two of three tasks), and `XDG_CONFIG_HOME` keeps the
  computer's own opencode settings out (it had picked up an MCP server).

Still rough:

- 32k is tight: reading four whole files once overflowed the main
  conversation; opencode compacted and carried on.
- It seldom ran the tests after a `.toml` change, whatever `AGENTS.md` says.
- With an empty settings folder opencode tries, in the background, to install
  its plugin package from npm. Offline it fails and nothing waits on it.
- Asked for a title it did not know, it ran `curl` at Wikipedia. PowerShell's
  `curl` ignores the proxy variables, so the rehearsal could not stop that; on
  a computer with no network it fails.

## Releases

A release is `prepare --skeleton`, zipped, per platform, and the drive's own
Python runs the drive's own tests before it is attached. A pull request that
changes how a drive is laid out builds and tests it too.

## What proving the portable Python found

- The portable Python's `sqlite3` has FTS5 (SQLite 3.50.4).
- pdfium works through ctypes with no compiled glue; a few dozen lines declare
  the eleven functions Museion uses.
- A helper dies when Museion is killed outright on Windows (a Job Object) and
  on Linux (`PR_SET_PDEATHSIG`). On Linux the signal follows the *thread* that started
  the helper, so every helper is started from one long-lived thread. macOS has
  no equivalent; a normal quit still stops the helpers there.
