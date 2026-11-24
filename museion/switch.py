"""The last step of an upgrade: the parts in use move out, the new ones in.

Everything slow (copying, merging, testing) is done before this, in
<copy>/upgrade/new/ (upgrade.py). What is left is a list of renames, which
is written down first (upgrade/journal.json) and then carried out. A switch
cut short by a power failure or a pulled drive is finished from that list by
the next upgrade command or the next start of Museion (recover). A rename
that is refused, which on Windows means a program is still running from the
folder, undoes the ones before it and changes nothing.

What moved out is kept in <copy>/previous/<version>/, laid out as it was, so
going back is the same kind of switch in the other direction.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from museion import catalog, disk, launchers, manifest, package, prepare

WORK = "upgrade"        # staging, the journal and the logs, inside the copy
PREVIOUS = "previous"   # what the last switch replaced
JOURNAL = "journal.json"
HISTORY = "installed.json"  # in support/: every version this copy has been
ORIGINAL = f"support/{prepare.ORIGINAL_ZIP}"
PACKAGE = f"support/{package.NAME}"

Report = Callable[[str], None]


class Refused(RuntimeError):
    """The switch could not be made, and nothing was changed."""


def swap_moves(target: Path, parts: list[str], incoming: str, outgoing: str) -> list[tuple[str, str]]:
    """The renames that move the parts in use to `outgoing` and those in
    `incoming` into place, as paths relative to the copy's top folder.

    app/ is swapped a child at a time: on Windows a folder that is some
    program's current folder cannot be renamed, and the launcher's is app/."""
    away: list[tuple[str, str]] = []
    into: list[tuple[str, str]] = []
    for part in parts:
        if part == "app":
            away += [(f"app/{p.name}", f"{outgoing}/app/{p.name}") for p in sorted((target / "app").iterdir())]
            into += [(f"{incoming}/app/{p.name}", f"app/{p.name}") for p in sorted((target / incoming / "app").iterdir())]
        else:
            if (target / part).exists():
                away.append((part, f"{outgoing}/{part}"))
            into.append((f"{incoming}/{part}", part))
    for name in (ORIGINAL, PACKAGE):
        if (target / name).exists():
            away.append((name, f"{outgoing}/{name}"))
        if (target / incoming / name).exists():
            into.append((f"{incoming}/{name}", name))
    return away + into


def switch(target: Path, journal: dict, report: Report = lambda line: None) -> None:
    """Write the renames down, do them, then finish. `journal` holds "from"
    and "to" (versions), "moves", and whatever else the record of versions
    should keep about this switch."""
    work = target / WORK
    work.mkdir(exist_ok=True)
    disk.write_text(work / JOURNAL, json.dumps(journal, indent=2))
    done: list[tuple[str, str]] = []
    try:
        for src, dst in journal["moves"]:
            move(target / src, target / dst)
            done.append((src, dst))
    except OSError as e:
        for src, dst in reversed(done):
            _undo(target / src, target / dst)
        (work / JOURNAL).unlink(missing_ok=True)
        raise Refused(f"could not move {e.filename or 'a file'} ({e.strerror or e}). Is Museion still running "
                      "from that folder? Close it and try again; nothing was changed.") from e
    finish(target, journal)


def recover(target: Path, report: Report = lambda line: None) -> bool:
    """Finish a switch that was cut short. True if there was one."""
    target = Path(target)
    try:
        journal = json.loads((target / WORK / JOURNAL).read_text(encoding="utf-8"))
        moves = [(str(s), str(d)) for s, d in journal["moves"]]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    report(f"Finishing the switch to {journal.get('to')} that was cut short")
    for src, dst in moves:
        move(target / src, target / dst)
    finish(target, journal)
    return True


def move(src: Path, dst: Path, attempts: int = 60) -> None:
    """Rename `src` to `dst`, unless that was already done. A rename refused
    because something still holds the file (a virus scanner, a program on
    its way out) is tried again for half a minute. The Python running this
    cannot be moved while it runs, so its folder is copied, and tidy()
    removes the original later."""
    if not src.exists() or dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if inside(Path(sys.executable), src):
        part = dst.with_name(dst.name + ".part")
        shutil.rmtree(part, ignore_errors=True)
        shutil.copytree(src, part, symlinks=True)
        part.rename(dst)
        return
    for attempt in range(attempts):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def _undo(src: Path, dst: Path) -> None:
    try:
        if src.exists():  # it was copied, not moved
            shutil.rmtree(dst, ignore_errors=True)
        elif dst.exists():
            move(dst, src, attempts=1)
    except OSError:
        pass


def finish(target: Path, journal: dict) -> None:
    """After the renames: the launchers and README as the code now in app/
    would write them, the record of versions, and the staging folder gone.
    Safe to run twice."""
    launchers.write_all(target)
    try:
        title = catalog.bundle(manifest.bundle(target))[0].title
        launchers.write_readme(target, title, skeleton=not manifest.read(target))
    except (OSError, ValueError, LookupError):
        pass  # the README it has stays
    entry = {k: v for k, v in journal.items() if k != "moves"}
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    past = history(target)
    if not past or {k: v for k, v in past[-1].items() if k != "at"} != {k: v for k, v in entry.items() if k != "at"}:
        past.append(entry)
        disk.write_text(target / "support" / HISTORY, json.dumps(past, indent=2) + "\n")
    (target / WORK / JOURNAL).unlink(missing_ok=True)
    tidy(target)


def tidy(target: Path) -> None:
    """Remove what an upgrade leaves behind once nothing needs it: the
    staging folder, an unpacked download, and what is left in previous/ of a
    version that has been gone back to. Leaves everything alone while a
    switch is unfinished, and whatever this Python is running from."""
    target = Path(target)
    work = target / WORK
    if (work / JOURNAL).exists():
        return
    running = Path(sys.executable)
    for folder in (work / "new", work / "incoming"):
        if folder.exists() and not inside(running, folder):
            shutil.rmtree(folder, ignore_errors=True)
    previous = target / PREVIOUS
    for kept in previous.iterdir() if previous.is_dir() else ():
        if kept.is_dir() and not (kept / "app" / "museion").is_dir() and not inside(running, kept):
            shutil.rmtree(kept, ignore_errors=True)


def history(target: Path) -> list[dict]:
    """Every switch this copy has made, oldest first."""
    try:
        data = json.loads((Path(target) / "support" / HISTORY).read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False
