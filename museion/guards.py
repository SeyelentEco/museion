"""Guards: what the harness says back to an answer that is not good enough.

Each guard looks at an answer the model gave without calling a tool, and
either lets it stand (returns None) or sends one message back (returns a
Push). A guard fires at most once per run. An agent lists the guards it wants,
by name, in its agent.toml, in the order they are tried.

These were the corrections built into the ask loop, one by one, each after a
failure that is described beside its message. To add a guard: write a
function here taking (text, run) and returning Push or None, add it to GUARDS,
and add a test in tests/test_guards.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from museion.model_text import cited_numbers, has_prose_arithmetic, looks_like_leaked_tool_call

# Said once to an answer that worked a sum out in prose. The calculator is the
# only arithmetic allowed; "20 ÷ 8 = 2.5" written by the model is the 343.75 ml
# failure waiting to happen again.
CALCULATE_FIRST = (
    "That answer did arithmetic in prose. Do every calculation with the calculate "
    "tool, one expression each, then give the answer again reporting each figure "
    "exactly as calculate returned it, with the same citations."
)

CONTINUE = (
    "Your answer was cut off mid-sentence by a length limit. Continue exactly from "
    "where it stopped -- do not repeat anything already said, do not start over, do "
    "not add a preamble -- and finish the answer."
)

CUT_SHORT = (
    "\n\n*(This answer reached its length limit and was cut short. "
    "Ask about the remaining steps separately.)*"
)

# Said to the model when the searches run out, and again if it hands back a
# tool call as text anyway. Without it a planning question that wanted a
# seventh search produced a bare <tool_call> block as its answer.
NO_MORE_TOOLS = (
    "No more searches or calculations are available. Answer now from the passages "
    "already found, citing each by its number."
)

# Said once to a tool call that came back as text, while tools are still on
# offer. Withholding them instead, as before, meant a calculation written out
# as text was never made, and the next turn did the sum in prose unchecked.
CALL_AGAIN = (
    "That tool call came through as text and did not run. Make the call again "
    "with the tool, or answer if you have what you need."
)

# What is shown when the model's last word was still a tool call written out
# as text: that is not an answer.
NO_ANSWER = "The model did not manage an answer to this. Try asking again, perhaps in other words."

# Said once to an answer that cites nothing while passages were on the table.
# A hypothermia answer once came word for word from Where There Is No Doctor --
# kangarooing and all -- with no [n] anywhere, and was labelled general
# knowledge. The model is asked to point at its sources, and told plainly that
# an answer that owes them nothing may stand as it is.
CITE_OR_STAND = (
    "Your answer cites no passages. Every claim taken from the passages above must "
    "carry that passage's number, like [2]. Rewrite the answer with those citations, "
    "and if you cite anything do not say the library has nothing on this. If no part "
    "of it comes from the passages, repeat it unchanged."
)


class RunView(Protocol):
    """What a guard may look at: the run so far."""

    round: int
    max_rounds: int
    sources: list
    calculations: list


@dataclass
class Push:
    message: str  # said to the model as the person
    # True: the next turn gets no tools and must answer. False: tools stay on
    # offer, so the model can do what it was told (calculate, call properly).
    withhold_tools: bool = False


def calculate_first(text: str, run: RunView) -> Push | None:
    """A sum worked out in prose is sent back, tools still on offer, to be
    done by the calculator."""
    if not run.calculations and run.round < run.max_rounds and has_prose_arithmetic(text):
        return Push(CALCULATE_FIRST)
    return None


def call_again(text: str, run: RunView) -> Push | None:
    """A tool call that came back as text is asked for again, properly."""
    if looks_like_leaked_tool_call(text) and run.round < run.max_rounds:
        return Push(CALL_AGAIN)
    return None


def cite_or_stand(text: str, run: RunView) -> Push | None:
    """An answer that cites none of the passages it was handed is asked to
    cite them or stand as it is; one that is still a tool call written as text
    is told to answer. Either way the next turn has no tools.

    Only numbers that name a passage count. A made-up [9], or a year in
    brackets, would otherwise take a place in the renumbering and move every
    real citation after it onto the wrong source."""
    leaked = looks_like_leaked_tool_call(text)
    cited = [n for n in cited_numbers(text) if n <= len(run.sources)]
    if leaked:
        return Push(NO_MORE_TOOLS, withhold_tools=True)
    if not cited and run.sources:
        return Push(CITE_OR_STAND, withhold_tools=True)
    return None


Guard = Callable[[str, RunView], "Push | None"]

GUARDS: dict[str, Guard] = {
    "calculate_first": calculate_first,
    "call_again": call_again,
    "cite_or_stand": cite_or_stand,
}

# The order the ask loop always used. An agent that names guards gets them
# in this order whatever order it lists them in, because the order matters:
# a sum in prose is fixed before citations are asked for.
ORDER = ["calculate_first", "call_again", "cite_or_stand"]


def check(names: list[str], text: str, run: RunView, fired: set[str]) -> tuple[str, Push] | None:
    """The first guard of `names` that has not fired yet and objects to
    `text`, with what it says; None if the answer stands."""
    for name in sorted(names, key=lambda n: ORDER.index(n) if n in ORDER else len(ORDER)):
        if name in fired or name not in GUARDS:
            continue
        push = GUARDS[name](text, run)
        if push is not None:
            return name, push
    return None
