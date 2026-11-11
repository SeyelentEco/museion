"""list_files: what is in the agent's own folder."""

from museion.agent_tools import READ, Tool
from museion.agent_tools._files import workspace


def run(args: dict, run) -> str:
    root = workspace(run)
    if not root.is_dir():
        return "Your folder is empty."
    rows = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and not any(p.startswith(".") for p in rel.parts) and rel.parts[0] not in ("runs", "tests", "chats", "notes"):
            rows.append(f"{rel.as_posix()} ({path.stat().st_size} bytes)")
    return "\n".join(rows) if rows else "Your folder is empty."


TOOL = Tool(
    name="list_files",
    description="List the files in your own folder.",
    parameters={},
    risk=READ,
    run=run,
    describe=lambda a: "list the agent's files",
)
