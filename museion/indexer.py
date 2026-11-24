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
import threading
import time
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

# One build at a time in this program: a second one (Build index pressed
# while a fetched archive is being indexed) waits for the first, then finds
# its work done. Two at once would share pdfium and the index.
_BUILDING = threading.Lock()

# How long a build waits for kiwix-serve to come back when it stops answering
# mid-archive: adding a folder or fetching an archive restarts it.
KIWIX_RETURN_SECONDS = 30.0


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
    with _BUILDING:
        return _index_library(library, extra_dirs, db, kiwix_url, pdfium_dir, force, report)


def _index_library(library, extra_dirs, db, kiwix_url, pdfium_dir, force, report) -> IndexStats:
    kiwix = Kiwix(kiwix_url)
    if not kiwix.is_up():
        raise ConnectionError(f"no kiwix-serve at {kiwix_url}. Start it over the library first.")
    extractor = PdfExtractor(Path(pdfium_dir))
    index = Index(db)
    try:
        dirs = [library, *extra_dirs]
        listings = []
        for dir in dirs:
            try:
                listings.append((dir, sorted(os.listdir(dir))))
            except OSError as e:
                print(f"  (skipping {dir}: {e})", file=sys.stderr)
        if len(listings) == len(dirs):
            forget_departed(index, library, listings)
        found_any = False
        for dir, entries in listings:
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
                # Afresh, and marked unfinished until the last document: a
                # run that stops partway is redone next time, not skipped.
                index.start_book(book)
                total = len(kind.docs)
                report(Archive(book, total))
                done = ArchiveDone(book)
                for i, d in enumerate(kind.docs):
                    title = d.title or d.file
                    try:
                        data = fetch_document(kiwix, book, d.file)
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
            # A folder is redone when its PDFs change: one dropped in later
            # was otherwise never indexed. An index from before this kept no
            # list, and takes the folder as it is now.
            files = "\n".join(pdfs)
            if index.has_book(book) and not force:
                if index.folder_files(book) in (None, files):
                    index.set_folder_files(book, files)
                    report(Skipped(book))
                    continue
            index.start_book(book)
            total = len(pdfs)
            report(Archive(book, total))
            done = ArchiveDone(book)
            for i, name in enumerate(pdfs):
                # Joined as text rather than with Path, which would tidy
                # "./found" into "found": the folder is stored exactly as
                # it was given.
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
            index.set_folder_files(book, files)
            report(done)

        if not found_any:
            raise FileNotFoundError(
                f"no .zim archives or PDFs in {', '.join(os.fspath(d) for d in dirs)}"
            )
        return index.stats()
    finally:
        index.close()


def forget_departed(index: Index, library, listings) -> None:
    """Drop what the index holds for archives and folders that are gone. An
    archive replaced by a newer release otherwise stayed, every document of
    the new one was a duplicate of it, and citations pointed at a file
    kiwix-serve no longer had. Only called when every folder could be read:
    a drive that is merely unplugged does not lose its index."""
    present = set()
    for dir, entries in listings:
        present |= {book_id_from_filename(n) for n in entries if n.endswith(".zim")}
        if Path(dir) != Path(library) and any(n.lower().endswith(".pdf") for n in entries):
            present.add(folder_book_id(dir))
    for book in index.processed_books():
        if book not in present:
            index.forget_book(book)


def fetch_document(kiwix: Kiwix, book: str, file: str) -> bytes:
    """One PDF from an archive. If kiwix-serve has stopped answering -- it is
    restarted when a folder is added or an archive fetched -- wait for it and
    ask again; if it does not come back, stop the build, since every document
    after this one would fail too and the archive would pass for done. A
    document that fails a second time, with the server answering, is that
    document's fault."""
    try:
        return kiwix.content(book, f"files/{file}")
    except ConnectionError:
        pass
    deadline = time.monotonic() + KIWIX_RETURN_SECONDS
    while not kiwix.is_up():
        if time.monotonic() > deadline:
            raise RuntimeError(f"the content server stopped while {book} was being indexed. "
                               "Start services and build the index again to finish it.")
        time.sleep(1.0)
    return kiwix.content(book, f"files/{file}")


def title_from_filename(name: str) -> str:
    """Turns "Where_There_Is_No_Doctor-2015.pdf" into "Where There Is No
    Doctor 2015". A found document has no database.js to name it; the
    filename is all there is."""
    stem = name.rsplit(".", 1)[0]
    return " ".join(stem.replace("_", " ").replace("-", " ").split())
