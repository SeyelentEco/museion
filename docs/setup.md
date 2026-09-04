# Setting up, start to finish

Everything below needs internet. Once it is done, nothing does. Do it *before* you need it.

**The short way:** install the app, open the Library tab, choose three folders (library,
models, helper programs), pick a bundle, and press **Fetch everything**. It does steps 2–6
below for you, shows what it will download and how large it is before it starts, and
resumes if interrupted. The rest of this page is the same thing by hand, which is also
how you'd prepare a drive on one machine to carry to another.

**Adding more later:** the **Catalogue** on the same tab lists every archive Museion knows
how to fetch, by category, with its size, what it is, which bundles include it, and
whether you already have it. Press **Fetch** on any item. It is verified against its
publisher's checksum, the content server is restarted to serve it, and it is indexed —
so it is askable the moment the progress bar finishes. The same list from the command
line is `node scripts/fetch.js --list`.

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

The installer lands in `target/release/bundle/`. Build the app with `tauri build`, not a
bare `cargo build`: only the Tauri CLI compiles the web UI in. A cargo-built exe opens a
window pointed at the dev server and shows "localhost refused to connect" — and a later
`cargo build --release --workspace` will quietly overwrite a good exe with that. On first launch the Library tab asks for
the paths from steps 2 and 4. **Save**, **Start services**, **Build index** (if you skipped
step 5), **Fit model to this machine** (if you skipped step 6). Then ask.

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
