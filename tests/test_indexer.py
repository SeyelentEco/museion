"""Building the index over a library, with tests/fake_kiwix.py standing in for
kiwix-serve. Most tests stand in for pdfium too; the last one uses the real
pdfium, when MUSEION_PDFIUM names the folder holding it, and checks the rows
it builds are the ones the Rust version built from the same library."""

import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

import index_corpus
from fake_kiwix import FakeKiwix
from museion import sidecar
from museion.index import DUPLICATE, IMAGE_ONLY, INDEXED, Index, IndexStats
from museion.indexer import (
    FAILED,
    Archive,
    ArchiveDone,
    Document,
    Native,
    Skipped,
    folder_book_id,
    index_library,
    title_from_filename,
)
from museion.pdf import Extracted, Page

PDFIUM = os.environ.get("MUSEION_PDFIUM")
ARCHIVE = index_corpus.ARCHIVE
FOLDER = "folder:found"

# Everything the test library produces, in order, on a first run.
FIRST_RUN = [
    Native(index_corpus.NATIVE),
    Archive(ARCHIVE, 3),
    Document(ARCHIVE, "Don't Drink the Water", 1, 3, INDEXED),
    Document(ARCHIVE, "Emergency Preparedness (2).pdf", 2, 3, INDEXED),
    Document(ARCHIVE, "Missing From The Archive", 3, 3, FAILED),
    ArchiveDone(ARCHIVE, indexed=2, failed=1),
    Archive(FOLDER, 6),
    Document(FOLDER, "Broken", 1, 6, FAILED),
    Document(FOLDER, "Field First Aid", 2, 6, INDEXED),
    Document(FOLDER, "Home Canning", 3, 6, INDEXED),
    Document(FOLDER, "Scanned Map", 4, 6, IMAGE_ONLY),
    Document(FOLDER, "Water Storage copy", 5, 6, DUPLICATE),
    Document(FOLDER, "Water Treatment WHO", 6, 6, INDEXED),
    ArchiveDone(FOLDER, indexed=3, duplicate=1, image_only=1, failed=1),
]


class FakeExtractor:
    """Stands in for pdfium. It knows the text of every PDF in the test
    library, and fails on anything else as pdfium does on a broken file."""

    def __init__(self, lib_dir):
        every = [*index_corpus.ARCHIVE_PDFS.values(), *index_corpus.FOLDER_PDFS.values()]
        self.known = {index_corpus.pdf(pages): pages for pages in every}

    def extract(self, data: bytes) -> Extracted:
        if data not in self.known:
            raise ValueError("parsing PDF: not in the format expected")
        pages = self.known[data]
        return Extracted(pages=[Page(i + 1, text) for i, text in enumerate(pages)],
                         page_count=len(pages), word_count=sum(len(t.split()) for t in pages))


class LibraryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.library, self.found = index_corpus.write_library(root)
        self.db = root / "index.db"
        self.kiwix = FakeKiwix()
        self.addCleanup(self.kiwix.close)
        self.kiwix.entries.update(index_corpus.kiwix_entries())

    def index(self, pdfium="unused", **options):
        events = []
        stats = index_library(self.library, [self.found], self.db, self.kiwix.url, pdfium,
                              report=events.append, **options)
        return stats, events


class IndexLibraryTest(LibraryTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("museion.indexer.PdfExtractor", FakeExtractor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_indexes_archives_and_loose_pdfs(self):
        stats, events = self.index()
        self.assertEqual(events, FIRST_RUN)
        # The counts the Rust version reported for the same library.
        self.assertEqual(stats, IndexStats(documents=6, pages=14, words=347, image_only=1))
        ix = Index(self.db)
        try:
            # The archive with its own index needs nothing, and the stray PDF
            # in the library folder is not content.
            self.assertEqual(ix.processed_books(), [FOLDER, ARCHIVE])
            files = [r[0] for r in ix.conn.execute("SELECT file FROM docs WHERE book = ?", (FOLDER,))]
            self.assertIn(os.path.join(str(self.found), "Home_Canning.pdf"), files)
        finally:
            ix.close()
        self.assertIn(f"/content/{ARCHIVE}/files/Emergency%20Preparedness%20%281%29.pdf", self.kiwix.requests)

    def test_a_second_run_leaves_finished_archives_alone(self):
        self.index()
        _, events = self.index()
        self.assertEqual(events, [Native(index_corpus.NATIVE), Skipped(ARCHIVE), Skipped(FOLDER)])

    def test_force_rebuilds_without_doubling_anything(self):
        first, _ = self.index()
        again, events = self.index(force=True)
        self.assertEqual(events, FIRST_RUN)
        self.assertEqual(again, first)

    def test_a_missing_folder_is_skipped(self):
        err = io.StringIO()
        with redirect_stderr(err):
            stats = index_library(self.library, [self.found, self.found.parent / "nowhere"], self.db,
                                  self.kiwix.url, "unused")
        self.assertIn("(skipping", err.getvalue())
        self.assertEqual(stats.documents, 6)

    def test_says_when_there_is_nothing_to_index(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(FileNotFoundError, "no .zim archives or PDFs in"):
            index_library(empty, [empty], self.db, self.kiwix.url, "unused")

    def test_needs_kiwix_serve(self):
        url = f"http://127.0.0.1:{sidecar.free_port()}"
        with self.assertRaisesRegex(ConnectionError, "no kiwix-serve at .*Start it over the library"):
            index_library(self.library, [self.found], self.db, url, "unused")


class NamesTest(unittest.TestCase):
    def test_titles_from_filenames_read_like_titles(self):
        self.assertEqual(title_from_filename("Where_There_Is_No_Doctor-2015.pdf"), "Where There Is No Doctor 2015")
        self.assertEqual(title_from_filename("FM 21-76.PDF"), "FM 21 76")

    def test_folder_ids_are_stable_and_distinct_from_archives(self):
        # Spelled as Windows and POSIX paths explicitly, so this passes on
        # every system; a plain Path would only read one of them correctly.
        self.assertEqual(folder_book_id(PureWindowsPath(r"E:\found\manuals")), "folder:manuals")
        self.assertEqual(folder_book_id(PurePosixPath("/x/manuals")), "folder:manuals")
        self.assertFalse(folder_book_id(PurePosixPath("/x/manuals")).endswith(".zim"))
        self.assertEqual(folder_book_id(os.path.join("found", "manuals")), "folder:manuals")
        self.assertEqual(folder_book_id(PureWindowsPath("E:\\")), "folder:E:\\")


@unittest.skipUnless(PDFIUM, "set MUSEION_PDFIUM to the folder holding the pdfium library")
class SameRowsAsRustTest(LibraryTestCase):
    def test_builds_the_rows_the_rust_version_built(self):
        self.index(pdfium=PDFIUM)
        # A copy, because opening a database in WAL mode writes beside it.
        rust_db = Path(self.tmp.name) / "rust-index.db"
        shutil.copyfile(index_corpus.RUST_DB, rust_db)
        self.assertEqual(rows(self.db), rows(rust_db))


def rows(db_path: Path) -> dict:
    """Everything an index holds but the time each archive was finished.
    Loose PDFs are stored by full path, so only their filenames are compared."""
    db = sqlite3.connect(db_path)
    try:
        docs = [(id, book, Path(file.replace("\\", "/")).name, *rest) for id, book, file, *rest in db.execute(
            "SELECT id, book, file, title, description, author, pages, words, image_only, text_sha256"
            " FROM docs ORDER BY id")]
        pages = db.execute("SELECT title, body, doc_id, page FROM pages ORDER BY rowid").fetchall()
        books = db.execute("SELECT book FROM books ORDER BY book").fetchall()
    finally:
        db.close()
    return {"docs": docs, "pages": pages, "books": books}


if __name__ == "__main__":
    unittest.main()
