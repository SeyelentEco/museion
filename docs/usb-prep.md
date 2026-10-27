# Preparing a drive for a machine with no connection

Museion needs internet once, to build the library. The machine that will *use* the library
never does. Those can be two different machines.

This matters more than it sounds. The common failure of preparedness tooling is that it
assumes you'll set it up before you need it. Being able to build a library on a working
connection and carry it to one that has none — a cabin, a boat, an air-gapped machine,
somewhere the network is simply gone — is what makes it usable after the fact rather than
only before.

## Prepare the whole drive

Settings › **Prepare a drive** in the app, or on the command line:

```bash
python -m museion prepare --dest E:\ --bundle go-bag --models qwen3-4b-instruct-q4,qwen3.6-35b-a3b-q4 --accelerators cuda,vulkan,cpu
```

Either one lays out a drive that runs Museion on any computer: the archives, the models
you chose, kiwix-serve and pdfium, a llama.cpp build for every kind of graphics card the
drive may meet, Museion's own code with the portable Python that runs it, opencode for
changing it, the licences, a README, and a manifest of every file with its checksum. The index is built on the drive, so the first question on the
far end is not preceded by ten minutes of indexing. Everything is hash-checked on the way
in and downloads resume, so a drive can be prepared over several sittings.

On the far end: plug it in and double-click `Museion.cmd` (`Museion.command` on a Mac).
Museion opens in the computer's web browser. The first start on a computer measures
its graphics card, picks the build and the model that fit, and remembers that on the
computer, not the drive. See [portable.md](portable.md) for how the drive is laid out and
what happens on a read-only one.

| edition | contents | drive |
|---|---|---|
| Go Bag, small model | Go Bag, Qwen3-4B, all three builds | 32 GB |
| Go Bag, both tiers | + Qwen3.6-35B Q4 | 64 GB |
| Homestead | Homestead bundle, both tiers | 128 GB |
| Library | full collection, both tiers | 512 GB SSD |

A flash stick works; an SSD is better. Running from the drive means llama.cpp reads the
model from it on every start, and a 22 GB model over a slow stick is minutes. A copy of
the whole drive on the computer's own disk runs the same and starts faster.

No time to prepare one? Each GitHub release has a zip of everything but the content and
the models (see [portable.md](portable.md#c-the-release)): unzip it onto the drive, start
it, and let the welcome screen fetch the rest into the drive.

## Or just the library, by hand

On the machine that has internet, point the fetcher straight at the removable drive:

```bash
git clone https://github.com/SeyelentEco/museion
cd museion
python -m museion fetch go-bag --dest /Volumes/MYDRIVE/museion-library
```

On Windows:

```bash
python -m museion fetch go-bag --dest E:\museion-library
```

Downloads resume, so unplugging the drive or losing the connection partway is recoverable —
run the same command again and it picks up where it stopped. Files are only given their
real name once the checksum matches, so a half-written archive can never be mistaken for a
complete one.

## If the downloads fail

Every archive has eight web mirrors, and the fetcher tries them all. If every one is
down or unreachable from where you are, it prints the archive's torrent and magnet link
instead — Kiwix publishes both for every file. Download it with any BitTorrent client,
put the file in your library folder, and run `python -m museion verify --library` on it
(or press Fetch again in the app): it is checked against the same checksum and picked up as if it had come from a
mirror.

Museion deliberately does not download via BitTorrent itself, and never seeds. Whether
your machine joins a swarm, and what that does to your bandwidth and visibility, is a
decision for you and your own client, not one a tool should make for you.

## Verify on the drive, before you leave

```bash
python -m museion verify --library /Volumes/MYDRIVE/museion-library
```

On a drive made by Prepare a drive, `python -m museion verify --drive E:\` (or Settings ›
This drive) checks the models too, against the drive's own manifest.

Do this while you still have a connection. If a file is corrupt, this is the last moment
re-fetching it is easy.

## Verify again later

Drives that sit in a cupboard for two years develop bad sectors, and nothing announces it.
Re-run the verify occasionally. It re-hashes every file against the catalog and tells you
exactly which command re-fetches anything that has rotted.

This is the check worth running when you *don't* need the library, so that it works when
you do.

## Filesystem

Use exFAT if the drive will move between Windows and macOS. Avoid FAT32: it caps files at
4 GB, and several of the larger archives — Wikipedia especially — exceed that. A download
that fails at exactly 4 GB is almost always this.

## What to carry

The Go Bag is 822 MB and fits anywhere. If you have the space, the larger bundles are worth
carrying — but a small verified library you actually have beats a large one you meant to
download.
