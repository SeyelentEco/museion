"""Shared by the tools that read and write the agent's own folder: keeping
every path inside it."""

from pathlib import Path

from museion.agent_tools import ToolError

# What an agent may write: text a person can open anywhere.
SUFFIXES = (".md", ".txt", ".csv")
MAX_BYTES = 200_000


def workspace(run) -> Path:
    if run.workspace is None:
        raise ToolError("This agent has no folder of its own to keep files in.")
    return Path(run.workspace)


def inside(run, name: str, writing: bool = False) -> Path:
    """The file `name` in the agent's folder, refusing anything outside it."""
    root = workspace(run).resolve()
    name = (name or "").strip().replace("\\", "/")
    if not name or name.startswith("/") or ":" in name:
        raise ToolError(f"{name!r} is not a file name in your folder. Use a plain name like stores.csv.")
    path = (root / name).resolve()
    if root not in path.parents:
        raise ToolError(f"{name} is outside your folder; you can only use files inside it.")
    if any(part.startswith(".") for part in path.relative_to(root).parts):
        raise ToolError(f"{name}: names starting with a dot are not allowed.")
    if writing and path.suffix.lower() not in SUFFIXES:
        raise ToolError(f"You can only write {', '.join(SUFFIXES)} files.")
    return path


def keep_previous(run, path: Path) -> None:
    """Before a file is changed, its last version goes to .history/, so
    every write can be undone by hand."""
    if path.is_file():
        root = workspace(run).resolve()
        history = root / ".history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
        rel = path.relative_to(root).as_posix().replace("/", "__")
        # Two writes in one second (two notes in one turn) must not share a
        # name, or the second copy replaces the first and that version is lost.
        target, n = history / f"{stamp}-{rel}", 2
        while target.exists():
            target, n = history / f"{stamp}-{n}-{rel}", n + 1
        target.write_bytes(path.read_bytes())


def record(run, path: Path) -> None:
    rel = path.resolve().relative_to(workspace(run).resolve()).as_posix()
    if rel not in run.files:
        run.files.append(rel)
