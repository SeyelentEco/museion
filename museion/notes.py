"""Your own notes and records: plain files in the library's notes folder.

Notes are Markdown or text (the pump fix, which beans grew, what a remedy
did); records are CSV tables (stores, medicines, a health log, a maintenance
log, places, people). A person writes them in any text editor or in the Notes
tab, and an agent writes them when asked, after an Allow / Deny card. Every
change keeps the previous version in notes/.history/, so nothing is lost.

They are searched with the library and cited like it, but always marked as
the person's own words: an answer built on them alone says "From your own
notes", never "From your library".

The notes have an index of their own, notes/.index.db, rebuilt whenever a
file in the folder has changed since it was last built. Notes are small and
change often; the library's index is large and built once, and keeping them
apart means rebuilding one never touches the other, and a test's notes never
reach the person's index.
"""

from __future__ import annotations

import csv
import io
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from museion.index import Hit, Index, content_terms, coverage
from museion.pdf import Extracted, Page

# The book id notes are indexed under, and so how a citation of one is told
# apart from a library passage.
NOTES_BOOK = "notes"
# The folder in the library that holds them.
FOLDER = "notes"
INDEX_NAME = ".index.db"
HISTORY = ".history"
# What a note can be: text a person can open anywhere.
SUFFIXES = (".md", ".txt", ".csv")
MAX_BYTES = 500_000
# A section of a note longer than this is split at a paragraph.
SECTION_CHARS = 1800
# Rows of a record per indexed part.
ROWS_PER_PART = 25

_lock = threading.Lock()


class NoteError(ValueError):
    """A problem with a note's name or contents, worded for the person."""


@dataclass
class NoteFile:
    name: str  # relative to the notes folder, with forward slashes
    size: int
    modified: float  # seconds since the epoch


def listing(folder: Path | None) -> list[NoteFile]:
    """Every note and record, by name. Dot-named files and folders (the
    index, .history) are Museion's, not notes."""
    if folder is None or not folder.is_dir():
        return []
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if f.startswith(".") or not f.lower().endswith(SUFFIXES):
                continue
            path = Path(root) / f
            try:
                st = path.stat()
            except OSError:
                continue
            out.append(NoteFile(path.relative_to(folder).as_posix(), st.st_size, st.st_mtime))
    return out


def path_of(folder: Path, name: str) -> Path:
    """The note `name` in the folder, refusing anything outside it, any
    dot-named part, and any kind of file other than .md, .txt and .csv."""
    clean = (name or "").strip().replace("\\", "/")
    if not clean or clean.startswith("/") or ":" in clean:
        raise NoteError(f"{name!r} is not a note's name. Use a plain name like stores.csv or pump-repair.md.")
    parts = [p for p in clean.split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") for p in parts):
        raise NoteError(f"{name}: a note's name may not start with a dot or go up a folder.")
    if Path(parts[-1]).suffix.lower() not in SUFFIXES:
        raise NoteError(f"{name}: notes are .md, .txt or .csv files.")
    root = folder.resolve()
    path = root.joinpath(*parts).resolve()
    if root not in path.parents:
        raise NoteError(f"{name} is outside the notes folder.")
    return path


def read(folder: Path, name: str) -> str:
    path = path_of(folder, name)
    if not path.is_file():
        raise NoteError(f"There is no note called {name} yet.")
    return path.read_text(encoding="utf-8-sig", errors="replace")


def write(folder: Path, name: str, text: str, append: bool = False) -> Path:
    """Write a note, or add to its end; the version before goes to
    .history first. Returns the file written."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise NoteError(f"That is too long for one note; keep notes under {MAX_BYTES // 1000} kB.")
    path = path_of(folder, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    keep_previous(folder, path)
    if not text.endswith("\n"):
        text += "\n"
    if append and path.is_file():
        old = path.read_text(encoding="utf-8-sig", errors="replace")
        if old and not old.endswith("\n"):
            text = "\n" + text
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")
    return path


def keep_previous(folder: Path, path: Path) -> None:
    if not path.is_file():
        return
    history = folder / HISTORY
    history.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rel = path.resolve().relative_to(folder.resolve()).as_posix().replace("/", "__")
    target, n = history / f"{stamp}-{rel}", 2
    while target.exists():
        target, n = history / f"{stamp}-{n}-{rel}", n + 1
    target.write_bytes(path.read_bytes())


# ── searching ────────────────────────────────────────────────────────────────

def search(folder: Path | None, query: str, limit: int, weigh: Index | None = None) -> tuple[list[Hit], float]:
    """Search the notes: the hits and the best term coverage, as
    Index.search_ungated gives them. Nothing when there are no notes.

    Coverage is weighed by the library's index (`weigh`), not the notes'
    own: in a folder of three notes every word is in a third of them, and
    "rice" would weigh no more than "much". The library knows which words
    carry a question. Without it every term weighs the same."""
    if folder is None or not listing(folder):
        return [], 0.0
    with _lock:
        index = refreshed(folder)
        try:
            hits, _ = index.search_ungated(query, limit)
        finally:
            index.close()
    terms = content_terms(query)
    weights = weigh._term_weights(terms) if weigh is not None else [1.0] * len(terms)
    if weights and not any(weights):
        weights = [1.0] * len(terms)
    for h in hits:
        h.coverage = coverage(f"{h.doc_title} {h.body}", terms, weights)
    return hits, max((h.coverage for h in hits), default=0.0)


def refreshed(folder: Path) -> Index:
    """The notes index, rebuilt first if any note was added, changed or
    removed since it was built."""
    files = listing(folder)
    signature = "\n".join(f"{f.name}\t{f.size}\t{f.modified:.3f}" for f in files)
    index = Index(folder / INDEX_NAME)
    if index.has_book(NOTES_BOOK) and index.folder_files(NOTES_BOOK) == signature:
        return index
    index.start_book(NOTES_BOOK)
    for f in files:
        try:
            text = (folder / f.name).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        extracted = parts(f.name, text)
        if extracted.pages:
            index.add_document(NOTES_BOOK, f.name, f.name, "", "", extracted)
    index.mark_book_done(NOTES_BOOK)
    index.set_folder_files(NOTES_BOOK, signature)
    return index


class NoteText(Extracted):
    """A note's text. Never "image only": that test is for scanned PDFs, and
    a two-line note is short, not a scan."""

    def is_image_only(self) -> bool:
        return False


def parts(name: str, text: str) -> Extracted:
    """A note cut into the parts a citation can point at: a Markdown note by
    its headings, a record by groups of rows, each row written out with its
    column names so a search for "rice" finds "item: rice; quantity: 10"."""
    if name.lower().endswith(".csv"):
        chunks = record_parts(name, text)
    else:
        chunks = text_parts(text)
    out = NoteText()
    for i, chunk in enumerate(chunks, start=1):
        out.pages.append(Page(number=i, text=chunk))
        out.word_count += len(chunk.split())
    out.page_count = len(out.pages)
    return out


def text_parts(text: str) -> list[str]:
    sections: list[str] = []
    current: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if re.match(r"^\s{0,3}#{1,6}\s", line) and any(s.strip() for s in current):
            sections.append("\n".join(current).strip())
            current = []
        current.append(line)
    if any(s.strip() for s in current):
        sections.append("\n".join(current).strip())
    out = []
    for s in sections:
        while len(s) > SECTION_CHARS:
            cut = s.rfind("\n\n", 0, SECTION_CHARS)
            if cut < SECTION_CHARS // 3:
                cut = s.rfind(" ", 0, SECTION_CHARS)
            if cut <= 0:
                cut = SECTION_CHARS
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)
    return out


def rows_of(text: str) -> tuple[list[str], list[list[str]]]:
    """A record's header and rows. Blank lines are skipped."""
    rows = [r for r in csv.reader(io.StringIO(text.replace("\r\n", "\n"))) if any(c.strip() for c in r)]
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], rows[1:]


def record_parts(name: str, text: str) -> list[str]:
    header, rows = rows_of(text)
    if not header:
        return []
    if not rows:
        return [f"{name}: a record with the columns {', '.join(header)}, and no rows yet."]
    out = []
    for start in range(0, len(rows), ROWS_PER_PART):
        group = rows[start:start + ROWS_PER_PART]
        lines = [f"{name}, rows {start + 1}-{start + len(group)} of {len(rows)}:"]
        for row in group:
            cells = [f"{h}: {v.strip()}" for h, v in zip(header, row) if v.strip()]
            lines.append("; ".join(cells))
        out.append("\n".join(lines))
    return out


# Files agents kept in their own folders before they kept them in the notes:
# (agent, file). Copied across once, so a list kept before is not lost.
ADOPTED = [("stores-keeper", "stores.csv"), ("garden-planner", "garden-plan.md")]


def adopt_old_files(work: Path, folder: Path) -> list[str]:
    """Copy the files agents kept in agent-work/ before into the notes,
    where they live now, unless the notes already have one. Returns the
    names copied."""
    copied = []
    for agent, name in ADOPTED:
        old = work / agent / name
        try:
            if old.is_file() and not (folder / name).exists():
                folder.mkdir(parents=True, exist_ok=True)
                (folder / name).write_bytes(old.read_bytes())
                copied.append(name)
        except OSError:
            continue
    return copied


# ── records ──────────────────────────────────────────────────────────────────

@dataclass
class RecordKind:
    file: str
    title: str
    about: str
    columns: list[str]


def record_kinds() -> list[RecordKind]:
    """The records Museion knows how to start (records.toml)."""
    import tomllib
    data = tomllib.loads(Path(__file__).with_name("records.toml").read_text(encoding="utf-8"))
    return [RecordKind(r["file"], r["title"], r["about"], list(r["columns"])) for r in data["records"]]


def start_record(folder: Path, file: str) -> Path:
    """Start one of the known records with its header line, unless it is
    already there."""
    kind = next((k for k in record_kinds() if k.file == file), None)
    if kind is None:
        raise NoteError(f"There is no kind of record called {file}.")
    path = path_of(folder, file)
    if not path.exists():
        write(folder, file, ",".join(kind.columns))
    return path


def parse_date(text: str):
    """A YYYY-MM-DD date (the time after it ignored), or None."""
    import datetime
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})", text or "")
    if not m:
        return None
    try:
        return datetime.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


@dataclass
class Due:
    row: dict[str, str]
    due: object  # datetime.date
    days: int  # from today: negative is overdue


def dates_due(text: str, column: str, every: str | None = None, today=None) -> tuple[list[Due], list[dict]]:
    """Rows of a record by how soon their date comes: an expiry date in
    `column`, or, with `every`, the date in `column` (last done) plus that
    many days. Returns the rows with a date, soonest first, and the rows
    without one."""
    import datetime
    today = today or datetime.date.today()
    header, rows = rows_of(text)
    if column not in header:
        raise NoteError(f"The record has no column {column}; its columns are {', '.join(header)}.")
    if every and every not in header:
        raise NoteError(f"The record has no column {every}; its columns are {', '.join(header)}.")
    dated, undated = [], []
    for r in rows:
        row = {h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(header)}
        when = parse_date(row[column])
        if when is not None and every:
            try:
                when = when + datetime.timedelta(days=float(row[every]))
            except ValueError:
                when = None
        if when is None:
            undated.append(row)
        else:
            dated.append(Due(row, when, (when - today).days))
    dated.sort(key=lambda d: d.days)
    return dated, undated
