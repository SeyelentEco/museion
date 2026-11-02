"""read_page: more of a passage already found, by its number.

A search shows the first part of each page. This shows all of it, which an
agent writing a lesson or a procedure needs.
"""

from museion.agent_tools import READ, Tool, ToolError

LIMIT = 8000


def run(args: dict, run) -> str:
    n = args["number"]
    if not 1 <= n <= len(run.sources):
        raise ToolError(f"There is no passage [{n}]; search first, then read a passage by its number.")
    h = run.sources[n - 1]
    where = f"page {h.page}" if h.page > 0 else h.book
    return f"[{n}] {h.doc_title} ({where}), in full:\n{h.body[:LIMIT]}"


TOOL = Tool(
    name="read_page",
    description="Read the whole of a passage already found by search_library, by its number.",
    parameters={"number": {"type": "integer", "description": "The passage's number, as in [3]"}},
    required=["number"],
    risk=READ,
    run=run,
    describe=lambda a: f"read passage [{a['number']}] in full",
)
