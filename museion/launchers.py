"""The files at the top of a prepared drive that a person double-clicks or
reads first: the launchers and the README.

Each launcher runs the drive's own Python on the drive's own copy of
Museion, from wherever the drive is mounted. They are written for the system
preparing the drive, since that is the system the drive's Python and helper
programs are built for.
"""

from __future__ import annotations

import sys
from pathlib import Path

WINDOWS = {
    "Museion.cmd": """@echo off
rem Starts Museion from this drive and opens it in the browser.
rem Close this window, or the last Museion tab, to stop it.
cd /d "%~dp0app"
"%~dp0python\\python.exe" -m museion %*
if errorlevel 1 pause
""",
    "Edit Museion.cmd": """@echo off
rem Opens opencode on Museion's own code, answering with the chat model on
rem this drive. See app\\AGENTS.md for how the code is laid out.
cd /d "%~dp0app"
"%~dp0python\\python.exe" -m museion edit %*
if errorlevel 1 pause
""",
}

# macOS opens a .command file in Terminal when it is double-clicked.
UNIX = {
    "Museion.command" if sys.platform == "darwin" else "museion.sh": """#!/bin/sh
# Starts Museion from this drive and opens it in the browser.
# Close this window, or the last Museion tab, to stop it.
cd "$(dirname "$0")/app" || exit 1
exec ../python/bin/python3 -m museion "$@"
""",
    "Edit Museion.command" if sys.platform == "darwin" else "edit-museion.sh": """#!/bin/sh
# Opens opencode on Museion's own code, answering with the chat model on
# this drive. See app/AGENTS.md for how the code is laid out.
cd "$(dirname "$0")/app" || exit 1
exec ../python/bin/python3 -m museion edit "$@"
""",
}


def launchers() -> dict[str, str]:
    """Launcher file names and their contents, for this system."""
    return WINDOWS if sys.platform == "win32" else UNIX


def write_all(root: Path) -> None:
    for name, text in launchers().items():
        path = root / name
        # Windows reads a .cmd file in the console's code page; CRLF and plain
        # ASCII keep every version of cmd.exe happy.
        data = text.replace("\n", "\r\n") if name.endswith(".cmd") else text
        if not path.exists() or path.read_text(encoding="ascii", errors="replace") != data:
            path.write_bytes(data.encode("ascii"))
        path.chmod(0o755)


def write_readme(root: Path, bundle: str) -> None:
    names = list(launchers())
    start, edit = names[0], names[1]
    text = f"""Museion -- an offline reference library with an assistant that answers from it.

This drive carries the {bundle} collection, the language models, the helper
programs, and Museion itself with the Python that runs it. Nothing on it needs
the internet, and nothing needs installing.

To use it:  double-click {start}.
A window opens to say Museion is running, and Museion opens in your web
browser. The first start on a computer measures its graphics card and picks
the model that fits; that takes a minute and happens once per computer. Close
the browser tab, or that window, to stop it.

To change it:  double-click {edit}.
That opens opencode, a coding assistant, on Museion's own code in app/, using
the chat model on this drive. Tell it what you want changed. app/AGENTS.md
describes how the code is laid out and how to check a change. Every change can
be undone at once by unzipping support/museion-app-original.zip into this
folder, the one holding app/ (the files in the zip start with app/).
This needs one of the large models and a graphics card with 8 GB or more: the
small model answers questions, but it cannot be relied on to change code.

Running from a flash drive means the model is read from it on every start; a
copy of this whole folder on the computer's own disk starts much faster, and
works the same.

support/LICENSES/README.txt says what is on the drive and under what terms.
MANIFEST.yaml lists every downloaded file with its checksum. To check them
years from now, use Settings > This drive in Museion, or run: {start} verify
"""
    newline = "\r\n" if sys.platform == "win32" else "\n"
    (root / "README.txt").write_bytes(text.replace("\n", newline).encode("utf-8"))
