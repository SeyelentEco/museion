"""What the page's Settings can ask about versions and upgrades.

Upgrading this copy happens in two steps, so the page is there to watch the
slow one. The newer copy's own program prepares everything while Museion
goes on running (upgrade_cli's --stage-only), reporting as it goes. Then it
is started once more to make the switch, the page asks Museion to quit, and
that program brings Museion back up, new, for the page to reload.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from museion import appmerge, disk, newer, switch, upgrade, upgrade_cli
from museion.appstate import AppState, UiError
from museion.upgrade import UpgradeError
from museion.version import VERSION
from museion.version import newer as is_newer

EVENT = "upgrade-progress"


def upgrade_state(state: AppState) -> dict:
    """What Settings shows about this copy: its version, whether it can be
    upgraded in place, what the person has changed, and how the last upgrade
    went."""
    out = {"version": VERSION, "root": None, "previous": None, "changes": None, "last": None, "failed": None}
    if not state.drive:
        return out
    root = state.drive.root
    out["root"] = str(root)
    out["previous"] = upgrade.previous_version(root)
    out["changes"] = upgrade.local_changes(root)
    past = switch.history(root)
    out["last"] = past[-1] if past else None
    out["notes"] = (root / "app" / appmerge.NOTES).is_file()
    try:
        out["failed"] = json.loads((root / switch.WORK / upgrade_cli.FAILED).read_text(encoding="utf-8")).get("error")
        # Told once: the page shows it now, and the next visit starts clean.
        (root / switch.WORK / upgrade_cli.FAILED).unlink()
    except (OSError, ValueError, AttributeError):
        pass
    return out


def upgrade_from(state: AppState, path: str, port: int) -> dict:
    """Prepare this copy's upgrade from a download or a newer copy, and
    leave a program waiting to make the switch when Museion quits. The page
    then quits Museion and waits for it to come back."""
    root = _own_root(state)

    def report(line: str) -> None:
        state.emit(EVENT, {"line": line})

    try:
        folder, unpacked = upgrade_cli.newer_copy(Path(path.strip()), root, report)
        source = upgrade.source_of(folder)
        if source.root is None or not (source.app / "museion" / "upgrade_cli.py").is_file():
            raise UpgradeError(f"{path} does not hold a whole copy of Museion that can install itself "
                               "(a download, or a drive of version 0.2.0 or later)")
        if not is_newer(source.version, VERSION):
            raise UpgradeError(f"that is version {source.version}; this copy is already {VERSION}")
        consume = ["--consume"] if unpacked else []
        command, cwd = upgrade_cli.child(source, root, "--stage-only", *consume)
        _watch(command, cwd, report)
        command, cwd = upgrade_cli.child(source, root, "--switch-only", "--wait-port", str(port), "--relaunch", str(port))
        upgrade_cli.spawn(command, cwd, root / switch.WORK / "switch.log")
    except UpgradeError as e:
        raise UiError(str(e)) from e
    return {"to": source.version}


def upgrade_back(state: AppState, port: int) -> dict:
    """Leave a program waiting to take this copy back a version when
    Museion quits."""
    root = _own_root(state)
    old = upgrade.previous_version(root)
    if old is None:
        raise UiError("No previous version is kept in this copy.")
    command, cwd = upgrade_cli.back_command(root)
    upgrade_cli.spawn(command + ["--wait-port", str(port), "--relaunch", str(port)], cwd,
                      root / switch.WORK / "switch.log")
    return {"to": old}


def newer_version(state: AppState) -> dict:
    """Ask GitHub for the latest release. The one thing here that uses the
    internet, and only when the person presses the button."""
    try:
        release = newer.latest()
    except newer.Unknown as e:
        raise UiError(str(e)) from e
    return {"version": release.version, "newer": release.is_newer(), "page": release.page, "notes": release.notes,
            "file": release.file, "size": release.size,
            # Fetching it here needs a copy to put it in and a checksum to hold it to.
            "can_fetch": bool(state.drive and release.url and release.sha256)}


def fetch_upgrade(state: AppState, port: int) -> dict:
    """Download the latest release for this system into this copy, check
    it, and prepare the upgrade from it as upgrade_from does."""
    root = _own_root(state)
    last = [0.0]

    def progress(done: int, total: int) -> None:
        # A line a second is plenty for a download of minutes.
        if time.monotonic() - last[0] >= 1 or done == total:
            last[0] = time.monotonic()
            state.emit(EVENT, {"line": f"Downloading {release.file}: {disk.gb(done)} of {disk.gb(total)}"})

    try:
        release = newer.latest()
        if not release.is_newer():
            raise UiError(f"Version {release.version} is the latest, and this copy is {VERSION}.")
        need = release.size * 4
        if disk.free_space(root) < need:
            raise UiError(f"{root} needs about {disk.gb(need)} free to download and unpack version "
                          f"{release.version}, and has {disk.gb(disk.free_space(root))}.")
        state.emit(EVENT, {"line": f"Downloading {release.file}"})
        file = newer.fetch(release, root / switch.WORK / "downloads", progress)
    except (newer.Unknown, RuntimeError) as e:
        raise UiError(str(e)) from e
    return upgrade_from(state, str(file), port)


def upgrade_other(state: AppState, path: str, with_changes: bool = False) -> str:
    """Put this version into another copy of Museion: an older folder, or a
    drive, with the changes made to this copy's code if asked. Returns what
    was done, for the person."""
    def report(line: str) -> None:
        state.emit(EVENT, {"line": line})

    try:
        target = Path(path.strip()).expanduser()
        if not path.strip() or not target.is_dir():
            raise UpgradeError("choose the folder or drive that holds the other copy")
        upgrade_cli.closed(target)
        outcome = upgrade.install(upgrade.this_museion(), target.resolve(), report=report, tests=upgrade.run_tests,
                                  with_changes=with_changes)
    except UpgradeError as e:
        raise UiError(str(e)) from e
    return "\n".join(upgrade_cli.summary(outcome, target)).strip()


def _own_root(state: AppState) -> Path:
    if not state.drive:
        raise UiError("This Museion runs from a git checkout, which is brought up to date with git pull.")
    return state.drive.root


def _watch(command: list[str], cwd: Path, report) -> None:
    """Run the newer copy's preparing step, passing on what it says."""
    said: list[str] = []
    with subprocess.Popen(command, cwd=cwd, env=upgrade_cli.clean_env(), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") as child:
        for line in child.stdout:
            line = line.strip()
            if line and line != upgrade_cli.READY:
                said.append(line)
                report(line)
    if child.returncode != 0:
        why = next((s for s in reversed(said) if s.startswith("Not upgraded: ")), said[-1] if said else "it said nothing")
        raise UpgradeError(why.removeprefix("Not upgraded: "))
