# What changed in each version

The newest version is first. This file travels inside every copy of Museion,
so someone with no internet can read what a newer copy would bring before
upgrading to it. The section for a version is also the text of its release on
GitHub. The version number itself lives in `museion/version.py`.

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
