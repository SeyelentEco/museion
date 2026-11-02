"""What an answer is: its text, where it came from, and what was computed.

Shared by the ask loop (ask.py), the agent harness (harness.py) and the
benchmark (evals.py), so none of them has to import another to name it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from museion.index import Hit

# Where an answer's substance came from -- decided by the tools that ran, and
# written into saved eval runs under these names.
LIBRARY = "library"  # built on passages from the library
LIBRARY_AND_CALCULATION = "library_and_calculation"  # passages plus the calculator
COMPUTED = "computed"  # the calculator alone
# No passage and no sum, but the agent read or wrote files (its stores list,
# its notes, the library folder): the answer is about those, not the world.
OWN_FILES = "own_files"
# The library had nothing and no calculation was involved: whatever the model
# said came from its own training. Shown as unverified.
GENERAL_KNOWLEDGE = "general_knowledge"

# What on_token receives instead of text when everything streamed so far must
# be taken back.
#
# The model narrates before it calls a tool -- "55 × 0.25 teaspoons is 339
# millilitres... let me check" -- and that narration went straight to the
# screen, wrong number and all, before the calculator returned 68. Text is
# streamed optimistically, and if the turn turns out to end in tool calls the
# caller is told to discard it. Only the final turn's text stands.
DISCARD = None


@dataclass
class Exchange:
    """One earlier question and its answer, as the person saw them."""

    question: str
    answer: str


@dataclass
class Calculation:
    expression: str
    result: str


@dataclass
class Answer:
    text: str
    sources: list[Hit]
    reranked: bool
    basis: str  # LIBRARY, LIBRARY_AND_CALCULATION, COMPUTED, OWN_FILES or GENERAL_KNOWLEDGE
    # Every calculation actually performed, verbatim. Shown to the reader as
    # ground truth: the model once reported 343.75 ml for a dose the
    # calculator had correctly returned as 67.8, by relabelling the result and
    # multiplying again in prose. The log makes that contradiction visible
    # instead of trusting the retelling.
    calculations: list[Calculation] = field(default_factory=list)
    # The tools that ran, in order (the seeded search not counted), and how
    # many model turns the answer took. Agent test cases check them.
    tools: list[str] = field(default_factory=list)
    rounds: int = 0
    # Files the agent wrote, relative to its own folder.
    files: list[str] = field(default_factory=list)
    # The run's log file, when the run was logged.
    log: str = ""


FILE_TOOLS = frozenset({"read_file", "write_file", "list_files", "notes", "library_files", "add_source"})


def basis_of(cited: bool, calculated: bool, tools: Sequence[str] = ()) -> str:
    """Where an answer came from, from whether it cites a passage, whether
    the calculator ran and which tools ran -- never from what the model says."""
    if cited:
        return LIBRARY_AND_CALCULATION if calculated else LIBRARY
    if calculated:
        return COMPUTED
    return OWN_FILES if FILE_TOOLS.intersection(tools) else GENERAL_KNOWLEDGE
