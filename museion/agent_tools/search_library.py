"""search_library: search the offline library and number the passages.

The searching itself -- keyword search, the archives' own indexes through
kiwix-serve, the reranker -- is the library's (ask.py, Asker.gather). This
file only offers it to the model and numbers what comes back, continuously
across searches, so a citation names one passage for the whole run.
"""

from museion.agent_tools import READ, Tool

DESCRIPTION = ("Search the offline reference library. Returns numbered passages with the "
               "document title and page. Cite passages by their number.")


def run(args: dict, run) -> str:
    if run.library is None:
        return "The library is not available."
    from museion.ask import render_search
    hits, reranked = run.library.gather(args["query"])
    run.reranked |= reranked
    return render_search(hits, run.sources)


TOOL = Tool(
    name="search_library",
    description=DESCRIPTION,
    parameters={"query": {"type": "string", "description": "What to search for, as keywords or a short phrase"}},
    required=["query"],
    risk=READ,
    run=run,
    describe=lambda a: f"search the library for \"{a['query']}\"",
)
