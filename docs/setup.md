# Setting up, start to finish

Everything below needs internet. Once it is done, nothing does. Do it *before* you need it.

**The short way:** run `python -m museion` in a copy of this repository (or double-click
`Museion.cmd` in an unzipped release). On a computer that isn't set up the page shows only
a set-up wizard, one question per step:

1. **Start** — what set-up does, and what Museion found when it looked at this computer.
2. **Folders** — one folder for everything, on a drive with room (or the library, models
   and helper programs in separate places). It shows the free space as you type.
3. **Model** — each model with whether it fits this computer, its size, what it is good
   at and what it cannot do, and one marked *Recommended for this computer*. It also offers
   the small model as a spare that runs on any computer. See
   [hardware.md](hardware.md#choosing-a-model).
4. **Library** — which bundle to start with, from the 1 GB Go Bag upwards.
5. **Home area** — optional: your town, for frost dates and a map.
6. **Set up** — the full list of what will be downloaded, its total size, and whether it
   fits. Nothing downloads before you press **Set up this computer** here. Then it does
   steps 2–6 below for you, ticking off each stage, and carries on where it stopped if it
   is interrupted and you press **Try again**.

When it finishes it gets out of the way — Ask, Library, Evals and Settings are what you
see from then on. (*Run first-time set-up again* under Settings brings it back for a new
computer, and **Settings › Models › Help me choose a model** is the model step on its
own, for changing model later.) The rest of this page is the same thing by hand.

**Room:** the top of the Library shelf shows what the library takes on disk and what is
free on its drive. Anything that would not fit is marked with how much more it needs, and
cannot be started — Museion refuses before the first byte rather than failing at 98%.

**Adding more later:** the **Library** tab has two shelves. *In your library* is what you
have — click anything to read it. *Available to fetch* is everything else in the
catalogue, by category with sizes — click to fetch. Each is verified against its
publisher's checksum, the content server is restarted to serve it, and it is indexed, so
it is askable the moment the progress bar finishes. The same list from the command line
is `python -m museion fetch --list`.

**Content that isn't in the catalogue:** under **Settings → Other content folders**, add
any folder. `.zim` archives in it are served and searched like catalogue ones; loose PDFs
are indexed by page, cited like everything else, and open in the page's reader at the
cited page. The catalogue is a starting point, not a boundary.

Budget: about ten minutes of your attention and, on a normal connection, half an hour of
downloading for the Go Bag plus a 4B model, or a few hours if you take the 35B MoE.

## 0. Prerequisites

- **Python** 3.11 or newer. Nothing else: Museion uses only Python's standard library,
  so there is nothing to `pip install`. (A release zip carries its own Python.)
- Disk: 1 GB for the Go Bag, 3–37 GB for a model, 250+ GB if you want all of Wikipedia.

Every command below is run from the top of the repository. On Windows, `python` may be
called `py`.

## 1. Get the code

```bash
git clone https://github.com/SeyelentEco/museion
cd museion
```

## 2. Helper programs

```bash
python -m museion tools --dest bin
```

Detects your GPU and fetches the matching llama.cpp build (CUDA, Vulkan, Metal or CPU),
plus kiwix-serve and pdfium. Everything is checked against pinned checksums. The
catalogue has no Linux builds of llama.cpp or kiwix-serve yet, so on Linux this installs
only what it has; point Settings at your own builds.

## 3. Find out what your machine is

```bash
python -m museion hardware
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
python -m museion fetch go-bag --dest /path/to/library
python -m museion fetch qwen3-reranker-0.6b --dest /path/to/models
python -m museion fetch qwen3.6-35b-a3b-q4  --dest /path/to/models   # or the model for your tier
```

`python -m museion fetch --list` shows every bundle, archive and model. Downloads resume
if interrupted, and a file only gets its real name once its checksum matches.

## 5. Index

The Go Bag archives are PDF collections and need a local index; Wikipedia-style archives
do not. The indexer works out which is which. It reads content through kiwix-serve, so
start that first:

```bash
./bin/kiwix-serve --port 8090 /path/to/library/*.zim
python -m museion index --library /path/to/library --pdfium bin
```

A few minutes for the Go Bag. You will see each document go by.

## 6. Fit the model to your GPU

```bash
python -m museion fit --model /path/to/models/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf --fit-exe bin/llama-fit-params
```

Prints the `-ngl`/`-ot` split measured against your actual card, leaving 2 GiB of it free
(filling the card made answers several times slower; see [python.md](python.md)). Skip
this on the Modest tier; a 4B model just fits.

## 7. Open it

```bash
python -m museion
```

Museion starts and opens in your default browser at `http://127.0.0.1:8070/`. Under
**Settings**, give it the paths from steps 2 and 4, then **Save**, **Start services**,
**Build index** (if you skipped step 5), **Fit model to this machine** (if you skipped step
6). Then ask. It stops, with its helpers, a minute after the last Museion tab closes, or
with the Quit button.

## Changing it

Nothing needs building. Change a file and see the change:

| you changed | do this |
|---|---|
| the system prompt, a limit | Settings › Advanced creates `tuning.toml` beside `config.json` and opens it; save it and ask again |
| the page (`museion/ui/`) | reload the page |
| the Python | stop Museion (Quit, or Ctrl+C in its window) and run it again |
| how the index is built | Settings › Rebuild index |

**The servers can stay up between runs.** Set `MUSEION_KEEP_SERVICES=1` and the servers
Museion starts are left running when it stops. The next start finds them answering on
their ports, checks that the chat and reranker servers report the configured model files,
and adopts them: the page is usable in seconds and a 22 GB model loads once a day, not
once a restart. Adopted servers are never killed on exit; the Stop button and a
benchmark's model swap still stop them.

**The ask loop from the command line.**

```bash
python -m museion eval --library <library> --only reason          # a capability, a horizon, or one case id
python -m museion eval --library <library> --only bleach-drum --label x   # saved like any other run
python -m museion chat --library <library>                          # follow-ups keep their context
python -m museion retrieval-eval --library <library>                # retrieval alone, no chat model
```

These use the servers on their usual ports (chat 8080, reranker 8081, kiwix-serve 8090),
and `--tuning` takes the same override file the page uses. Set `MUSEION_TRACE=1` to see
where the seconds go in an answer: the rewrite, the seeded search, each model turn.

**The tests** run offline in a few seconds: `python -m unittest discover -s tests`.
[AGENTS.md](../AGENTS.md) has the rules the code keeps and a map of where things are.

## Before you rely on it

Pull the network cable and ask it something. That is the only test that validates the
premise, and it is worth doing once while fixing anything is still easy. Then re-run
`python -m museion verify --library /path/to/library` every few months; drives in
cupboards rot quietly. See [usb-prep.md](usb-prep.md) for carrying a finished library to a
machine that has no connection at all.
