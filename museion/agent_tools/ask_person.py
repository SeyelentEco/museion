"""ask_person: stop and ask the person something only they know."""

from museion.agent_tools import READ, Tool


def run(args: dict, run) -> str:
    answer = run.person.ask(run.spec.id, args["question"])
    return f"The person answered: {answer}"


TOOL = Tool(
    name="ask_person",
    description=("Ask the person a short question when you need something only they know "
                 "(how many people, what they have, what they see). Wait for the answer."),
    parameters={"question": {"type": "string"}},
    required=["question"],
    risk=READ,
    run=run,
    describe=lambda a: f"ask: {a['question']}",
)
