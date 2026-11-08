"""Keeping a run's conversation inside the model's context.

When it would not fit, the harness folds old tool results away -- by fixed
rules, never by asking the model to summarise. Local models drop numbers when
they summarise, and a summary cannot be checked afterwards; a rule can.

The rules, in order:

1. Never touched: the system prompt, every message from the person (the goal,
   their answers, the guards' messages), and the last `keep_rounds` rounds.
2. Old tool results go first, oldest first, each replaced by a one-line note.
   A search's note keeps its passage numbers: the passages themselves stay in
   the harness, so a citation of [2] still opens the right page.
   Calculator results are never removed: they are short, and they are the
   numbers the answer must report.
3. Then the text of old answers is cut to its first 300 characters.

If it still does not fit, the harness has the model answer with what it has.
Every fold is written to the run's log.
"""

from __future__ import annotations

import json
import re

KEEP_TEXT = 300
STUB_MARK = "[folded] "
CUT_MARK = " [... cut to save room]"


def prompt_chars(messages: list[dict]) -> int:
    """The size of a conversation, in characters, as the model will read it."""
    return sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls") or "")) for m in messages)


def compact(messages: list[dict], room: int, keep_rounds: int) -> list[str]:
    """Fold old material away, in place, until the conversation fits in
    `room` characters or nothing more may go. Returns a line per fold."""
    removed: list[str] = []
    protected = protected_from(messages, keep_rounds)
    calls = calls_by_id(messages)

    for i, m in enumerate(messages):
        if prompt_chars(messages) <= room:
            return removed
        if i >= protected or m.get("role") != "tool" or (m.get("content") or "").startswith(STUB_MARK):
            continue
        name, args = calls.get(m.get("tool_call_id", ""), ("tool", {}))
        if name == "calculate":
            continue
        m["content"] = stub(name, args, m.get("content") or "")
        removed.append(m["content"])

    for i, m in enumerate(messages):
        if prompt_chars(messages) <= room:
            break
        if i >= protected or m.get("role") != "assistant":
            continue
        text = m.get("content") or ""
        if len(text) > KEEP_TEXT and not text.endswith(CUT_MARK):
            m["content"] = text[:KEEP_TEXT] + CUT_MARK
            removed.append(f"an earlier reply cut to {KEEP_TEXT} characters")
    return removed


def protected_from(messages: list[dict], keep_rounds: int) -> int:
    """The index of the first message of the last `keep_rounds` rounds. A
    round starts with each assistant message that makes tool calls."""
    starts = [i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls")]
    if keep_rounds <= 0 or not starts:
        return len(messages)
    if len(starts) <= keep_rounds:
        return starts[0]
    return starts[-keep_rounds]


def calls_by_id(messages: list[dict]) -> dict[str, tuple[str, dict]]:
    out = {}
    for m in messages:
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            out[call.get("id", "")] = (fn.get("name", "tool"), args if isinstance(args, dict) else {})
    return out


def stub(name: str, args: dict, content: str) -> str:
    """The note left where a tool result was."""
    numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", content, flags=re.MULTILINE)]
    what = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items())
    note = f"{STUB_MARK}[{name}({what}): result removed to save room"
    if numbers:
        span = f"{min(numbers)}-{max(numbers)}" if len(numbers) > 1 else str(numbers[0])
        note += f"; it showed passages {span}, which may still be cited by number"
    return note + "]"
