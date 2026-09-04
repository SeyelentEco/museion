# Museion

**An offline knowledge library with a local AI assistant, for when the internet is gone.**

Museion is a desktop app that keeps a curated reference library on your own disk — survival
and post-disaster guidance, medicine, practical making and repair, and general reference —
and runs a language model locally on top of it. You ask a question in plain language; it
finds the relevant pages in your library and answers **from them, with citations you can
open and read yourself**.

No internet. No account. No cloud. Nothing leaves the machine.

The assistant is called **Hypatia**, after the last great teacher of Alexandria. Museion is
the institution around her — named for the Alexandrian research establishment, which was
never merely a room full of scrolls but a place where scholars lived, taught, and answered
questions.

---

## Two things to be honest about up front

**1. Setting this up requires internet. Using it does not.**

The library is between 800 MB and 250 GB depending on how much you want. That cannot live
in a git repository. This repo ships *manifests* — verified checksums, mirrors, and torrent
links — and fetches content on first run. A preparedness tool you can only install *before*
the disaster is one most people won't have when it matters, so set it up now, and see
[docs/usb-prep.md](docs/usb-prep.md) for preparing a drive to carry to a machine that has
no connection at all.

**2. This is a reference library, not a guarantee.**

Museion does not contain everything you need to survive, and no honest project would claim
otherwise. It contains the best freely-licensed reference corpus that exists, made
searchable offline, with citations so you can judge the source yourself. Where the library
is silent, Hypatia is built to say so rather than invent something — see
[docs/safety.md](docs/safety.md).

---

## Status

Working end to end: fetch a library, index it, ask it questions, get cited answers.

```bash
npm install

# 1. Helper programs — kiwix-serve, llama-server, pdfium.
#    Picks the right build for your machine (CUDA / Vulkan / Metal / CPU).
node scripts/fetch-tools.mjs --dest ./bin

# 2. Content (822 MB, verified against upstream checksums)
node scripts/fetch.js go-bag --dest /path/to/library

# 3. Build the search index (needs kiwix-serve running over the library)
cargo build --release
./target/release/museion index --library /path/to/library --pdfium ./bin

# 4. Ask
./target/release/museion ask --db /path/to/library/museion-index.db \
  "how much bleach makes a gallon of water safe to drink?"
```

You still supply a chat model yourself — any GGUF llama.cpp can load. `museion hardware`
says which class of model your machine should run and why.

The desktop app builds too — `cd apps/desktop && npm install && npx tauri build` — with an
Ask tab, a Browse tab over the raw archives, and a Library tab that detects your hardware
and supervises the local services.

**What it answers like.** Asked about bleach dosing, it gives ¼ teaspoon per gallon [1],
notes the 16-drops alternative [4][5], and warns to use only unscented bleach, to filter
cloudy water first, and that bleach will not remove chemical pollutants — every claim
carrying a page-level citation into *Purifying Water During an Emergency*.

Asked for a 55-gallon drum instead, it searches, reads the dose, and hands the arithmetic
to a calculator — `convert(55 * 0.25, tsp, ml) = 67.77` — then shows that calculation
under the answer as ground truth. A language model multiplying in its head got that one
wrong by a factor of five before the calculator existed.

Every answer is labelled by what actually produced it: *from your library*, *from your
library, with calculation*, *computed*, or *general knowledge — not from your library,
unverified*. Asked what caused the Bronze Age collapse, it answers from general
knowledge under that last label; asked anything medical or safety-critical the library
doesn't hold, it says so and stops. See [docs/retrieval.md](docs/retrieval.md).

On a machine that isn't set up, the app opens to a welcome screen and nothing else: it
detects your hardware, shows exactly what it would fetch and how large each piece is —
helper programs, the reranker, the chat model for your tier, the content bundle you pick
— then downloads it all (resumable, verified), fits the model to your GPU, starts the
services and builds the index. Then it gets out of the way. The catalogue is compiled
into the app, so an installed copy knows where everything comes from without the repo.
Everything the flow does, `museion` does from the command line too.

---

## Design

Three local processes, no Docker, no Python runtime to package:

```
┌─ Museion (Tauri v2: Rust shell + web UI) ────────────────┐
│  hardware detection · sidecar supervision · downloader   │
│  retrieval · chat · article browser                      │
└──────────────┬──────────────────────┬────────────────────┘
               │                      │
     ┌─────────▼─────────┐  ┌─────────▼──────────────────┐
     │ kiwix-serve       │  │ llama-server (chat)        │
     │ ZIM content +     │  │ llama-server (reranker)    │
     │ full-text index   │  │ CUDA / Vulkan / Metal      │
     └───────────────────┘  └────────────────────────────┘
```

### Why there is no embedding step

Projects that embed every article chunk into a vector database before you can ask anything
spend hours per archive and two to three times the disk — and you still can't query until
it finishes. Museion doesn't do that.

Most ZIM archives already ship with a working **Xapian full-text index**, so Museion
searches the index that is already there and reranks the top candidates with a small
cross-encoder to fix keyword search's weakness at paraphrase. Those archives are queryable
the moment the download completes.

Some archives — notably everything built by web crawl, which includes the whole `zimgit-*`
series in the Go Bag — ship an index that is technically present but effectively empty. We
measured it: zero hits for *chlorine*, *bleach*, *wound* or *bowline* across the entire
bundle. For those, Museion builds a local SQLite FTS5 index at import: minutes for the Go
Bag, not the hours a vector database would cost. Which path an archive takes is detected
automatically.

[docs/retrieval.md](docs/retrieval.md) has the measurements and the reasoning.

### Why the fetcher looks the way it does

Kiwix publishes a `.sha256`, a `.magnet` and a `.torrent` beside every archive. So the
catalog carries **authentic upstream checksums for content nobody had to download**, and
the mirror list is harvested from the magnet's webseeds rather than guessed — eight
independent mirrors per file, including nluug.nl, dotsrc.org and mirror.download.kiwix.org.
If the main redirector is down or blocked, your library still downloads. That is the
entire point.

Downloads land in a `.part` file and are renamed only once the hash matches, so a file
under its real name is always a good file. Resume re-hashes what is already on disk before
appending, and a server that ignores a `Range` request is detected rather than silently
producing a corrupt double-length file.

---

## Content

| Bundle | Size | Contents |
|---|---|---|
| **Go Bag** | 822 MB | Post-disaster, water, medicine, food preparation, knots |
| Medicine | ~12 GB | MDWiki / WikiMed, MedlinePlus *(planned)* |
| Homestead | ~60 GB | + Wikipedia (no images), iFixit, Appropedia, wikiHow, Wikibooks *(planned)* |
| Library | ~250 GB+ | + full Wikipedia, Gutenberg, Wikiversity, Stack Exchange, maps *(planned)* |

In the app, the Library tab's **Catalogue** lists all of these with size and description,
shows which you already have, and fetches any one of them on a button — verified, served,
and indexed, so it's askable straight away. Adding your own sources is a YAML file and
one command — see [docs/adding-content.md](docs/adding-content.md).

Museion ships no content itself. Everything it fetches is freely licensed, mostly CC BY-SA;
each manifest records the license and attribution, and the app surfaces them with every
citation.

## License

MIT for the code. The content it fetches carries its own licenses.
