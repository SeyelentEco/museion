"""The Notes tab's commands: list, read, save and start notes and records.

The files are notes.py's; this only finds the folder for this computer and
words problems for the person.
"""

from __future__ import annotations

from pathlib import Path

from museion import notes
from museion.appstate import AppState, UiError


def folder(state: AppState) -> Path:
    found = state.notes_dir(state.config())
    if found is None:
        raise UiError("No library is set up yet, so there is nowhere to keep notes.")
    return found


def list_notes(state: AppState) -> dict:
    """The notes and records, and the kinds of record that can be started."""
    where = folder(state)
    have = {f.name for f in notes.listing(where)}
    return {
        "folder": str(where),
        "notes": [{"name": f.name, "size": f.size, "modified": f.modified} for f in notes.listing(where)],
        "kinds": [{"file": k.file, "title": k.title, "about": k.about, "columns": k.columns,
                   "have": k.file in have} for k in notes.record_kinds()],
    }


def read_note(state: AppState, name: str) -> dict:
    where = folder(state)
    try:
        return {"name": name, "text": notes.read(where, name), "path": str(notes.path_of(where, name))}
    except notes.NoteError as e:
        raise UiError(str(e)) from None


def save_note(state: AppState, name: str, text: str) -> dict:
    """Save a note from the page. The version before is kept, as for an
    agent's write."""
    where = folder(state)
    try:
        notes.write(where, name, text)
    except notes.NoteError as e:
        raise UiError(str(e)) from None
    return read_note(state, name)


def new_note(state: AppState, name: str, record: str = "") -> dict:
    """Start a note (empty, or with a heading) or one of the known records."""
    where = folder(state)
    try:
        if record:
            notes.start_record(where, record)
            return read_note(state, record)
        name = name.strip()
        if name and "." not in Path(name).name:
            name += ".md"
        path = notes.path_of(where, name)
        if path.exists():
            raise UiError(f"There is already a note called {name}.")
        title = Path(name).stem.replace("-", " ").replace("_", " ").strip().capitalize()
        notes.write(where, name, f"# {title}\n" if name.lower().endswith(".md") else "")
    except notes.NoteError as e:
        raise UiError(str(e)) from None
    return read_note(state, name)


def open_notes_folder(state: AppState) -> None:
    from museion.commands import open_path
    where = folder(state)
    where.mkdir(parents=True, exist_ok=True)
    open_path(state, str(where))
