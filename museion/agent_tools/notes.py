"""notes: the agent's memory, notes.md in its own folder.

What is in notes.md is put in the agent's prompt at the start of every run
(the last few thousand characters), and the person can read and edit it.
"""

import time

from museion.agent_tools import WORKSPACE, Tool, ToolError
from museion.agent_tools._files import inside, keep_previous, record


def run(args: dict, run) -> str:
    path = inside(run, "notes.md")
    action = args.get("action", "read")
    if action == "read":
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return "Your own working notes are empty. (The person's notes and records are separate: list_notes, read_note.)"
    text = (args.get("text") or "").strip()
    if not text:
        raise ToolError("Give the text to note.")
    path.parent.mkdir(parents=True, exist_ok=True)
    keep_previous(run, path)
    if action == "replace":
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
    elif action == "add":
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(f"- {time.strftime('%Y-%m-%d')}: {text}\n")
    else:
        raise ToolError("action must be read, add or replace.")
    record(run, path)
    return "Noted."


TOOL = Tool(
    name="notes",
    description=("Your notes, kept between conversations: read them, add a line, or replace them. "
                 "Note what you will need next time (the household's size, what was decided), "
                 "not facts from the library."),
    parameters={
        "action": {"type": "string", "description": "read, add or replace"},
        "text": {"type": "string", "description": "For add or replace: the note"},
    },
    required=["action"],
    risk=WORKSPACE,
    run=run,
    describe=lambda a: "read the agent's notes" if a.get("action") == "read" else f"{a.get('action')} a note: {a.get('text', '')[:120]}",
)
