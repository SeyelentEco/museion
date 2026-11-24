# What changed in each version

The newest version is first. This file travels inside every copy of Museion,
so someone with no internet can read what a newer copy would bring before
upgrading to it. The section for a version is also the text of its release on
GitHub. The version number itself lives in `museion/version.py`.

## 0.2.0

Upgrading: a newer Museion can be put into an older one, from a file, with no
internet. `docs/upgrading.md` explains all of it.

- **Upgrade this copy** (Settings › Version and upgrades, or
  `upgrade <download or folder>`): give it a newer download or the folder of a
  newer copy. The new version is unpacked beside the old, tested, and switched
  to; Museion restarts.
- **Bring another copy up to this version** (the same card, or
  `upgrade --into <folder>`): an older folder, or a drive. This is how a copy
  of version 0.1.0, which cannot upgrade itself, is upgraded: unpack the newer
  download next to it and point it at the old folder.
- Your library, models, notes, settings and your own agents are never touched.
  The Python and the helper programs are replaced only when the newer copy
  carries different builds of them.
- **Changes you made to Museion's code are carried over.** Each file is merged
  with the new version's. A change that cannot be merged, because the new
  version changed the same lines, is set aside in `my-changes/` with a note in
  `app/UPGRADE-NOTES.md` saying how to put it back, which Edit Museion can do.
  `python -m museion changes` lists what you have changed.
- **Two copies of your own**: bringing another copy up to date can take along
  the changes you made to this copy's code (a tick box, or `--with-changes`),
  so a drive and a computer copy can be kept the same.
- **Check for a newer version** (a button on the card): asks GitHub for the
  latest release and can download it, check it and upgrade from it. It is the
  only part that uses the internet, and nothing is asked unless you press it.
- **Go back** (the card's button, or `upgrade --back`): the version an upgrade
  replaced is kept in `previous/` until the next upgrade.
- An upgrade cut short by a power failure or a pulled drive is finished the
  next time Museion starts.

## 0.1.0

The first version that can be downloaded and run without git or an installed
Python.

- A download for Windows, for macOS on Apple silicon and for Linux (x86-64).
  Each holds Museion, the Python that runs it and the helper programs; the
  first start fetches the library and the model you choose.
- Linux is supported: the catalogue has Linux builds of llama.cpp, kiwix-serve
  and pdfium. A Linux computer with an NVIDIA or AMD graphics card uses the
  Vulkan build of llama.cpp (there is no CUDA build for Linux), and needs the
  system's Vulkan driver for it; without one Museion runs on the processor.
- Museion has a version number, shown beside its name on the page and printed
  by `python -m museion version`.
- Helper programs are unpacked without symbolic links, so a Linux or macOS
  drive formatted exFAT holds them.
- Fixed: drives and downloads went out without the agents, which left Ask
  answering without citations.

What it does, as of this version: cited answers from an offline library (Ask),
agents for particular jobs, your own notes and records, offline maps and
making a map, a seasonal almanac with frost dates for your home area, an
optional Meshtastic radio, portable drives, a benchmark, and changing Museion
itself offline with opencode and a local model.
