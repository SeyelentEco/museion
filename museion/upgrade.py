"""Upgrading a copy of Museion, from a file, with no internet.

One operation does it all: **a newer copy installs itself into an older
one.** The code that runs is always the newer copy's, so an upgrade never
depends on what an old version knew how to do, and the copy being upgraded
is not running while its files are changed.

  python -m museion upgrade --into <folder>   put this Museion into that copy
  python -m museion upgrade <zip or folder>   upgrade this copy from a newer
                                              one (upgrade_cli.py unpacks it
                                              and runs the line above in it)
  python -m museion upgrade --back            go back to the version before

A copy's folder holds three kinds of thing (docs/upgrading.md):

  the person's   library/ models/ config.json tuning.toml agents/ and the
                 marker: never touched
  Museion's      python/ and bin/: replaced, and only when the newer copy's
                 builds differ (package.py records which each copy holds)
  both           app/, Museion's code, which the person may have changed:
                 merged (appmerge.py)

It happens in two steps. stage() builds the new parts in <copy>/upgrade/new/
and runs the new code's tests there; nothing in use is touched, and Museion
can go on running in the copy meanwhile. switch_staged() then swaps them in
(switch.py), which takes a moment and needs Museion in that copy closed.
What was replaced goes to <copy>/previous/<version>/, and go_back() swaps
it home again.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from museion import appmerge, catalog, disk, package, prepare, switch, version
from museion.portable import MARKER
from museion.switch import ORIGINAL, PREVIOUS, WORK, Report, inside

MINE = "my-changes"     # changes of the person's that could not be carried
STAGED = "staged.json"  # in upgrade/: what stage() built, for switch_staged()
PARTS = ("app", "python", "bin")

# Room left free after the new parts are copied in.
HEADROOM = 200 << 20


class UpgradeError(RuntimeError):
    """Why an upgrade cannot happen, worded for the person."""


@dataclass
class Source:
    """The newer Museion: its code, and its Python and helpers if it is a
    whole copy (a drive or a download) and not just a checkout of the code."""
    app: Path
    root: Path | None
    version: str
    package: package.Package


@dataclass
class Outcome:
    old: str
    new: str
    parts: list[str]                                  # of "app", "python", "bin"
    carried: list[str] = field(default_factory=list)  # the person's changes now in the new code
    aside: list[str] = field(default_factory=list)    # those that could not be carried
    aside_folder: str = ""
    # True when the merged code failed its tests and the new version went in
    # without any of the person's changes.
    dropped_changes: bool = False


# ── looking ──────────────────────────────────────────────────────────────────

def source_of(path: Path) -> Source:
    """The Museion at `path`: a copy's top folder, its app/ folder, or a
    checkout of the repository."""
    path = Path(path).resolve()
    for app, root in ((path / "app", path), (path, path.parent if path.name == "app" else None)):
        found = package.code_version(app)
        if found:
            whole = root is not None and (root / MARKER).is_file()
            return Source(app, root if whole else None, found, package.read(root) if whole else package.Package())
    raise UpgradeError(f"{path} is not a copy of Museion (or is one from before version 0.1.0)")


def this_museion() -> Source:
    return source_of(prepare.APP_DIR)


def check_target(target: Path) -> str:
    """The version of the copy at `target`, having made sure it is one that
    can be upgraded here. Empty for a copy from before versions."""
    target = Path(target)
    if not (target / MARKER).is_file() or not (target / "app" / "museion").is_dir():
        raise UpgradeError(f"{target} is not a Museion folder: it has no {MARKER} file and app/ folder. "
                           "(A copy made with git is brought up to date with git pull.)")
    made_for = package.made_for(target)
    here = (catalog.current_platform(), catalog.current_arch())
    if made_for[0] and made_for != here:
        raise UpgradeError(f"{target} is a copy for {made_for[0]} ({made_for[1]}); this computer is {here[0]} "
                           f"({here[1]}). Upgrade it on the kind of computer it runs on.")
    return package.code_version(target / "app")


def parts_to_replace(source: Source, target: Path) -> list[str]:
    """Which of app, python and bin the newer copy would replace."""
    parts = ["app"]
    if source.root is None:
        return parts
    held = package.read(target).helpers
    new = source.package.helpers
    if (source.root / "python").is_dir() and new.get("python") and new["python"] != held.get("python"):
        parts.append("python")
    if (source.root / "bin").is_dir() and any(v != held.get(k) for k, v in new.items() if k != "python"):
        parts.append("bin")
    return parts


def local_changes(target: Path) -> dict[str, str] | None:
    """What the person has changed in a copy's code since it was shipped:
    path -> "changed", "added" or "removed". None if the copy has no record
    of what was shipped."""
    base = appmerge.read_original(Path(target) / ORIGINAL)
    if base is None:
        return None
    return appmerge.local_changes(base, appmerge.read_tree(Path(target) / "app"))


def previous_version(target: Path) -> str | None:
    """The version go_back() would return to, if one is kept."""
    folder = Path(target) / PREVIOUS
    kept = [p.name for p in folder.iterdir() if (p / "app" / "museion").is_dir()] if folder.is_dir() else []
    return max(kept, key=version.parse) if kept else None


def staged(target: Path) -> dict | None:
    """What stage() left ready to switch to, if anything."""
    try:
        data = json.loads((Path(target) / WORK / STAGED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and (Path(target) / WORK / "new" / "app").is_dir() else None


def fingerprint(app: Path) -> str:
    """Changes when any file of the code does, without reading them all."""
    seen = hashlib.sha256()
    for f in sorted(Path(app).rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts and f.name != appmerge.NOTES:
            st = f.stat()
            seen.update(f"{f.relative_to(app).as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return seen.hexdigest()


# ── upgrading ────────────────────────────────────────────────────────────────

def run_tests(app: Path, log: Path) -> bool:
    """The staged code's own tests, with the Python running this upgrade."""
    if not (app / "tests").is_dir():
        return True
    # No caches written: this folder is about to become app/.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    with open(log, "wb") as out:
        done = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
                              cwd=app, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=1800)
    return done.returncode == 0


def stage(source: Source, target: Path, *, force: bool = False, consume: bool = False,
          tests: Callable[[Path, Path], bool] | None = run_tests, keep_changes: bool = False,
          report: Report = lambda line: None) -> Outcome:
    """Build the Museion at `source` inside the copy at `target`, ready to
    be switched to. Nothing the copy is using is touched.

    `force` allows a version that is not newer. `consume` moves the source's
    helper programs instead of copying them (for a source unpacked only to be
    installed). `tests` runs the new code's tests (None skips them);
    `keep_changes` keeps the person's changes even if the tests fail with
    them."""
    target = Path(target).resolve()
    switch.recover(target, report)
    old = check_target(target)
    if source.root is not None and source.root.resolve() == target:
        raise UpgradeError("that is this copy of Museion itself")
    if not force and not version.newer(source.version, old):
        if version.parse(source.version) == version.parse(old):
            raise UpgradeError(f"{target} is already version {old}. Nothing to upgrade.")
        raise UpgradeError(f"{target} is version {old}, which is newer than this one ({source.version}). "
                           "Nothing was changed.")
    parts = parts_to_replace(source, target)
    need = sum(disk.dir_size(source.root / p) for p in parts if p != "app") + HEADROOM
    if disk.free_space(target) < need:
        raise UpgradeError(f"{target} needs {disk.gb(need)} free for the upgrade and has {disk.gb(disk.free_space(target))}")

    work = target / WORK
    new_dir = work / "new"
    (work / STAGED).unlink(missing_ok=True)
    shutil.rmtree(new_dir, ignore_errors=True)
    new_dir.mkdir(parents=True)
    label = old or "an earlier version"
    aside_name = f"{MINE}/from-{old or 'before-versions'}-to-{source.version}"

    report(f"Carrying your changes into {source.version}")
    seen = fingerprint(target / "app")
    base = appmerge.read_original(target / ORIGINAL)
    mine = appmerge.read_tree(target / "app")
    new = _shipped(source)
    merged = appmerge.merge(base, mine, new)
    # A note from an earlier upgrade stays until the person deletes it.
    try:
        earlier = (target / "app" / appmerge.NOTES).read_text(encoding="utf-8")
    except OSError:
        earlier = ""
    _write_app(new_dir / "app", merged, label, source.version, aside_name, earlier)
    dropped = False
    if tests is not None:
        report("Testing the new version before it is used (a minute or two)")
        if not tests(new_dir / "app", work / "tests.log"):
            failed = UpgradeError(f"version {source.version} did not pass its own tests here, so nothing was "
                                  f"changed. The tests' output is in {work / 'tests.log'}.")
            if not merged.changes:
                raise failed
            if not keep_changes:
                report("The tests fail with your changes in; trying the new version as shipped")
                merged = appmerge.without_changes(base, mine, new)
                _write_app(new_dir / "app", merged, label, source.version, aside_name, earlier)
                if not tests(new_dir / "app", work / "tests.log"):
                    raise failed
                dropped = True
    if merged.aside:
        appmerge.write_aside(merged, new_dir / "aside")

    for part in parts[1:]:
        report(f"Copying the new {'Python' if part == 'python' else 'helper programs'}")
        _bring(source.root / part, new_dir / part, consume)
    if "bin" in parts:
        # A llama.cpp build the old copy has and the new one lacks stays.
        for build in sorted((target / "bin" / "llama").glob("*")):
            if build.is_dir() and not (new_dir / "bin" / "llama" / build.name).exists():
                shutil.copytree(build, new_dir / "bin" / "llama" / build.name)
    _write_zip(new_dir / ORIGINAL, new)
    held = package.read(target)
    held.version = source.version
    held.platform, held.arch = catalog.current_platform(), catalog.current_arch()
    for name, sha in source.package.helpers.items():
        if ("python" in parts) if name == "python" else ("bin" in parts):
            held.helpers[name] = sha
    package.write(new_dir, held)

    outcome = Outcome(old, source.version, parts, merged.carried(), merged.set_aside(),
                      aside_name if merged.aside else "", dropped)
    disk.write_text(work / STAGED, json.dumps({**asdict(outcome), "fingerprint": seen}, indent=2))
    return outcome


def switch_staged(target: Path, report: Report = lambda line: None) -> Outcome:
    """Swap in what stage() built. Museion must not be running in the copy."""
    target = Path(target).resolve()
    if switch.recover(target, report):
        raise UpgradeError("an earlier switch had been cut short and has now been finished. Start the upgrade again.")
    plan = staged(target)
    if plan is None:
        raise UpgradeError("nothing is ready to switch to; start the upgrade again")
    seen = plan.pop("fingerprint", "")
    outcome = Outcome(**plan)
    if seen != fingerprint(target / "app"):
        raise UpgradeError("Museion's code in this copy changed while the upgrade was being prepared. "
                           "Start the upgrade again, so the change is carried over too.")
    if "python" in outcome.parts and inside(Path(sys.executable), target / "python"):
        raise UpgradeError("this upgrade replaces the Python that is running it. Start it from the newer copy: "
                           "python -m museion upgrade --into <this folder>.")
    report("Switching to the new version")
    keep = f"{PREVIOUS}/{outcome.old or 'before-versions'}"
    shutil.rmtree(target / keep, ignore_errors=True)
    moves = switch.swap_moves(target, outcome.parts, f"{WORK}/new", keep)
    if outcome.aside_folder:
        shutil.rmtree(target / outcome.aside_folder, ignore_errors=True)
        moves.append((f"{WORK}/new/aside", outcome.aside_folder))
    (target / WORK / STAGED).unlink()
    try:
        switch.switch(target, {"from": outcome.old, "to": outcome.new, "moves": moves, "parts": outcome.parts,
                               "carried": outcome.carried, "aside": outcome.aside,
                               "aside_folder": outcome.aside_folder, "dropped_changes": outcome.dropped_changes}, report)
    except switch.Refused as e:
        disk.write_text(target / WORK / STAGED, json.dumps({**asdict(outcome), "fingerprint": seen}, indent=2))
        raise UpgradeError(str(e)) from e
    # One previous version is kept: the one just replaced.
    for stale in (target / PREVIOUS).iterdir():
        if stale.name != Path(keep).name and not inside(Path(sys.executable), stale):
            shutil.rmtree(stale, ignore_errors=True)
    return outcome


def install(source: Source, target: Path, *, report: Report = lambda line: None, **options) -> Outcome:
    """Put the Museion at `source` into the copy at `target`: stage, then
    switch."""
    stage(source, target, report=report, **options)
    return switch_staged(target, report)


def go_back(target: Path, report: Report = lambda line: None) -> tuple[str, str]:
    """Put back what the last upgrade replaced. Returns (the version left,
    the version returned to). The version left is kept in previous/ in its
    turn, so going back can itself be undone."""
    target = Path(target).resolve()
    switch.recover(target, report)
    old = previous_version(target)
    if old is None:
        raise UpgradeError("no previous version is kept in this copy")
    current = package.code_version(target / "app") or "before-versions"
    kept = target / PREVIOUS / old
    parts = [p for p in PARTS if (kept / p).exists()]
    if "python" in parts and inside(Path(sys.executable), target / "python"):
        raise UpgradeError("going back replaces the Python that is running this. Run it with the previous one: "
                           f"{prepare.python_exe(kept)} -m museion upgrade --back")
    report(f"Going back from {current} to {old}")
    (target / WORK / STAGED).unlink(missing_ok=True)
    # The same version twice (one reinstalled with --force) needs two names.
    leaving = current if current != old else f"{current}-again"
    # What an earlier going-back left there (the Python it ran on) would
    # stand in the way of the parts moving out.
    shutil.rmtree(target / PREVIOUS / leaving, ignore_errors=True)
    moves = switch.swap_moves(target, parts, f"{PREVIOUS}/{old}", f"{PREVIOUS}/{leaving}")
    try:
        switch.switch(target, {"from": current, "to": old, "moves": moves, "parts": parts, "back": True}, report)
    except switch.Refused as e:
        raise UpgradeError(str(e)) from e
    return current, old


# ── small pieces ─────────────────────────────────────────────────────────────

def _shipped(source: Source) -> dict[str, bytes]:
    """The newer version's code as it was shipped: from its own record if it
    is a whole copy (whoever has that copy may have changed its app/), else
    from its files."""
    if source.root is not None:
        shipped = appmerge.read_original(source.root / ORIGINAL)
        if shipped is not None:
            return shipped
    return {f.relative_to(source.app).as_posix(): f.read_bytes() for f in prepare.app_files(source.app)}


def _write_app(folder: Path, merged: appmerge.Result, old: str, new: str, aside_name: str, earlier: str = "") -> None:
    shutil.rmtree(folder, ignore_errors=True)
    for path, data in merged.files.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    notes = "\n\n".join(text.strip() for text in (appmerge.notes(merged, old, new, aside_name), earlier) if text.strip())
    if notes:
        (folder / appmerge.NOTES).write_text(notes + "\n", encoding="utf-8")


def _write_zip(path: Path, files: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files.items()):
            z.writestr(f"app/{name}", data)


def _bring(src: Path, dst: Path, move: bool) -> None:
    """Copy a part of the newer copy into the staging folder; or move it,
    when the newer copy was unpacked only for this and is on the same disk."""
    if move and not inside(Path(sys.executable), src):
        try:
            src.rename(dst)
            return
        except OSError:
            pass  # another disk: copy
    shutil.copytree(src, dst, symlinks=True)
