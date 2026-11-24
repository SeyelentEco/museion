"""Merging two sets of changes to one text file, line by line.

An upgrade meets a file three times: as Museion shipped it before (the
base), as the person has it now (theirs to keep), and as the new version
ships it. When only one side changed a stretch of lines, that side's lines
are taken; when both changed the same stretch in the same way, likewise.
When both changed the same stretch differently there is no right answer to
compute, and merge() says so instead of guessing: the caller keeps both
files and a person, or the coding assistant, decides.

This is what `git merge` does to a file, written with difflib because a
drive has no git on it.
"""

from __future__ import annotations

from difflib import SequenceMatcher


def merge(base: list[str], mine: list[str], new: list[str]) -> list[str] | None:
    """The lines of `base` with both the changes that made it `mine` and the
    changes that made it `new`, or None if they collide."""
    out: list[str] = []
    at_base = at_mine = at_new = 0
    for base_start, base_end, mine_start, new_start in _agreed(base, mine, new):
        # The stretch before this agreed run, as each of the three has it.
        was = base[at_base:base_start]
        me = mine[at_mine:mine_start]
        them = new[at_new:new_start]
        if me == was:
            out += them
        elif them == was or them == me:
            out += me
        else:
            return None
        length = base_end - base_start
        out += base[base_start:base_end]
        at_base, at_mine, at_new = base_end, mine_start + length, new_start + length
    return out


def _agreed(base: list[str], mine: list[str], new: list[str]) -> list[tuple[int, int, int, int]]:
    """Runs of lines that all three have unchanged, in order, as (start in
    base, end in base, start in mine, start in new); a last empty run marks
    the ends of the three."""
    with_mine = SequenceMatcher(None, base, mine, autojunk=False).get_matching_blocks()
    with_new = SequenceMatcher(None, base, new, autojunk=False).get_matching_blocks()
    runs: list[tuple[int, int, int, int]] = []
    i = j = 0
    while i < len(with_mine) and j < len(with_new):
        m_base, m_mine, m_len = with_mine[i]
        n_base, n_new, n_len = with_new[j]
        start, end = max(m_base, n_base), min(m_base + m_len, n_base + n_len)
        if start < end:
            runs.append((start, end, m_mine + start - m_base, n_new + start - n_base))
        if m_base + m_len < n_base + n_len:
            i += 1
        else:
            j += 1
    runs.append((len(base), len(base), len(mine), len(new)))
    return runs


def merge_text(base: bytes, mine: bytes, new: bytes) -> bytes | None:
    """merge() for the bytes of three versions of a file. None if the changes
    collide or the file is not text. Line endings do not count as a change
    (an editor on Windows may rewrite every one), and the result has the new
    version's."""
    try:
        texts = [b.decode("utf-8") for b in (base, mine, new)]
    except UnicodeDecodeError:
        return None
    merged = merge(*(t.splitlines() for t in texts))
    if merged is None:
        return None
    ending = "\r\n" if "\r\n" in texts[2] else "\n"
    last = ending if merged and any(t.endswith(("\n", "\r")) for t in texts[1:]) else ""
    return (ending.join(merged) + last).encode("utf-8")


def same_text(a: bytes, b: bytes) -> bool:
    """Are two files the same, line endings aside?"""
    if a == b:
        return True
    try:
        return a.decode("utf-8").splitlines() == b.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
