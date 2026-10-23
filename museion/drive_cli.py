"""The command-line side of drives: prepare one, check one, and change
Museion from one with opencode (`edit`). Kept apart from cli.py, whose
commands are about the library and its answers.

  python -m museion prepare --dest E:\\ --bundle go-bag --models qwen3-4b-instruct-q4
  python -m museion verify
  python -m museion edit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from museion import appstate, edit, manifest, portable, prepare


def add_commands(commands) -> None:
    c = commands.add_parser("prepare", help="build a drive that runs Museion on any computer")
    c.add_argument("--dest", required=True, type=Path, help="the drive, or a folder to copy to one")
    c.add_argument("--bundle", default="go-bag", help="the collection to carry (default go-bag)")
    c.add_argument("--models", default="qwen3-4b-instruct-q4",
                   help="chat model ids from models/, comma-separated (default qwen3-4b-instruct-q4)")
    c.add_argument("--accelerators", default=",".join(prepare.default_accelerators()),
                   help="llama.cpp builds to carry: cuda, vulkan, cpu, metal (default: "
                        f"{','.join(prepare.default_accelerators())})")
    c.add_argument("--no-index", action="store_true", help="leave the index to be built on first use")
    c.add_argument("--no-opencode", action="store_true", help="leave out opencode")
    c.add_argument("--skeleton", action="store_true", help="everything except content and models")
    c.set_defaults(run=cmd_prepare)

    c = commands.add_parser("verify", help="check a drive's files against their checksums")
    c.add_argument("--drive", type=Path, help="the drive's folder (default: the drive Museion is on)")
    c.set_defaults(run=cmd_verify)

    c = commands.add_parser("edit", help="open opencode on Museion's own code, with the local chat model")
    c.add_argument("--config-dir", type=Path, help="folder holding config.json (default: as the window)")
    c.add_argument("opencode_args", nargs=argparse.REMAINDER, help="passed on to opencode")
    c.set_defaults(run=cmd_edit)


def listed(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


def cmd_prepare(args) -> None:
    plan = prepare.Plan(
        dest=args.dest,
        bundle=args.bundle,
        models=[] if args.skeleton else listed(args.models),
        accelerators=listed(args.accelerators),
        build_index=not args.no_index,
        skeleton=args.skeleton,
        opencode=not args.no_opencode,
    )
    est = prepare.estimate(plan)
    print(f"\nPreparing {plan.dest}\n")
    for name, size in est.items:
        print(f"  {prepare.human(size):>8}  {name}")
    print(f"\n  {prepare.human(est.bytes)} in all, {prepare.human(est.remaining)} still to fetch, "
          f"{prepare.human(est.free_bytes)} free\n")
    last = ""

    def report(p: prepare.Progress) -> None:
        nonlocal last
        if p.kind != prepare.DOWNLOAD and last:
            print()  # end the progress line
            last = ""
        if p.kind == prepare.PHASE:
            print(f"\n== {p.item}")
        elif p.kind == prepare.NOTE:
            print(f"   {p.item}")
        else:
            line = (f"   {p.item}: {p.received * 100 // p.total}%" if p.total > 0
                    else f"   {p.item}: {prepare.human(p.received)}")
            if line != last:
                print(f"\r{line}          ", end="", flush=True)
                last = line

    root = prepare.run(plan, report)
    print(f"\nDrive ready at {root}\n")


def cmd_verify(args) -> None:
    root = args.drive
    if root is None:
        drive = portable.detect()
        if drive is None:
            raise ValueError("Museion is not running from a drive; say which with --drive")
        root = drive.root
    problems = manifest.verify(root, lambda item, done, total: print(f"\r   {item} ({done}/{total})          ",
                                                                  end="", flush=True))
    print()
    if problems:
        print("\n".join(f"  {p}" for p in problems))
        sys.exit(1)
    print(f"Every file in {root / 'MANIFEST.yaml'} is as it was when the drive was prepared.")


def cmd_edit(args) -> None:
    state = appstate.open_state(args.config_dir)
    rest = args.opencode_args[1:] if args.opencode_args[:1] == ["--"] else args.opencode_args
    sys.exit(edit.run(state, rest))
