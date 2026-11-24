"""Carrying a person's changes to Museion's code into a new version.

Museion is meant to be changed by whoever has it, so an upgrade cannot just
replace app/. Every drive keeps support/museion-app-original.zip, app/ as it
was shipped, which gives each file three versions: as shipped before (the
base), as the person has it now (mine), and as the new version ships it.

  mine is the base           -> the new version's file
  the new file is the base   -> mine, kept as it is
  mine only (I added it)     -> kept
  both changed, apart        -> merged line by line (merge3.py)
  both changed the same lines-> the new version's file; mine is set aside

Nothing of the person's is thrown away. What could not be carried is copied
to my-changes/ beside app/, with a diff of what they had changed, and listed
in app/UPGRADE-NOTES.md for them, or the coding assistant, to put back.
"""

from __future__ import annotations

import difflib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from museion import merge3

# Written into the new app/ when something could not be carried. Never part
# of what is shipped, so never a "change" itself.
NOTES = "UPGRADE-NOTES.md"

KEPT = "kept"            # mine, unchanged by the new version
ADDED = "added"          # mine, a file Museion never shipped
MERGED = "merged"        # both changed it, in different places
REMOVED = "removed"      # I deleted it, and it stays deleted
SET_ASIDE = "set aside"  # could not be carried; the new version's file is used


@dataclass
class Result:
    # The new app/: relative path (with forward slashes) -> contents.
    files: dict[str, bytes]
    # What became of each of the person's changes: path -> one of the above.
    changes: dict[str, str] = field(default_factory=dict)
    # For each path set aside: (mine, a diff of what I had changed, why).
    aside: dict[str, tuple[bytes | None, str, str]] = field(default_factory=dict)

    def carried(self) -> list[str]:
        return sorted(p for p, how in self.changes.items() if how != SET_ASIDE)

    def set_aside(self) -> list[str]:
        return sorted(self.aside)


def read_tree(folder: Path) -> dict[str, bytes]:
    """Every file under an app/ folder, without Python's caches or the
    upgrade notes."""
    out: dict[str, bytes] = {}
    folder = Path(folder)
    for f in sorted(folder.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
            rel = f.relative_to(folder).as_posix()
            if rel != NOTES:
                out[rel] = f.read_bytes()
    return out


def read_original(zip_path: Path) -> dict[str, bytes] | None:
    """app/ as shipped, from support/museion-app-original.zip. None if the
    drive has no such file or it cannot be read."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            return {name[len("app/"):]: z.read(name) for name in z.namelist()
                    if name.startswith("app/") and not name.endswith("/")}
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def local_changes(base: dict[str, bytes], mine: dict[str, bytes]) -> dict[str, str]:
    """What the person has changed since app/ was shipped: path -> "changed",
    "added" or "removed"."""
    out: dict[str, str] = {}
    for path in sorted(set(base) | set(mine)):
        if path not in base:
            out[path] = "added"
        elif path not in mine:
            out[path] = "removed"
        elif not merge3.same_text(base[path], mine[path]):
            out[path] = "changed"
    return out


def diff(path: str, base: bytes | None, mine: bytes | None) -> str:
    """What the person changed in one file, as a unified diff."""
    def lines(data: bytes | None) -> list[str] | None:
        if data is None:
            return []
        try:
            return data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return None
    a, b = lines(base), lines(mine)
    if a is None or b is None:
        return f"{path}: not a text file; the whole file is kept beside this note.\n"
    return "\n".join(difflib.unified_diff(a, b, f"shipped/{path}", f"yours/{path}", lineterm="")) + "\n"


def merge(base: dict[str, bytes] | None, mine: dict[str, bytes], new: dict[str, bytes]) -> Result:
    """The new version's app/ with the person's changes carried into it."""
    if base is None:
        # No record of what was shipped, so no way to tell their changes
        # from the old version's own code. The new version goes in as it is;
        # the whole old app/ is kept in previous/ by the upgrade.
        return Result(dict(new))
    result = Result({})
    for path in sorted(set(base) | set(mine) | set(new)):
        was, me, them = base.get(path), mine.get(path), new.get(path)
        if me is None:
            if was is None:
                result.files[path] = them  # new in this version
            elif them is not None and not merge3.same_text(was, them):
                # I deleted it, and the new version changed it: it comes back.
                result.files[path] = them
                result.changes[path] = SET_ASIDE
                result.aside[path] = (None, f"{path}: you had deleted this file.\n",
                                      "you deleted it, and the new version changed it, so it is back")
            elif them is not None:
                result.changes[path] = REMOVED
            continue
        if was is not None and merge3.same_text(me, was):
            if them is not None:
                result.files[path] = them  # untouched by me: the new version's, or gone with it
            continue
        # From here on the file is one I added or changed.
        if them is None:
            if was is None:
                result.files[path], result.changes[path] = me, ADDED
            else:
                result.changes[path] = SET_ASIDE
                result.aside[path] = (me, diff(path, was, me), "the new version no longer has this file")
        elif merge3.same_text(me, them):
            result.files[path] = them
        elif was is not None and merge3.same_text(them, was):
            result.files[path], result.changes[path] = me, KEPT
        else:
            merged = merge3.merge_text(was, me, them) if was is not None else None
            if merged is not None:
                result.files[path], result.changes[path] = merged, MERGED
            else:
                result.files[path] = them
                result.changes[path] = SET_ASIDE
                why = ("you and the new version changed the same lines" if was is not None
                       else "the new version has its own file of this name")
                result.aside[path] = (me, diff(path, was, me), why)
    return result


def without_changes(base: dict[str, bytes] | None, mine: dict[str, bytes], new: dict[str, bytes]) -> Result:
    """The new version's app/ as shipped, with every one of the person's
    changes set aside: what an upgrade falls back to when the merged code
    fails its tests."""
    result = Result(dict(new))
    for path, kind in local_changes(base or {}, mine).items() if base is not None else ():
        result.changes[path] = SET_ASIDE
        me = mine.get(path)
        result.aside[path] = (me, diff(path, base.get(path), me) if kind != "removed" else f"{path}: you had deleted this file.\n",
                              "the new version did not pass its tests with your changes in it")
    return result


def notes(result: Result, old: str, new: str, aside_folder: str) -> str:
    """The text of app/UPGRADE-NOTES.md: what was not carried, and where it
    is. Empty when everything was."""
    if not result.aside:
        return ""
    lines = [f"# Changes of yours that the upgrade to {new} could not carry over", "",
             f"This copy of Museion was upgraded from {old or 'an earlier version'} to {new}. You had changed",
             "its code. Most changes are carried into a new version automatically; the ones",
             "below could not be, and the new version's file is in use instead. Nothing was",
             f"thrown away: your version of each file, and a diff of exactly what you had",
             f"changed in it, are in `{aside_folder}/`, beside `app/`.", "",
             "To put a change back, apply the same change to the new file, run the tests",
             "(`python -m unittest discover -s tests`), and delete its entry below. When no",
             "entries are left, delete this file. The coding assistant (Edit Museion) can do",
             "this: ask it to \"re-apply the changes listed in UPGRADE-NOTES.md\".", ""]
    for path in result.set_aside():
        mine, _, why = result.aside[path]
        lines += [f"## `{path}`", "", f"- Why it was not carried: {why}.",
                  f"- What you had changed: `{aside_folder}/{path}.diff`"]
        if mine is not None:
            lines.append(f"- Your whole file: `{aside_folder}/{path}`")
        lines.append("")
    return "\n".join(lines)


def write_aside(result: Result, folder: Path) -> None:
    """Copy what was set aside to `folder`: each file as the person had it,
    and beside it a .diff of what they had changed."""
    for path, (mine, text, _) in result.aside.items():
        target = Path(folder) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if mine is not None:
            target.write_bytes(mine)
        target.with_name(target.name + ".diff").write_text(text, encoding="utf-8")
