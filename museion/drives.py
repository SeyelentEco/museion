"""The window's side of drives: Settings › Prepare a drive, and checking the
drive Museion is running from. The work itself is in prepare.py.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from museion import catalog, manifest, prepare
from museion.appstate import AppState, UiError


def prepare_options(state: AppState) -> dict:
    """What a drive can be built from: the bundles, the chat models, and the
    llama.cpp builds this system can carry."""
    bundles = []
    for b in catalog.bundles():
        _, sources = catalog.bundle(b.id)
        bundles.append({"id": b.id, "title": b.title, "size_bytes": sum(s.size_bytes for s in sources)})
    models = [{"id": m.id, "title": m.title, "size_bytes": m.size_bytes, "tiers": m.tiers}
              for m in catalog.models() if m.role == "chat"]
    opencode = next((t for t in catalog.drive_tools() if t.id == "opencode"), None)
    picked = catalog.tool_variant_named(opencode, "cpu") if opencode else None
    return {
        "bundles": bundles,
        "models": models,
        "accelerators": prepare.default_accelerators(),
        "opencode_bytes": picked[1].size_bytes if picked else None,
    }


def plan_from(plan: dict) -> prepare.Plan:
    """A plan as the page sends it: the same fields, paths as text."""
    known = {f.name for f in dataclasses.fields(prepare.Plan)}
    args = {k: v for k, v in plan.items() if k in known}
    if not str(args.get("dest") or "").strip():
        raise UiError("Choose where the drive is first.")
    args["dest"] = Path(str(args["dest"]).strip())
    return prepare.Plan(**args)


def prepare_estimate(state: AppState, plan: dict) -> dict:
    return dataclasses.asdict(prepare.estimate(plan_from(plan)))


def prepare_drive(state: AppState, plan: dict) -> str:
    """Build the drive, reporting as it goes. Everything already in place is
    skipped, so a run that was interrupted continues where it stopped."""
    phase = ""

    def report(p: prepare.Progress) -> None:
        nonlocal phase
        if p.kind == prepare.PHASE:
            phase = p.item
            state.emit("prepare-progress", {"phase": phase, "item": "", "received": 0, "total": 0})
        else:
            state.emit("prepare-progress", {"phase": phase, "item": p.item, "received": p.received, "total": p.total})

    return str(prepare.run(plan_from(plan), report))


def verify_drive(state: AppState) -> str:
    """Check the drive Museion runs from against its manifest. Returns a
    sentence for the person; a damaged file is an error naming it."""
    if not state.drive:
        raise UiError("Museion is not running from a drive.")

    def report(item: str, done: int, total: int) -> None:
        state.emit("verify-progress", {"item": item, "received": done, "total": total})

    problems = manifest.verify(state.drive.root, report)
    if problems:
        raise UiError("; ".join(problems))
    return "Every file in the manifest is as it was when the drive was prepared."
