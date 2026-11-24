"""Helper programs the person brings themselves.

Museion downloads its helper programs -- llama-server to run the models,
kiwix-serve to serve the archives, pdfium to read PDFs -- from pinned builds
in tools/. Where that list has no build for a system (Linux, when this was
written; a Mac with an Intel processor has no llama.cpp), set-up used to
download the models and the library and then stop at "kiwix-serve path not
configured". The wizard now asks for the person's own copies first, looks
for them where a package manager puts them, and checks each one is there
before anything large is fetched.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from museion import catalog, pdf
from museion.appstate import AppState, UiError

# What is asked for, in order: the id the page sends back, the config field
# it fills, what it is in plain words, and where to get it.
PROGRAMS = [
    {"id": "llama-server", "field": "llama_exe", "kind": "program",
     "label": "llama-server",
     "what": "Runs the models. From llama.cpp: a release from github.com/ggml-org/llama.cpp, your "
             "system's package, or built from source. Keep llama-fit-params beside it if your build has it."},
    {"id": "kiwix-serve", "field": "kiwix_exe", "kind": "program",
     "label": "kiwix-serve",
     "what": "Serves the archives. In the kiwix-tools package of most Linux systems, or from "
             "download.kiwix.org/release/kiwix-tools."},
    {"id": "pdfium", "field": "pdfium_dir", "kind": "dir",
     "label": "The folder holding pdfium",
     "what": f"Reads PDFs for the index: the folder with {pdf.library_filename()} in it, from "
             "github.com/bblanchon/pdfium-binaries."},
]

# Where a library installed by hand or by a package usually is.
LIB_DIRS = ["/usr/local/lib", "/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu",
            "/usr/lib/aarch64-linux-gnu", "/opt/homebrew/lib", "/opt/pdfium/lib", "/opt/pdfium"]


def provided(program: str, accel) -> bool:
    """Does the catalogue have a build of this program for this system?"""
    return any(program in t.provides and catalog.tool_variant(t, accel) is not None for t in catalog.tools())


def find(program: dict, current: Path | None) -> str:
    """Where this program already is: the path in Settings if it is still
    there, else where the system would find it. Empty if nowhere."""
    if program["kind"] == "dir":
        for folder in [current, *map(Path, LIB_DIRS)]:
            if folder and has_pdfium(folder):
                return str(folder)
        return ""
    if current and current.is_file():
        return str(current)
    return shutil.which(program["id"]) or ""


def has_pdfium(folder: Path) -> bool:
    try:
        pdf.find_library(folder)
        return True
    except (FileNotFoundError, OSError):
        return False


def missing(state: AppState) -> list[dict]:
    """The helper programs the catalogue cannot fetch for this system, each
    with where one was found already. Empty on a system the catalogue covers,
    and from a drive, which carries its own."""
    if state.drive:
        return []
    cfg = state.config()
    accel = state.hardware().accelerator
    return [{"id": p["id"], "label": p["label"], "what": p["what"], "kind": p["kind"],
             "path": find(p, getattr(cfg, p["field"]))}
            for p in PROGRAMS if not provided(p["id"], accel)]


def problem(program: dict, text: str) -> str | None:
    """Why this path will not do, or None."""
    if not text.strip():
        return f"{program['label']}: not given."
    path = Path(text.strip())
    if program["kind"] == "dir":
        return None if has_pdfium(path) else f"There is no {pdf.library_filename()} in {path}."
    if not path.is_file():
        return f"{program['label']}: there is no file at {path}."
    if sys.platform != "win32" and not os.access(path, os.X_OK):
        return f"{program['label']}: {path} is not a program this account may run."
    return None


def use_own_programs(state: AppState, paths: dict) -> str:
    """Check the paths the wizard was given for the programs the catalogue
    lacks, and save them. `paths` is {"llama-server": "/usr/bin/llama-server",
    ...}. Raises with every problem at once, so they are fixed in one go."""
    wanted = {p["id"]: p for p in PROGRAMS if not provided(p["id"], state.hardware().accelerator)}
    problems = [why for id, p in wanted.items() if (why := problem(p, str(paths.get(id) or "")))]
    if problems:
        raise UiError(" ".join(problems))
    cfg = state.config()
    for id, p in wanted.items():
        setattr(cfg, p["field"], Path(str(paths[id]).strip()))
    state.save_config(cfg)
    llama = wanted.get("llama-server") and Path(str(paths["llama-server"]).strip())
    if llama and not (llama.parent / "llama-fit-params").exists() and not (llama.parent / "llama-fit-params.exe").exists():
        return ("Saved. There is no llama-fit-params beside llama-server, so a large model cannot be "
                "measured against the graphics card and may load slowly or not at all.")
    return "Saved."
