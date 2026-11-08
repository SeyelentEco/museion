"""add_source: add an archive in the library folder to the catalogue.

The same as `python -m museion add-source`: the entry is measured from the
file, with nothing in it that cannot be known offline. It changes Museion's
own catalogue, outside the agent's folder, so the person is always asked.
"""

from pathlib import Path

from museion.agent_tools import ASK, Tool, ToolError

SHELVES = ("survival", "medicine", "making", "reference")


def run(args: dict, run) -> str:
    folder = getattr(run.setting, "library_dir", None)
    if folder is None:
        raise ToolError("There is no library folder on this computer.")
    name = Path(args["file"]).name
    if args["category"] not in SHELVES:
        raise ToolError(f"category must be one of {', '.join(SHELVES)}.")
    from museion import catalog_add
    try:
        target = catalog_add.add_source(Path(folder) / name, args["title"], args["category"])
    except ValueError as e:
        raise ToolError(str(e)) from e
    return f"Added {name} to the catalogue as {target.name}. It shows when the Library tab is reloaded."


TOOL = Tool(
    name="add_source",
    description=("Add a .zim archive that is in the library folder to the catalogue, so it shows "
                 "on the Library tab. The title must be the archive's own title; if you do not "
                 "know it, ask the person."),
    parameters={
        "file": {"type": "string", "description": "The archive's file name, as library_files lists it"},
        "title": {"type": "string"},
        "category": {"type": "string", "description": "survival, medicine, making or reference"},
    },
    required=["file", "title", "category"],
    risk=ASK,
    run=run,
    describe=lambda a: f"add {a['file']} to the catalogue as \"{a['title']}\" on the {a['category']} shelf",
)
