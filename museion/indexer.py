"""Building the local index over the library and any extra content folders.

One loop, shared by the command line and the desktop app, reporting progress
through a callback so a person can see which of 155 documents it is on rather
than staring at a spinner for several minutes.

Three kinds of thing are indexed:
  * PDF-collection archives (the zimgit bundles), read through kiwix-serve;
  * archives with a working index of their own, which need nothing built;
  * loose PDFs in a folder someone added, read straight from disk. The
    catalogue is a starting point, not a boundary -- a manual a person has
    found belongs in the same index as everything else.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

from museion.index import DUPLICATE, IMAGE_ONLY, INDEXED, Index, IndexStats
from museion.kiwix import FULL_TEXT_INDEXED, Kiwix, book_id_from_filename
from museion.pdf import PdfExtractor

# What happened to one document: INDEXED, DUPLICATE or IMAGE_ONLY (as
# Index.add_document reports them), or FAILED when it could not be read.
FAILED = "failed"

# What reading one document can fail with: fetching or reading the file
# (OSError), a PDF pdfium cannot parse (ValueError), or the database
# (sqlite3.Error). One bad document is counted and skipped; anything else is a
# bug and stops the run, so it gets seen.
DOCUMENT_ERRORS = (OSError, ValueError, sqlite3.Error)


# Progress reports, one class per kind of event.

@dataclass
class Archive:
    """Starting on an archive that needs a local index built."""
    book: str
    documents: int


@dataclass
class Native:
    """An archive whose own index works; nothing to build."""
    book: str


@dataclass
class Skipped:
    """Already indexed; left alone."""
    book: str


@dataclass
class Document:
    """One document handled. `done` counts within the current archive."""
    book: str
    title: str
    done: int
    total: int
    outcome: str


@dataclass
class ArchiveDone:
    """Finished an archive."""
    book: str
    indexed: int = 0
    duplicate: int = 0
    image_only: int = 0
    failed: int = 0

    def tally(self, outcome: str) -> None:
        if outcome == INDEXED:
            self.indexed += 1
        elif outcome == DUPLICATE:
            self.duplicate += 1
        elif outcome == IMAGE_ONLY:
            self.image_only += 1
        else:
            self.failed += 1


Progress = Archive | Native | Skipped | Document | ArchiveDone


def folder_book_id(dir: PurePath | str) -> str:
    """The book id under which a folder's loose PDFs are indexed. Stable
    across runs, distinct from any archive, and recognisable in a citation."""
    path = dir if isinstance(dir, PurePath) else PurePath(dir)
    name = path.name
    # A path ending in ".." or a bare drive or root has no name of its own.
    if name in ("", ".."):
        name = str(dir)
    return f"folder:{name}"


def index_library(
    library: Path | str,
    extra_dirs: list[Path | str],
    db: Path | str,
    kiwix_url: str,
    pdfium_dir: Path | str,
    force: bool = False,
    report: Callable[[Progress], None] = lambda progress: None,
) -> IndexStats:
    """Index every archive and loose PDF in the library and extra folders.

    `extra_dirs` are further folders of content: .zim archives (served by
    kiwix-serve, so they must be among the files it was started with) and
    loose PDFs."""
    kiwix = Kiwix(kiwix_url)
    if not kiwix.is_up():
        raise ConnectionError(f"no kiwix-serve at {kiwix_url}. Start it over the library first.")
    extractor = PdfExtractor(Path(pdfium_dir))
    index = Index(db)
    try:
        dirs = [library, *extra_dirs]
        found_any = False
        for dir in dirs:
            try:
                entries = sorted(os.listdir(dir))
            except OSError as e:
                print(f"  (skipping {dir}: {e})", file=sys.stderr)
                continue

            # Archives.
            for filename in entries:
                if not filename.endswith(".zim"):
                    continue
                found_any = True
                book = book_id_from_filename(filename)
                if index.has_book(book) and not force:
                    report(Skipped(book))
                    continue
                kind = kiwix.classify(book)
                if kind.kind == FULL_TEXT_INDEXED:
                    report(Native(book))
                    continue
                if force:
                    index.forget_book(book)
                total = len(kind.docs)
                report(Archive(book, total))
                done = ArchiveDone(book)
                for i, d in enumerate(kind.docs):
                    title = d.title or d.file
                    try:
                        data = kiwix.content(book, f"files/{d.file}")
                        added = index.add_document(
                            book, d.file, title, d.description, d.author, extractor.extract(data)
                        )
                        outcome = added.kind
                    except DOCUMENT_ERRORS:
                        outcome = FAILED
                    done.tally(outcome)
                    report(Document(book, title, i + 1, total, outcome))
                index.mark_book_done(book)
                report(done)

            # Loose PDFs: only in extra folders. The library folder holds
            # archives and the index, and a stray PDF there is not content.
            if Path(dir) == Path(library):
                continue
            pdfs = [n for n in entries if n.lower().endswith(".pdf")]
            if not pdfs:
                continue
            found_any = True
            book = folder_book_id(dir)
            if index.has_book(book) and not force:
                report(Skipped(book))
                continue
            if force:
                index.forget_book(book)
            total = len(pdfs)
            report(Archive(book, total))
            done = ArchiveDone(book)
            for i, name in enumerate(pdfs):
                # Joined as text rather than with Path, which would tidy
                # "./found" into "found": the Rust version stores the folder
                # exactly as it was given, and so does this.
                path = os.path.join(os.fspath(dir), name)
                title = title_from_filename(name)
                try:
                    data = Path(path).read_bytes()
                    # `file` is the full path: it is what opens the document later.
                    added = index.add_document(book, path, title, "", "", extractor.extract(data))
                    outcome = added.kind
                except DOCUMENT_ERRORS:
                    outcome = FAILED
                done.tally(outcome)
                report(Document(book, title, i + 1, total, outcome))
            index.mark_book_done(book)
            report(done)

        if not found_any:
            raise FileNotFoundError(
                f"no .zim archives or PDFs in {', '.join(os.fspath(d) for d in dirs)}"
            )
        return index.stats()
    finally:
        index.close()


def title_from_filename(name: str) -> str:
    """Turns "Where_There_Is_No_Doctor-2015.pdf" into "Where There Is No
    Doctor 2015". A found document has no database.js to name it; the
    filename is all there is."""
    stem = name.rsplit(".", 1)[0]
    return " ".join(stem.replace("_", " ").replace("-", " ").split())
