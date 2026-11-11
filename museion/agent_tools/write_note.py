"""write_note: write one of the person's notes or records, or add to its end.

The notes folder is the person's, not the agent's, so every write waits for
them to press Allow. The version before is kept in notes/.history/.
"""

from museion import notes
from museion.agent_tools import ASK, Tool, ToolError
from museion.agent_tools._notes import folder, written


def run(args: dict, run) -> str:
    try:
        notes.write(folder(run), args["name"], args["text"], bool(args.get("append")))
    except notes.NoteError as e:
        raise ToolError(str(e)) from None
    written(run, args["name"])
    return f"Saved {args['name']} in the notes."


# Lines of a change shown on the Allow / Deny card.
SHOWN_LINES = 12


def describe(a: dict, run=None) -> str:
    """What the write would change, so the person allows a change they have
    seen: the lines added, or for a rewrite of an existing note the lines
    that differ from what is there now."""
    text = a.get("text", "")
    old = None
    if run is not None and not a.get("append"):
        try:
            old = notes.read(folder(run), a["name"])
        except (notes.NoteError, ToolError, OSError):
            old = None
    if old is None:
        verb = "add to" if a.get("append") else "write"
        lines = text.splitlines()
        shown = lines[:SHOWN_LINES] + ([f"... and {len(lines) - SHOWN_LINES} more lines"] if len(lines) > SHOWN_LINES else [])
        return f"{verb} your note {a['name']}:\n" + "\n".join(shown)
    import difflib
    diff = [d for d in difflib.unified_diff(old.splitlines(), text.splitlines(), lineterm="", n=0)
            if d[:1] in "+-" and not d.startswith(("+++", "---"))]
    if not diff:
        return f"write your note {a['name']} again, unchanged"
    shown = diff[:SHOWN_LINES] + ([f"... and {len(diff) - SHOWN_LINES} more changed lines"] if len(diff) > SHOWN_LINES else [])
    return f"change your note {a['name']} (+ added, - removed):\n" + "\n".join(shown)


TOOL = Tool(
    name="write_note",
    description=("Write one of the person's notes or records (.md, .txt or .csv) in the notes "
                 "folder, replacing it, or adding to its end with append=true. The person must "
                 "allow each write. For a record, read it first and write it back whole with "
                 "the same header line."),
    parameters={
        "name": {"type": "string", "description": "The note's name, like stores.csv or pump-repair.md"},
        "text": {"type": "string", "description": "What to write"},
        "append": {"type": "boolean", "description": "Add to the end instead of replacing"},
    },
    required=["name", "text"],
    risk=ASK,
    run=run,
    describe=describe,
)
