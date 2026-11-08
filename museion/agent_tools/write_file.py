"""write_file: write a text file in the agent's own folder.

The file's previous version, if any, is kept in .history/ first.
"""

from museion.agent_tools import WORKSPACE, Tool, ToolError
from museion.agent_tools._files import MAX_BYTES, inside, keep_previous, record


def run(args: dict, run) -> str:
    path = inside(run, args["name"], writing=True)
    text = args["text"]
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ToolError(f"That is too long to write; keep files under {MAX_BYTES // 1000} kB.")
    path.parent.mkdir(parents=True, exist_ok=True)
    keep_previous(run, path)
    if args.get("append"):
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    else:
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n")
    record(run, path)
    return f"Wrote {args['name']} ({len(text)} characters)."


TOOL = Tool(
    name="write_file",
    description=("Write a text file (.md, .txt or .csv) in your own folder, replacing it, or adding "
                 "to its end with append=true. Use it to save work the person will want later."),
    parameters={
        "name": {"type": "string", "description": "The file's name, like stores.csv or lesson.md"},
        "text": {"type": "string", "description": "What to write"},
        "append": {"type": "boolean", "description": "Add to the end instead of replacing"},
    },
    required=["name", "text"],
    risk=WORKSPACE,
    run=run,
    describe=lambda a: f"{'add to' if a.get('append') else 'write'} {a['name']} in the agent's folder",
)
