"""How much room there is, and how much the library is using.

A person deciding whether to fetch Wikipedia needs one number more than any
other: whether it will fit. Free space comes straight from the OS through
shutil.disk_usage, which asks statvfs on macOS and Linux and
GetDiskFreeSpaceExW on Windows.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def free_space(path: Path) -> int:
    """Bytes free on the volume holding `path`. Raises OSError if it cannot
    be read."""
    # The path must exist; walk up to the nearest existing ancestor so a
    # library folder that has not been created yet still gets an answer.
    p = Path(path)
    while not p.exists():
        if p.parent == p:
            raise FileNotFoundError("no existing ancestor")
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError as e:
        raise OSError(f"could not read free space for {path}") from e


def dir_size(dir: Path) -> int:
    """Total bytes of files under `dir`, recursing a few levels. Symlinks are
    not followed."""
    return _walk(Path(dir), 0)


def _walk(d: Path, depth: int) -> int:
    if depth > 3:
        return 0
    total = 0
    try:
        with os.scandir(d) as entries:
            for e in entries:
                try:
                    if e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                    elif e.is_dir(follow_symlinks=False):
                        total += _walk(Path(e.path), depth + 1)
                except OSError:
                    continue  # vanished or unreadable: counts as nothing
    except OSError:
        return 0
    return total


def volume_name(path: Path) -> str:
    """A short name for the volume: "E:" on Windows, "/" otherwise."""
    s = str(path)
    if len(s) >= 2 and s[1] == ":":
        return s[:2].upper()
    return "/"


def gb(n: int) -> str:
    """A size in binary units, exactly as the page writes it (gb in
    museion/ui/dom.js), so the page and the command line never quote two
    different sizes for the same file."""
    g = n / 1073741824
    if g >= 1000:
        return f"{g / 1024:.1f} TB"
    if g >= 1:
        return f"{g:.0f} GB" if g >= 10 else f"{g:.1f} GB"
    return f"{round(n / 1048576)} MB"
