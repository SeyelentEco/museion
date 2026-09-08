# Setting up, start to finish

Everything below needs internet. Once it is done, nothing does. Do it *before* you need it.

**The short way:** install the app and open it. On a machine that isn't set up it shows
only a welcome screen: choose three folders (library, models, helper programs), pick a
bundle, and press **Set up this machine**. It does steps 2–6 below for you, shows what it
will download and how large it is before it starts, and resumes if interrupted. When it
finishes it gets out of the way — Ask, Browse and Library are what you see from then on.
(*Run first-time set-up again* on the Library tab brings it back for a new machine.) The
rest of this page is the same thing by hand, which is also how you'd prepare a drive on
one machine to carry to another.

**Room:** the top of the Library shelf shows what the library takes on disk and what is
free on its drive. Anything that would not fit is marked with how much more it needs, and
cannot be started — Museion refuses before the first byte rather than failing at 98%.

**Adding more later:** the **Library** tab has two shelves. *In your library* is what you
have — click anything to read it. *Available to fetch* is everything else in the
catalogue, by category with sizes — click to fetch. Each is verified against its
publisher's checksum, the content server is restarted to serve it, and it is indexed, so
it is askable the moment the progress bar finishes. The same list from the command line
is `node scripts/fetch.js --list`.

**Content that isn't in the catalogue:** under **Settings → Other content folders**, add
any folder. `.zim` archives in it are served and searched like catalogue ones; loose PDFs
are indexed by page, cited like everything else, and open in your system's PDF viewer.
The catalogue is a starting point, not a boundary.

Budget: about ten minutes of your attention and, on a normal connection, half an hour of
downloading for the Go Bag plus a 4B model, or a few hours if you take the 35B MoE.

## 0. Prerequisites

- **Node** 20 or newer, for the fetch scripts.
- **Rust** stable, for building `museion` and the app. On Windows that means the MSVC C++
  Build Tools as well; on macOS, the Xcode command-line tools.
- Disk: 1 GB for the Go Bag, 3–37 GB for a model, 250+ GB if you want all of Wikipedia.

## 1. Clone and install

```bash
git clone https://github.com/SeyelentEco/museion
cd museion
npm install
cargo build --release
```

## 2. Helper programs

```bash
node scripts/fetch-tools.mjs --dest ./bin
```

Detects your GPU and fetches the matching llama.cpp build (CUDA, Vulkan, Metal or CPU),
plus kiwix-serve and pdfium. Everything is checked against pinned checksums.

## 3. Find out what your machine is

```bash
./target/release/museion hardware
```

```
  Accelerator : NVIDIA GeForce RTX 3060 (CUDA)
  Memory      : 12,288 MB
  System RAM  : 65,276 MB
  Tier        : Capable -- a sparse MoE with expert tensors offloaded to system RAM
```

The tier names a model manifest. See [hardware.md](hardware.md) for why the tiers are
what they are.

## 4. Content and models

Pick a place with room. Museion never chooses this for you.

```bash
node scripts/fetch.js go-bag --dest /path/to/library
node scripts/fetch.js qwen3-reranker-0.6b --dest /path/to/models
node scripts/fetch.js qwen3.6-35b-a3b-q4  --dest /path/to/models   # or the manifest for your tier
```

`node scripts/fetch.js --list` shows every bundle, archive and model. Downloads resume if
interrupted, and a file only gets its real name once its checksum matches.

## 5. Index

The Go Bag archives are PDF collections and need a local index; Wikipedia-style archives
do not. The indexer works out which is which. It reads content through kiwix-serve, so
start that first:

```bash
./bin/kiwix-serve --port 8090 /path/to/library/*.zim
./target/release/museion index --library /path/to/library --pdfium ./bin
```

A few minutes for the Go Bag. You will see each document go by.

## 6. Fit the model to your GPU

```bash
./target/release/museion fit --model /path/to/models/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf --fit-exe ./bin/llama-fit-params
```

Prints the `-ngl`/`-ot` split measured against your actual card. Skip this on the Modest
tier; a 4B model just fits.

## 7. Run the app

```bash
cd apps/desktop && npm install && npx tauri build
```

On Windows the installer is `target/release/bundle/nsis/Museion_<version>_x64-setup.exe`;
on macOS a `.dmg` under `bundle/dmg/`. Run the Windows installer and Museion is installed
per user (no administrator prompt), with a Start menu entry and a desktop shortcut, and
listed under Settings > Apps for uninstalling. It reads the same configuration as the exe
you ran from `target/release`, so a library and models set up there carry over. For an
unattended install, `Museion_<version>_x64-setup.exe /S`; add `/NS` to skip the shortcuts.
The installer expects the WebView2 runtime that Windows 10 and 11 ship with; on a machine
without it, it downloads the bootstrapper, which needs internet once.

Build the app with `tauri build`, not a bare `cargo build`: only the Tauri CLI compiles the web UI in. A cargo-built exe opens a
window pointed at the dev server and shows "localhost refused to connect" — and a later
`cargo build --release --workspace` will quietly overwrite a good exe with that. On first launch the Library tab asks for
the paths from steps 2 and 4. **Save**, **Start services**, **Build index** (if you skipped
step 5), **Fit model to this machine** (if you skipped step 6). Then ask.

## Iterating on it

The release build exists to make the installer. Nothing about trying a change needs it.

| you changed | do this | wait |
|---|---|---|
| the system prompt, a limit | edit `tuning.yaml` (Settings › Advanced), ask again | 0 s |
| the UI (`main.ts`, `index.html`, `styles.css`) | `scripts\dev.ps1`, save the file | ~1 s, the window reloads |
| Rust in the ask loop | `scripts\dev-eval.ps1 <case-or-subset>` | ~25 s to build, then the cases |
| Rust in the app | `scripts\dev.ps1` rebuilds and relaunches, or `scripts\dev-build.ps1 -Run` | ~25 s |
| anything, for the installer | `npm run tauri build` | ~3 min |

Measured on an i7-12700K: the LTO release build takes about 3 minutes; the first debug
build of the app 88 seconds; a debug rebuild after a Rust edit 24 seconds.

**The servers stay up between runs.** `scripts\dev.ps1` and `dev-build.ps1 -Run` set
`MUSEION_KEEP_SERVICES=1`, which starts kiwix-serve and the two llama-servers outside the
job that would otherwise end them with the app. The next launch finds them answering on
their ports, checks that the chat and reranker servers report the configured model files,
and adopts them: the window is usable in seconds and the 22 GB model loads once a day, not
once a build. Adopted servers are never killed on exit; the Stop button and a benchmark's
model swap still stop them. Without the variable, nothing changes: servers Museion
started die with it, Job Object and all.

**Prompts and limits without a build.** The system prompt, the search-rewrite prompt, the
answer length, the round limit, how many passages are kept and the reranker threshold live
in [`crates/museion-core/tuning.yaml`](../crates/museion-core/tuning.yaml), compiled in
as the defaults. Settings › Advanced creates a copy beside `config.json` and opens it; the
app reads that file before every question, so a saved edit shows in the next answer. The
command line takes the same file with `--tuning`, and `scripts\dev-eval.ps1` passes it
automatically when it exists, so what the benchmark tests is what the app would do.

**The ask loop from the command line.**

```bash
scripts\dev-eval.ps1 reason                 # a capability, a horizon, or one case id
scripts\dev-eval.ps1 bleach-drum -Label x   # the run is saved like any other
scripts\dev-eval.ps1 -Chat                  # a conversation: follow-ups keep their context
museion chat --db <library>\museion-index.db  # the same, by hand
```

Set `MUSEION_TRACE=1` to see where the seconds go in an answer: the rewrite, the seeded
search, each model turn.

**If cargo cannot find `link.exe` or the Windows SDK.** On the development machine the
SDK lives on an external disk that is registered at `E:\Dev\Windows10SDK`; when that disk
comes up under another letter every C build script fails. `scripts\vsenv.ps1` loads the
Visual Studio environment with the SDK paths pointed at wherever the disk is, and the dev
scripts dot-source it when present. It is a workaround for that machine, not a build
step: on any machine with the SDK where it was installed, the scripts work without it.

## Or skip the app

Everything the app does, the command line does:

```bash
./target/release/museion ask --db /path/to/library/museion-index.db "how do I purify water without chemicals"
```

with `llama-server` running on 8080 for the chat model and 8081 for the reranker — see
`models/*.yaml` for the arguments each one wants.

## Before you rely on it

Pull the network cable and ask it something. That is the only test that validates the
premise, and it is worth doing once while fixing anything is still easy. Then re-run
`node scripts/verify.js --dest /path/to/library` every few months; drives in cupboards
rot quietly. See [usb-prep.md](usb-prep.md) for carrying a finished library to a machine
that has no connection at all.
