"""The retrieval check: the cases in evals/retrieval.toml, scored against
what `search` or `gather` prints for each question. The command is
`python -m museion retrieval-eval` (cmd_retrieval_eval in cli.py); this file
holds the cases and the scoring, so they can be tested without an index.

The printed output is what is scored, as a person reading it would judge it:
a case passes when the passages shown contain one of its words, or, for a
question the library cannot answer, when nothing is shown at all.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CASES_PATH = Path(__file__).resolve().parent.parent / "evals" / "retrieval.toml"

# What search and gather print when nothing matches (cli.py).
NOTHING = ("nothing in the library matches", "nothing in the library covers")


@dataclass
class Case:
    question: str
    why: str
    expect: list[str] = field(default_factory=list)
    needs_rerank: bool = False
    find_nothing: bool = False


def cases(path: Path = CASES_PATH) -> list[Case]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, c in enumerate(data.get("case", []), start=1):
        try:
            case = Case(**c)
        except TypeError as e:
            raise ValueError(f"{path.name}, case {i}: {e}") from None
        if not case.find_nothing and not case.expect:
            raise ValueError(f"{path.name}, case {i}: give expect, or find_nothing = true")
        out.append(case)
    return out


def score(case: Case, output: str) -> tuple[bool, str]:
    """Whether the output passes the case, and a few words on why."""
    text = output.lower()
    found_nothing = any(n in text for n in NOTHING)
    if case.find_nothing:
        return (True, "correctly found nothing") if found_nothing else \
            (False, "returned passages for a topic the library does not cover")
    if found_nothing:
        return False, "found nothing"
    hit = [w for w in case.expect if w.lower() in text]
    return bool(hit), f"matched {len(hit)}/{len(case.expect)}: {', '.join(hit) or 'none'}"
