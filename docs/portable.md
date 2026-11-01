# Museion on a drive

Carrying a library, its models, the helper programs and Museion itself on one removable
drive, so that it runs on a computer with nothing installed and no internet, and so that
whoever has the drive can change Museion as well as use it.

Three pieces, each needing the one before it:

| | what | where |
|---|---|---|
| A | **Portable mode**: Museion runs from wherever the drive comes up | `museion/portable.py` |
| B | **Prepare a drive**: lay out a complete drive from a working copy | `museion/prepare.py`, Settings › Prepare a drive |
| C | **The release**: everything except content and models, as one zip per platform | `.github/workflows/release.yml` |

## A. Portable mode

**Detection.** If a file named `museion-portable` sits in the folder above the code
(above `app/`), that folder is the *portable root*, and everything is found relative to
it:

```
<root>/
  museion-portable      marker; holds a drive id, so per-computer state can be keyed
  config.json           paths, stored relative to <root>
  tuning.toml           optional prompt/limit override (Settings › Advanced)
  README.txt            what the drive is and how to start it
  MANIFEST.yaml         every archive and model with its size and SHA-256
  Museion.cmd           starts Museion (Museion.command on macOS, museion.sh on Linux)
  Edit Museion.cmd      opens opencode on app/ (Edit Museion.command, edit-museion.sh)
  app/                  Museion's code, catalogue, tests, docs and AGENTS.md
  python/               the portable Python that runs it
  library/              archives, museion-index.db, evals/
  models/               .gguf files
  bin/                  kiwix-serve, pdfium, opencode, rg
  bin/llama/cuda/       llama.cpp builds for every kind of graphics card a computer may have
  bin/llama/vulkan/
  bin/llama/cpu/        (bin/llama/metal/ on a macOS drive)
  support/              LICENSES/, museion-app-original.zip
```

Drive letters stop mattering: the config holds `library`, not `E:\library`. The
launchers run `python/` on `app/` from wherever they are, and Museion opens in whatever
browser the computer has, so nothing is installed.

**What is the computer's, not the drive's.** Which llama.cpp build to run, which model
tier fits, and the GPU layer split that `llama-fit-params` measures are properties of the
computer the drive is plugged into. They live in a *machine profile* under the computer's
local app data (`org.museion.desktop/portable/<drive id>/machine.json`), and are laid
over the drive's config at launch. The first launch on a new computer detects the
hardware, picks the build (CUDA, else Vulkan, else CPU), picks the largest model on the
drive that the tier allows, and fits it at the first start; the drive itself is not
written. The same drive in a different computer gets a different profile and never sees
the first one. A model chosen in Settings is saved to the profile too, so it does not
follow the drive to the next computer.

**Read-only drives.** The index is opened as SQLite finds it; a write-protected drive
still answers questions. Anything Museion writes for itself -- eval runs, a rebuilt index
after adding content -- goes to the library folder when that is writable and to the
profile's folder when it is not. A drive whose flushes fail (some USB disks) has its
index opened without them; an index can always be rebuilt.

**Adopted servers work unchanged.** Two drives, or a drive and a copy on the disk, on the
same computer share the ports; whichever started the servers, the other uses them.

**Changing it.** `Edit Museion` runs `python -m museion edit`: it starts the drive's chat
model with a 32k context and opens [opencode](https://opencode.ai) on `app/`, set up to
use only that model and to reach for nothing on the network.
[python.md](python.md#the-drive-and-changing-museion) says what that took.
`app/AGENTS.md` is what opencode reads first. `support/museion-app-original.zip` holds
`app/` as it was prepared; unzipped into the drive's top folder it undoes every change.

## B. Prepare a drive

Settings › Prepare a drive, and `python -m museion prepare` on the command line, take:

- a destination folder (a drive's top folder, or any folder that will be copied to one);
- a bundle (Go Bag, Medicine, Homestead, Library);
- which chat models to carry (any of the catalogue's tiers; the reranker always);
- which llama.cpp builds to carry (CUDA, Vulkan, CPU; Metal for a macOS drive);

and then, reusing the downloader, tool installer and indexer:

1. check the space against the sum of what was chosen, and refuse up front;
2. lay out the folders above and write the marker with a fresh id;
3. download the archives, the models, every chosen build, the portable Python, opencode
   and ripgrep, verifying each hash, into place -- resumable, so a drive can be prepared
   over several sittings;
4. copy Museion's code into `app/`, and a pristine zip of it into `support/`;
5. write the launchers, `support/LICENSES/` from the catalogue's licence fields, and the
   README;
6. build the index on the drive, so the first question on the far end is not preceded by
   ten minutes of indexing;
7. write `MANIFEST.yaml`: what the drive holds, with sizes and hashes, so a recipient can
   check it years later with `python -m museion verify` or Settings › This drive.

The drive is laid out for the system preparing it: its Python and helper programs are
built for that system, so prepare a Windows drive on Windows and a macOS drive on a Mac.

**Editions and what they need.**

| edition | contents | drive |
|---|---|---|
| Go Bag, small model | Go Bag, Qwen3-4B, all three builds | 32 GB |
| Go Bag, both tiers | + Qwen3.6-35B Q4 | 64 GB |
| Homestead | Homestead bundle, both tiers | 128 GB |
| Library | full collection, both tiers | 512 GB SSD |

exFAT throughout, so a drive moves between Windows and macOS.

Running from a flash stick means llama.cpp maps a 22 GB model over USB on every start; a
copy of the whole folder on the computer's own disk starts much faster and works the
same.

## C. The release

A GitHub release asset, `museion-drive-<version>-windows.zip` (and `-macos`), holding
everything that is not content: Museion's code, the portable Python, every helper build,
opencode and ripgrep, licences, README, the marker, and empty `library/` and `models/`.
It is `python -m museion prepare --skeleton`, zipped. Unzipped onto a drive it runs, sees
an empty library, and offers the same first-run fetch as anywhere else -- into the drive.
Content and models are never mirrored on GitHub; they are fetched from where they already
live, with the hashes the catalogue records.

The workflow builds the zips on a version tag, for Windows and for macOS on Apple
silicon, and before attaching them runs the tests with the drive's own Python from its
own `app/`. A pull request that changes how a drive is laid out builds and tests them
too. There is no Linux zip until the catalogue has Linux builds of llama.cpp and
kiwix-serve. Nothing in the macOS zip is notarized; if macOS refuses to run a program
from it, `xattr -dr com.apple.quarantine` on the unzipped folder lifts the quarantine a
download carries.

## Not doing

- Disk images. Sizes vary by edition and Windows users have no easy way to write one.
- BitTorrent. Museion does not download by torrent or seed; that stands.
- Hosting archives or models on GitHub. They have homes, and the catalogue records them.
