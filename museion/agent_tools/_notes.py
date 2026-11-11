"""Shared by the tools that use the person's notes folder (notes.py)."""

from pathlib import Path

from museion.agent_tools import ToolError


def folder(run) -> Path:
    found = getattr(run.setting, "notes_dir", None)
    if found is None:
        raise ToolError("There is no notes folder here: no library is set up.")
    return Path(found)


def written(run, name: str) -> None:
    shown = f"notes/{name.strip().replace(chr(92), '/')}"
    if shown not in run.files:
        run.files.append(shown)
