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

**M0 — the content layer.** The catalog, the fetcher, and verification work today.
The desktop app is not built yet.

What runs right now:

```bash
npm install
node scripts/fetch.js --list
node scripts/fetch.js go-bag --dest /path/to/library
node scripts/verify.js --dest /path/to/library
```

That gives you a verified 822 MB library of post-disaster, water, medicine, food and knots
references. It is already useful with any ZIM reader — [Kiwix](https://kiwix.org) will open
it — while the app is being built.

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

Every ZIM file already ships with a **Xapian full-text index**. Projects that embed every
article chunk into a vector database before you can ask anything spend hours per archive
and two to three times the disk — and you still can't query until it finishes.

Museion searches the index that is already there, then reranks the top candidates with a
small cross-encoder to fix keyword search's weakness at paraphrase. Your library is
queryable the moment the download completes. A semantic index over a small core corpus is
a later, optional refinement — not a precondition for the thing working.

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

Adding your own sources is a YAML file and one command — see
[docs/adding-content.md](docs/adding-content.md).

Museion ships no content itself. Everything it fetches is freely licensed, mostly CC BY-SA;
each manifest records the license and attribution, and the app surfaces them with every
citation.

## License

MIT for the code. The content it fetches carries its own licenses.
