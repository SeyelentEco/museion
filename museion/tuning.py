"""The prompts and limits of the ask loop, as data.

They ship beside this file as tuning.toml and can be overridden, field by
field, by a file of the same name in the app's config folder. That file is
read on every question, so editing a prompt and seeing the new behaviour
takes as long as saving the file -- no restart, no model reload.
"""

from __future__ import annotations

import dataclasses
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# The defaults, with their comments. Also what "create override file" copies,
# so the person editing it starts from the real text and the reasons for it.
DEFAULTS_PATH = Path(__file__).with_name("tuning.toml")


@dataclass
class Tuning:
    system_prompt: str
    rewrite_prompt: str
    answer_tokens: int
    max_rounds: int
    max_continuations: int
    max_history: int
    history_chars: int
    keep: int
    min_rerank_score: float


def defaults() -> Tuning:
    data = tomllib.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    names = {f.name for f in dataclasses.fields(Tuning)}
    return Tuning(**{k: v for k, v in data.items() if k in names})


def load(path: Path | None = None) -> Tuning:
    """The defaults with the override file applied on top. No file means the
    defaults. A file that will not parse, or that gives a field the wrong kind
    of value, is reported on stderr and ignored as a whole: a typo in the
    override must not turn into an empty system prompt."""
    tuning = defaults()
    if path is None:
        return tuning
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return tuning
    try:
        overrides = tomllib.loads(text.lstrip("﻿"))
        problem = _check(overrides)
    except tomllib.TOMLDecodeError as e:
        problem = str(e)
    if problem:
        print(f"  (ignoring {path}: {problem})", file=sys.stderr)
        return tuning
    for field in dataclasses.fields(Tuning):
        if field.name in overrides:
            value = overrides[field.name]
            setattr(tuning, field.name, float(value) if field.type == "float" else value)
    return tuning


def _check(overrides: dict) -> str | None:
    """What is wrong with an override file, or None. Fields it does not name
    are left alone; names Museion does not know are ignored."""
    for field in dataclasses.fields(Tuning):
        if field.name not in overrides:
            continue
        value = overrides[field.name]
        if field.type == "str" and not isinstance(value, str):
            return f"{field.name} should be text"
        if field.type == "int" and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            return f"{field.name} should be a whole number"
        if field.type == "float" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            return f"{field.name} should be a number"
    return None
