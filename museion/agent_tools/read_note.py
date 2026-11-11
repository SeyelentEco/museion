"""read_note: read one of the person's notes or records in full."""

from museion import notes
from museion.agent_tools import READ, Tool, ToolError
from museion.agent_tools._notes import folder

LIMIT = 16000


def run(args: dict, run) -> str:
    try:
        text = notes.read(folder(run), args["name"])
    except notes.NoteError as e:
        raise ToolError(f"{e} list_notes shows what there is.") from None
    if len(text) > LIMIT:
        return text[:LIMIT] + f"\n[... {len(text) - LIMIT} more characters not shown]"
    return text or "(the note is empty)"


TOOL = Tool(
    name="read_note",
    description=("Read one of the person's notes or records in full, like stores.csv or "
                 "pump-repair.md. What they wrote is theirs, not the library: say so when you use it."),
    parameters={"name": {"type": "string", "description": "The note's name, like stores.csv"}},
    required=["name"],
    risk=READ,
    run=run,
    describe=lambda a: f"read your note {a['name']}",
)
