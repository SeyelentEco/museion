"""delegate: hand part of the work to another agent.

The helper starts with an empty conversation, so its searching and reading
never fill this agent's context: only its answer comes back, cut to about
1,500 characters, with the passages it cited added to this agent's own (so
they can be cited here by number). One level only: a helper cannot delegate.
A helper never gets tools riskier than this agent's, and its approvals still
go to the person.
"""

import re

from museion.agent_tools import READ, Tool

LIMIT = 1500


def run(args: dict, run) -> str:
    answer = run.delegate(args["agent"], args["task"])
    # Its [n] name its own passages; give them this run's numbers.
    numbers = {}
    for i, h in enumerate(answer.sources, start=1):
        key = (h.book, h.page, h.file)
        at = next((j + 1 for j, s in enumerate(run.sources) if (s.book, s.page, s.file) == key), 0)
        if not at:
            run.sources.append(h)
            at = len(run.sources)
        numbers[i] = at
    text = re.sub(r"\[(\d+)\]", lambda m: f"[{numbers.get(int(m.group(1)), '?')}]", answer.text)
    run.calculations.extend(answer.calculations)
    if len(text) > LIMIT:
        text = text[:LIMIT] + " [... cut]"
    listed = "\n".join(f"[{numbers[i]}] {h.doc_title}" for i, h in enumerate(answer.sources, start=1))
    return f"{args['agent']} answered:\n{text}" + (f"\n\nPassages it cited:\n{listed}" if listed else "")


TOOL = Tool(
    name="delegate",
    description=("Hand one part of the work to another agent, which works it out separately and "
                 "returns a short answer with its sources. Use it to research one topic at a time "
                 "without filling your own memory with passages."),
    parameters={
        "agent": {"type": "string", "description": "The other agent's id, like librarian"},
        "task": {"type": "string", "description": "What it should find out or work out, in a sentence or two"},
    },
    required=["agent", "task"],
    risk=READ,
    run=run,
    describe=lambda a: f"ask {a['agent']}: {a['task'][:120]}",
)
