"""The command-line side of upgrades (upgrade.py does the work).

  python -m museion upgrade museion-0.3.0-windows.zip   this copy, from a download
  python -m museion upgrade E:\\                         this copy, from a newer copy or drive
  python -m museion upgrade --into E:\\                  put this version into that copy
  python -m museion upgrade --back                      return to the version before
  python -m museion changes                             what you have changed in the code

An upgrade is always carried out by the newer copy's own code and Python,
so the first form unpacks the download inside this copy and starts the
third form there. The page's buttons (upgrade_commands.py) do the same in
two steps: prepare while Museion runs, then switch once it has closed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from museion import appmerge, disk, launchers, portable, prepare, sidecar, switch, tools, upgrade
from museion.upgrade import UpgradeError

DEFAULT_PORT = 8070  # app.py's; not imported, so this file loads without the whole app
READY = "READY TO SWITCH"
FAILED = "failed.json"  # in upgrade/: why the last switch started from the page did not happen


def add_commands(commands) -> None:
    c = commands.add_parser("upgrade", help="upgrade this copy from a newer download, or another copy from this one")
    c.add_argument("source", nargs="?", type=Path,
                   help="a newer Museion: the downloaded .zip or .tar.gz, or a folder or drive holding one")
    c.add_argument("--into", type=Path, metavar="FOLDER", help="put this Museion into the copy in that folder")
    c.add_argument("--back", action="store_true", help="go back to the version the last upgrade replaced")
    c.add_argument("--force", action="store_true", help="install it even if it is not newer")
    c.add_argument("--no-test", action="store_true", help="do not run the new version's tests first")
    c.add_argument("--keep-changes", action="store_true",
                   help="keep your changes to the code even if the tests fail with them")
    c.add_argument("--with-changes", action="store_true",
                   help="with --into: take along the changes made to this copy's code as well")
    # How the steps are started by this program itself.
    for flag in ("--consume", "--stage-only", "--switch-only", "--pause"):
        c.add_argument(flag, action="store_true", help=argparse.SUPPRESS)
    c.add_argument("--wait-port", type=int, help=argparse.SUPPRESS)
    c.add_argument("--relaunch", type=int, metavar="PORT", help=argparse.SUPPRESS)
    c.set_defaults(run=cmd_upgrade)

    c = commands.add_parser("changes", help="list what you have changed in Museion's code since it was shipped")
    c.add_argument("--diff", action="store_true", help="show each change line by line")
    c.set_defaults(run=cmd_changes)


def cmd_upgrade(args) -> None:
    code = 0
    try:
        if args.back:
            code = back(args)
        elif args.into:
            into(args)
        elif args.source:
            code = from_source(args)
        else:
            raise UpgradeError("say what to upgrade from (a download or a folder), or --into a folder, or --back")
    except UpgradeError as e:
        print(f"\nNot upgraded: {e}", flush=True)
        code = 1
        if args.relaunch:
            # The page that asked is waiting for Museion to come back, and
            # reads why it came back unchanged.
            disk.write_text(page_root(args) / switch.WORK / FAILED, json.dumps({"error": str(e)}))
    if args.relaunch:
        relaunch(page_root(args), args.relaunch)
    if args.pause:
        input("\nPress Enter to close this window. ")
    if code:
        sys.exit(code)


def page_root(args) -> Path:
    return args.into.resolve() if args.into else own_root()


# ── the three forms ──────────────────────────────────────────────────────────

def from_source(args) -> int:
    """Upgrade this copy from a download or a newer copy."""
    root = own_root()
    folder, unpacked = newer_copy(args.source, root, print)
    source = upgrade.source_of(folder)
    options = dict(force=args.force, keep_changes=args.keep_changes, consume=unpacked)
    if source.root is None or not (source.app / "museion" / "upgrade_cli.py").is_file():
        # Only the code (a checkout), or a version from before upgrades:
        # there is no newer program to hand over to.
        closed(root)
        show(upgrade.install(source, root, report=print, tests=None if args.no_test else upgrade.run_tests, **options), root)
        return 0
    flags = [f for f, on in (("--force", args.force), ("--no-test", args.no_test),
                             ("--keep-changes", args.keep_changes), ("--consume", unpacked)) if on]
    command, folder = child(source, root, *flags)
    if sys.platform == "win32" and "python" in upgrade.parts_to_replace(source, root):
        # Windows will not move a folder a program is running from, and
        # this is running from the Python about to be replaced.
        spawn(command + ["--pause"], folder, None)
        print("The upgrade continues in the new window.")
        return 0
    code = subprocess.run(command, cwd=folder, env=clean_env()).returncode
    switch.tidy(root)  # the unpacked download, which that program was running from
    return code


def into(args) -> None:
    """Put this Museion into another copy: all of it, or one of the two
    steps when the page is driving."""
    target = args.into.resolve()
    source = upgrade.this_museion()
    tests = None if args.no_test else upgrade.run_tests
    options = dict(force=args.force, consume=args.consume, tests=tests, keep_changes=args.keep_changes,
                   with_changes=args.with_changes)
    if args.stage_only:
        upgrade.stage(source, target, report=say, **options)
        say(READY)
        return
    if args.wait_port:
        wait_closed(args.wait_port)
    closed(target)
    if args.switch_only:
        outcome = upgrade.switch_staged(target, say)
    else:
        outcome = upgrade.install(source, target, report=say, **options)
    (target / switch.WORK / FAILED).unlink(missing_ok=True)
    show(outcome, target)


def back(args) -> int:
    root = own_root()
    command, folder = back_command(root)
    if command[0] != sys.executable and not upgrade.inside(Path(sys.executable), root / switch.PREVIOUS):
        # Going back replaces the Python running this, so the Python being
        # gone back to does it.
        if sys.platform == "win32":
            spawn(command + ["--pause"], folder, None)
            print("Going back continues in the new window.")
            return 0
        return subprocess.run(command, cwd=folder, env=clean_env()).returncode
    if args.wait_port:
        wait_closed(args.wait_port)
    closed(root)
    left, now = upgrade.go_back(root, say)
    (root / switch.WORK / FAILED).unlink(missing_ok=True)
    print(f"\nMuseion in {root} is version {now} again. Version {left} is kept in "
          f"{switch.PREVIOUS}/{left}; `upgrade --back` once more returns to it.", flush=True)
    return 0


def back_command(root: Path) -> tuple[list[str], Path]:
    """The command that takes this copy back a version, and the folder to
    run it from: with the kept Python when the upgrade replaced it."""
    kept = root / switch.PREVIOUS / (upgrade.previous_version(root) or "")
    python = prepare.python_exe(kept) if (kept / "python").is_dir() else Path(sys.executable)
    return [str(python), "-m", "museion", "upgrade", "--back"], root / "app"


def cmd_changes(args) -> None:
    root = own_root()
    changes = upgrade.local_changes(root)
    if changes is None:
        raise ValueError(f"{root} has no record of the code as it was shipped ({upgrade.ORIGINAL})")
    if not changes:
        print("The code is as it was shipped.")
        return
    one = len(changes) == 1
    print(f"{len(changes)} file{' differs' if one else 's differ'} from the code as it was shipped. "
          f"An upgrade carries {'it' if one else 'these'} over.\n")
    base = appmerge.read_original(root / upgrade.ORIGINAL) or {}
    for path, kind in changes.items():
        print(f"  {kind:8} {path}")
        if args.diff and kind == "changed":
            print(appmerge.diff(path, base[path], (root / "app" / path).read_bytes()))


# ── pieces the page's buttons use too ────────────────────────────────────────

def own_root() -> Path:
    """The top folder of the copy this code is running from."""
    drive = portable.detect()
    if drive is None:
        raise UpgradeError(f"this Museion ({prepare.APP_DIR}) is not a downloaded copy or a drive. "
                           "A copy made with git is brought up to date with git pull.")
    return drive.root


def newer_copy(path: Path, root: Path, report=lambda line: None) -> tuple[Path, bool]:
    """The folder of the copy at `path`, unpacking it first if it is a
    download. Returns (the folder, whether it was unpacked here)."""
    path = Path(path).expanduser()
    if path.is_dir():
        return path, False
    if not path.is_file():
        raise UpgradeError(f"there is no file or folder {path}")
    if not path.name.lower().endswith((".zip", ".tar.gz", ".tgz")):
        raise UpgradeError(f"{path.name} is not a Museion download (a .zip or a .tar.gz)")
    incoming = root / switch.WORK / "incoming"
    switch.recover(root)
    need = path.stat().st_size * 3
    if disk.free_space(root) < need:
        raise UpgradeError(f"{root} needs about {disk.gb(need)} free to unpack {path.name} and has "
                           f"{disk.gb(disk.free_space(root))}")
    if incoming.exists():
        shutil.rmtree(incoming)
    report(f"Unpacking {path.name}")
    try:
        tools.extract(path, incoming)
    except RuntimeError as e:
        raise UpgradeError(str(e)) from e
    # The Windows zip holds the files themselves; the others, one folder.
    for folder in (incoming, *sorted(p for p in incoming.iterdir() if p.is_dir())):
        if (folder / portable.MARKER).is_file():
            return folder, True
    raise UpgradeError(f"{path.name} does not hold a copy of Museion")


def child(source: upgrade.Source, target: Path, *flags: str) -> tuple[list[str], Path]:
    """The command that has the newer copy install itself into `target`,
    and the folder to run it from."""
    return ([str(prepare.python_exe(source.root)), "-m", "museion", "upgrade", "--into", str(target), *flags],
            source.app)


def clean_env() -> dict[str, str]:
    """This environment without what would point another Python at this
    one's files."""
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}


def spawn(command: list[str], folder: Path, log: Path | None) -> None:
    """Start a program that outlives this one: in a window of its own on
    Windows, in the background writing to `log` elsewhere."""
    if sys.platform == "win32":
        subprocess.Popen(command, cwd=folder, env=clean_env(), creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    out = open(log, "ab") if log else subprocess.DEVNULL
    subprocess.Popen(command, cwd=folder, env=clean_env(), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)


def relaunch(root: Path, port: int) -> None:
    """Start the copy's Museion again for the page that is waiting for it."""
    name = next(iter(launchers.launchers()))
    command = [str(root / name), "--no-browser", "--port", str(port)]
    if sys.platform == "win32":
        command = ["cmd", "/c", *command]
    spawn(command, root, root / switch.WORK / "museion.log")


def museion_at(port: int) -> dict | None:
    """What the Museion serving on this port says of itself, if one is."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=2) as res:
            hello = json.load(res)
        return hello if isinstance(hello, dict) and hello.get("app") == "museion" else None
    except (OSError, ValueError):
        return None


def closed(target: Path, port: int = DEFAULT_PORT) -> None:
    """Refuse to change a copy that Museion is running from."""
    hello = museion_at(port)
    if hello is None:
        return
    # Version 0.1.0 does not say which folder it runs from.
    where = hello.get("root")
    if where is None or (where and _same_folder(where, target)):
        raise UpgradeError("Museion is running. Close it (close its browser tab and its window), then run this again.")


def _same_folder(a: str | Path, b: str | Path) -> bool:
    # realpath: one folder can be reached by two spellings (a link, a short
    # Windows name), and a copy must not be changed for want of noticing.
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def wait_closed(port: int, seconds: int = 120) -> None:
    """Wait for the Museion on this port to stop, as the page asked it to."""
    until = time.monotonic() + seconds
    while sidecar.port_open(port):
        if time.monotonic() > until:
            raise UpgradeError("Museion did not stop, so nothing was changed.")
        time.sleep(0.5)


def say(line: str) -> None:
    print(line, flush=True)


def show(outcome: upgrade.Outcome, target: Path) -> None:
    for line in summary(outcome, target):
        print(line)


def summary(outcome: upgrade.Outcome, target: Path) -> list[str]:
    """What an upgrade did, for the person."""
    names = {"app": "the code", "python": "the Python that runs it", "bin": "the helper programs"}
    was = f" (was {outcome.old})." if outcome.old and outcome.old != outcome.new else "."
    lines = ["", f"Museion in {target} is now version {outcome.new}{was}",
             f"Replaced: {', '.join(names[p] for p in outcome.parts)}. "
             "Your library, models, notes and settings were not touched."]
    if outcome.carried:
        n = len(outcome.carried)
        lines.append(f"{n} change{'s' if n != 1 else ''} you had made to the code "
                     f"{'were' if n != 1 else 'was'} carried over: {', '.join(outcome.carried)}.")
    if outcome.brought:
        n = len(outcome.brought)
        lines.append(f"{n} change{'s' if n != 1 else ''} made to this copy's code {'were' if n != 1 else 'was'} "
                     f"taken along: {', '.join(outcome.brought)}.")
    if outcome.left_behind:
        n = len(outcome.left_behind)
        lines.append(f"The new version did not pass its tests with the change{'s' if n != 1 else ''} made to this "
                     f"copy's code, so {'they were' if n != 1 else 'it was'} not taken along: "
                     f"{', '.join(outcome.left_behind)}. {'They are' if n != 1 else 'It is'} still in this copy.")
    if outcome.aside:
        n = len(outcome.aside)
        why = ("The new version did not pass its tests with your changes in it, so they were all set aside"
               if outcome.dropped_changes else
               f"{n} change{'s' if n != 1 else ''} could not be carried over")
        lines += [f"{why}: {', '.join(outcome.aside)}.",
                  f"Nothing is lost: your files are in {outcome.aside_folder}, and app/{appmerge.NOTES} "
                  "says how to put each change back (Edit Museion can do it)."]
    launcher = next(iter(launchers.launchers()))
    lines.append(f"The version before is kept in {switch.PREVIOUS}/{outcome.old or 'before-versions'}. "
                 f"To go back to it: {launcher} upgrade --back")
    return lines
