"""library_files: the archives in the library folder, and whether each is in
the catalogue (and so on the Library tab's shelves)."""

from pathlib import Path

from museion.agent_tools import READ, Tool, ToolError


def run(args: dict, run) -> str:
    folder = getattr(run.setting, "library_dir", None)
    if folder is None or not Path(folder).is_dir():
        raise ToolError("There is no library folder on this computer.")
    from museion import catalog
    known = {s.filename for s in catalog.sources()}
    rows = []
    for path in sorted(Path(folder).glob("*.zim")):
        state = "in the catalogue" if path.name in known else "NOT in the catalogue"
        rows.append(f"{path.name} ({path.stat().st_size // 1_000_000} MB): {state}")
    return "\n".join(rows) if rows else "There are no .zim archives in the library folder."


TOOL = Tool(
    name="library_files",
    description="List the .zim archives in the library folder and whether each is in the catalogue.",
    parameters={},
    risk=READ,
    run=run,
    describe=lambda a: "list the archives in the library folder",
)
