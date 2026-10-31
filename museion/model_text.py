"""Reading what the model wrote: the passages it cites, the searches it
suggests, and the two things it must not do -- arithmetic in prose, and a
tool call written out as text instead of made.
"""

from __future__ import annotations

import re
import unicodedata

_DIGITS = set("0123456789")
_MARKER_CHARS = set("0123456789, ")


def cited_numbers(text: str) -> list[int]:
    """The source numbers an answer cites, in order of first appearance.
    Handles [2], [4][5] and [4, 5]."""
    out: list[int] = []
    i = 0
    while i < len(text):
        end = text.find("]", i) if text[i] == "[" else -1
        if end == -1:
            i += 1
            continue
        inner = text[i + 1:end]
        if inner and set(inner) <= _MARKER_CHARS:
            for token in inner.split(","):
                token = token.strip()
                if token and set(token) <= _DIGITS and int(token) > 0 and int(token) not in out:
                    out.append(int(token))
        i = end + 1
    return out


def parse_queries(text: str, question: str) -> list[str]:
    """Turn the rewrite's reply into searches: one per line, bullets,
    numbering and quotes stripped, nothing empty, nothing long enough to be a
    sentence, nothing that merely repeats the question, at most three."""
    out: list[str] = []
    for line in text.splitlines():
        q = line.strip().lstrip("-*•0123456789.) ").strip("\"'`").strip()
        if not q or q.endswith(":"):
            continue
        if len(q.split()) > 10 or q.lower() == question.strip().lower():
            continue
        if any(o.lower() == q.lower() for o in out):
            continue
        out.append(q)
        if len(out) == 3:
            break
    return out


def strip_citations(text: str) -> str:
    """Remove [n] citation markers from an earlier answer before it is
    replayed as history, so their numbers are not carried into a new answer
    whose passages are numbered afresh."""
    return renumber_or_strip(text, None)


def renumber_citations(text: str, cited: list[int]) -> str:
    """Rewrite [n] markers so the kept sources number 1..k in citation order.
    The reader sees a source list that matches the markers, with no gaps for
    passages the model was handed and never used. A marker naming none of
    them goes: it would be a citation of nothing."""
    return renumber_or_strip(text, cited)


def renumber_or_strip(text: str, cited: list[int] | None) -> str:
    """Shared walker: with `cited` it renumbers markers and removes those
    naming nothing cited, without it removes them all."""
    out = ""
    rest = text
    while (start := rest.find("[")) != -1:
        out += rest[:start]
        after = rest[start:]
        end = after.find("]")
        if end == -1:
            out += after
            rest = ""
            break
        inner = after[1:end]
        is_marker = bool(inner) and set(inner) <= _MARKER_CHARS and bool(set(inner) & _DIGITS)
        mapped = []
        if is_marker and cited is not None:
            numbers = [int(t) for t in (t.strip() for t in inner.split(",")) if t and set(t) <= _DIGITS]
            mapped = [str(cited.index(n) + 1) for n in numbers if n in cited]
        if not is_marker:
            out += after[:end + 1]
        elif mapped:
            out += f"[{', '.join(mapped)}]"
        elif out.endswith(" ") and after[end + 1:end + 2] in (".", ",", ";", ":"):
            # Stripped, and the space it leaves before a full stop goes too.
            out = out[:-1]
        rest = after[end + 1:]
    return out + rest


_OPERATORS = {"×", "÷", "*", "/", "+", "x", "X"}
_NUMBER = re.compile(r"(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", re.ASCII)
# What is around a number in prose: "(2", "litres,", "8." -- everything before
# the first digit or point and after the last.
_EDGES = re.compile(r"^[^0-9.]+|[^0-9.]+$")


def has_prose_arithmetic(text: str) -> bool:
    """Does the text work a sum out itself -- "4 × 2 = 8", "20 ÷ 8 = 2.5"? A
    number, an operator, a number and an equals sign followed by a number, in
    that order. Ranges ("5-10 minutes"), dates and "pH = 7" do not match."""
    # Space out the symbols so "4×2=8" and "4 × 2 = 8" split alike. An 'x'
    # counts as multiplication only between digits.
    spaced = ""
    for i, c in enumerate(text):
        between_digits = 0 < i < len(text) - 1 and text[i - 1] in _DIGITS and text[i + 1] in _DIGITS
        if c in "×÷=*/+" or (c in "xX" and between_digits):
            spaced += f" {c} "
        else:
            spaced += c

    def is_number(token: str) -> bool:
        # ¼, ½ and the like count as digits: the sources write doses so, and
        # "55 × ¼ tsp = 339 ml" is as much a sum done in prose as "55 × 0.25".
        token = "".join("1" if not c.isascii() and unicodedata.numeric(c, None) is not None else c
                        for c in token)
        t = _EDGES.sub("", token).replace(",", "")
        return bool(_NUMBER.fullmatch(t))

    tokens = spaced.split()
    for e, token in enumerate(tokens):
        if token != "=" or e + 1 >= len(tokens) or not is_number(tokens[e + 1]):
            continue
        # In the eight tokens before the '=': a number, then an operator, then
        # a number, with unit words allowed between them.
        number_before = operator = number_after = False
        for t in tokens[max(e - 8, 0):e]:
            if t in _OPERATORS:
                operator = operator or number_before
            elif is_number(t):
                if operator:
                    number_after = True
                else:
                    number_before = True
        if number_before and operator and number_after:
            return True
    return False


def looks_like_leaked_tool_call(text: str) -> bool:
    """A tool call the chat template failed to parse comes back as prose:
    `<tool_call>...`, or Qwen's `<function=search_library>` form. It is not an
    answer and must not be shown as one."""
    t = text.lstrip()
    return t.startswith("<tool_call>") or t.startswith("<function=")
