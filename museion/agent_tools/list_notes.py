"""list_notes: the person's notes and records, in the library's notes folder."""

import time

from museion import notes
from museion.agent_tools import READ, Tool
from museion.agent_tools._notes import folder


def run(args: dict, run) -> str:
    files = notes.listing(folder(run))
    today = f"Today is {time.strftime('%Y-%m-%d')}."
    if not files:
        return f"There are no notes yet. {today}"
    return today + "\n" + "\n".join(f"{f.name} ({f.size} bytes, changed {time.strftime('%Y-%m-%d', time.localtime(f.modified))})"
                     for f in files)


TOOL = Tool(
    name="list_notes",
    description=("List the person's own notes and records (the notes folder): Markdown notes and "
                 "CSV records such as stores.csv, medicines.csv or maintenance.csv."),
    parameters={},
    risk=READ,
    run=run,
    describe=lambda a: "list your notes",
)
