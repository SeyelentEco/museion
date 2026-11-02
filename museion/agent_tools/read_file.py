"""read_file: read a file in the agent's own folder."""

from museion.agent_tools import READ, Tool, ToolError
from museion.agent_tools._files import inside

LIMIT = 12000


def run(args: dict, run) -> str:
    path = inside(run, args["name"])
    if not path.is_file():
        raise ToolError(f"There is no file {args['name']} in your folder yet. list_files shows what there is.")
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > LIMIT:
        return text[:LIMIT] + f"\n[... {len(text) - LIMIT} more characters not shown]"
    return text or "(the file is empty)"


TOOL = Tool(
    name="read_file",
    description="Read a text file (.md, .txt or .csv) from your own folder, where your earlier work is kept.",
    parameters={"name": {"type": "string", "description": "The file's name, like stores.csv"}},
    required=["name"],
    risk=READ,
    run=run,
    describe=lambda a: f"read {a['name']}",
)
