# Museion on a drive

The plan for carrying a library, its models, the helper programs and the app itself on
one removable drive, and for making that drive without a repository checkout.

Three pieces, built in this order because each needs the one before it.

| | what | why first |
|---|---|---|
| A | **Portable mode**: the app runs from wherever it is, paths relative to itself | nothing on a drive works without it |
| B | **Prepare a drive**: the app (and CLI) build a complete drive | needs A to produce something that runs |
| C | **Skeleton release**: everything except content, as one zip on GitHub | needs B's layout, and A to run from it |

## A. Portable mode

**Detection.** If a file named `museion-portable` sits beside the executable, the folder
holding the executable is the *portable root*, and everything is found relative to it:

```
<root>\
  Museion.exe               the app
  museion-portable          marker; holds a drive id, so per-machine state can be keyed
  config.json               paths, stored relative to <root>
  tuning.yaml               optional prompt/limit override, as in an installed app
  library\                  archives, museion-index.db, evals\
  models\                   .gguf files
  bin\                      kiwix-serve, pdfium and the llama.cpp build in use
  bin\llama\cuda\           llama.cpp builds for every accelerator a machine might have
  bin\llama\vulkan\
  bin\llama\cpu\
  support\                  WebView2 offline installer, README, LICENSES
```

Drive letters stop mattering: the config holds `library`, not `E:\library`.

**What is the machine's, not the drive's.** Which llama.cpp build to run, which model
tier fits, and the GPU layer split that `llama-fit-params` measures are properties of the
computer the drive is plugged into. They live in a *machine profile* under the user's
local app data, keyed by the drive id from the marker, and are laid over the drive's
config at launch. The first launch on a new machine detects the hardware, picks the build
(CUDA, else Vulkan, else CPU), picks the largest model on the drive that the tier allows,
runs the fit, and saves the profile; the drive itself is not written. The same drive in a
different computer gets a different profile and never sees the first one.

**Read-only drives.** The index is opened as SQLite finds it; a write-protected drive
still answers questions. Anything the app writes for itself -- eval runs, a rebuilt index
after adding content -- goes to the library folder when that is writable and to the
machine profile's folder when it is not.

**Adopted servers work unchanged.** Two drives, or a drive and an installed copy, on the
same machine share the ports; whichever started the servers, the other uses them.

## B. Prepare a drive

Settings › Prepare a drive, and `museion prepare` on the command line, take:

- a destination folder (a drive root, or any folder that will be copied to one);
- a bundle (Go Bag, Medicine, Homestead, Library);
- which chat models to carry (any of the catalogue's tiers; the reranker always);
- which llama.cpp builds to carry (CUDA, Vulkan, CPU; Metal for a macOS drive);

and then, reusing the fetcher, tool installer and indexer that already exist:

1. check the space (`disk.rs`) against the sum of what was chosen, and refuse up front;
2. lay out the folders above and write the marker with a fresh id;
3. download the archives, the models and every chosen build, verifying each hash, into
   place -- resumable, so a drive can be prepared over several sittings;
4. copy the running executable in as `Museion.exe`, and the installer beside it in
   `support\` for people who would rather install than run from the drive;
5. download the WebView2 offline installer into `support\` (a machine without WebView2
   and without internet cannot open the window otherwise);
6. write `support\LICENSES\` from the catalogue's licence fields, and a README;
7. build the index on the drive, so the first question on the far end is not preceded by
   ten minutes of indexing;
8. verify everything once more and write `MANIFEST.yaml`: what the drive holds, with
   sizes and hashes, so a recipient can check it years later with one button.

**Editions and what they need.**

| edition | contents | drive |
|---|---|---|
| Go Bag, small model | Go Bag, Qwen3-4B, all three builds | 32 GB |
| Go Bag, both tiers | + Qwen3.6-35B Q4 | 64 GB |
| Homestead | Homestead bundle, both tiers | 128 GB |
| Library | full collection, both tiers | 512 GB SSD |

exFAT throughout, so a drive moves between Windows and macOS.

**Install to this computer.** The drive's launcher offers the other direction too: copy
the library and models to the local disk and run the installer. Running from a flash
stick means llama.cpp mapping a 22 GB model over USB on every launch; copying it once is
the better trade on any machine that will be used more than once.

## C. Skeleton release

A GitHub release asset, `museion-drive-<version>-windows.zip` (and `-macos`), holding
everything that is not content: the app, the installer, every helper build, the WebView2
offline installer, licences, README, the marker, and empty `library\` and `models\`.
About 1.5 GB, under the 2 GB per-asset limit. Unzipped onto a drive it runs, sees an
empty library, and offers the same first-run fetch the installed app does -- into the
drive. Content and models are never mirrored on GitHub; they are fetched from where
they already live, with the hashes the catalogue records.

A workflow builds the zip on a tagged release for Windows and macOS. The macOS side is
gated on notarization, which is still on the distribution list: an unsigned app on a
drive draws Gatekeeper's "damaged" warning.

## Not doing

- Disk images. Sizes vary by edition and Windows users have no easy way to write one.
- BitTorrent. Museion does not download by torrent or seed; that stands.
- Hosting archives or models on GitHub. They have homes, and the catalogue records them.
