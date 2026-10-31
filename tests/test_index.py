"""The local full-text index. Nothing here needs a helper program: documents
are built in memory, and tests/fixtures/rust-index.db stands in for an index
made by the Rust version (see tests/index_corpus.py)."""

import json
import math
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import index_corpus
from museion.index import (
    DUPLICATE,
    IMAGE_ONLY,
    INDEXED,
    Index,
    content_terms,
    is_navigation_page,
    to_fts_query,
)
from museion.pdf import Extracted, Page


FILLER = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
          "eiusmod tempor incididunt ut labore et dolore magna aliqua")


def doc(pages: list[str]) -> Extracted:
    """Build a document whose pages are long enough to read as real text.
    Padding matters: a page averaging under 20 words is treated as an image
    scan, so unpadded fixtures would exercise the wrong branch entirely."""
    built = [Page(number=i + 1, text=f"{t} {FILLER}") for i, t in enumerate(pages)]
    return Extracted(pages=built, page_count=max(len(built), 1),
                     word_count=sum(len(p.text.split()) for p in built))


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.ix = Index(self.path)

    def tearDown(self):
        self.ix.close()
        self.tmp.cleanup()

    def add_unrelated(self):
        """Give the index a page without the words being searched for. In an
        index where every page holds a term, that term says nothing about any
        page: its weight is zero, and the topic gate refuses the question."""
        self.ix.add_document("rope", "r.pdf", "Knots", "", "", doc(["tie a bowline in the rope"]))

    def test_an_index_that_cannot_be_written_is_still_read(self):
        # A locked SD card, or a Windows drive on a Mac: opening it the usual
        # way fails, since even reading a WAL database wants a file beside it.
        self.add_unrelated()
        self.ix.add_document("water", "w.pdf", "Water", "", "", doc(["boil the water for one minute"]))
        self.ix.close()
        real = sqlite3.connect

        def refuses_writing(target, *args, **kwargs):
            if not kwargs.get("uri"):
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return real(target, *args, **kwargs)
        with mock.patch("museion.index.sqlite3.connect", refuses_writing):
            self.ix = Index(self.path)
        hits, _ = self.ix.search_ungated("boil water", 5)
        self.assertEqual([h.doc_title for h in hits], ["Water"])

    def test_a_disk_that_cannot_flush_still_gets_an_index(self):
        # A Seagate USB disk failed every fsync, and so every SQLite write
        # with "disk I/O error". The index is rebuildable, so it goes
        # unsynchronised there rather than not at all.
        self.assertEqual(self.ix.conn.execute("PRAGMA synchronous").fetchone()[0], 1)  # NORMAL
        folder = Path(self.tmp.name) / "usb"
        folder.mkdir()
        with mock.patch("os.fsync", side_effect=OSError(9, "Bad file descriptor")):
            ix = Index(folder / "t.db")
        try:
            self.assertEqual(ix.conn.execute("PRAGMA synchronous").fetchone()[0], 0)  # OFF
            ix.add_document("rope", "r.pdf", "Knots", "", "", doc(["tie a bowline in the rope"]))
            self.assertEqual(ix.stats().documents, 1)
        finally:
            ix.close()
        self.assertEqual(list(folder.glob(".museion-*")), [])

    def test_image_only_documents_are_not_duplicates_of_each_other(self):
        """An image-only scan extracts to no pages, so every such document
        hashes identically. Treating that as a duplicate marks unrelated scans
        as copies of one another and hides the ones that need OCR."""
        scan_a = Extracted(pages=[], page_count=40, word_count=0)
        scan_b = Extracted(pages=[], page_count=12, word_count=0)
        self.assertEqual(self.ix.add_document("b", "a.pdf", "Foraging Guide", "", "", scan_a).kind, IMAGE_ONLY)
        self.assertEqual(self.ix.add_document("b", "b.pdf", "More Jerky", "", "", scan_b).kind, IMAGE_ONLY)
        s = self.ix.stats()
        self.assertEqual(s.documents, 2, "both scans should be recorded")
        self.assertEqual(s.image_only, 2, "both should be reported as needing OCR")

    def test_identical_text_is_deduplicated(self):
        """Genuine duplicates do still collapse: the Go Bag really does ship
        "Where There Is No Doctor" in two archives at identical length."""
        body = doc(["boil the water for one full minute to make it safe"])
        first = self.ix.add_document("medicine", "x.pdf", "Where There Is No Doctor", "", "", body)
        self.assertEqual(first.kind, INDEXED)
        again = self.ix.add_document("post-disaster", "y.pdf", "Where-there-is-no-Doctor", "", "", body)
        self.assertEqual((again.kind, again.duplicate_of), (DUPLICATE, "Where There Is No Doctor"))

    def test_finds_a_page_by_its_words(self):
        self.ix.add_document("water", "w.pdf", "Water Treatment", "", "WHO",
                             doc(["unrelated front matter", "add household bleach to disinfect the water"]))
        hits = self.ix.search("how do I disinfect water with bleach?", 5)
        self.assertTrue(hits, "expected a hit")
        self.assertEqual(hits[0].page, 2, "should cite the page the text is actually on")

    def test_refuses_questions_the_library_does_not_cover(self):
        """The grounding guarantee: a question whose distinctive term the
        library has never seen must return nothing, even when its common
        words match plenty of passages. Answering anyway is the failure mode
        that makes an offline reference tool dangerous."""
        self.ix.add_document("post-disaster", "a.pdf", "Survival Manual", "", "", doc([
            "blast overpressure can collapse lungs and rupture eardrums causing injury",
            "economic collapse and ice ages are among the scenarios people prepare for",
        ]))
        # "collapse" and "age" both occur; "bronze" never does.
        hits = self.ix.search("what caused the Bronze Age collapse", 5)
        self.assertEqual([(h.doc_title, h.coverage) for h in hits], [], "expected no answer")
        # The same corpus must still answer what it genuinely covers.
        self.assertTrue(self.ix.search("what does blast overpressure do to lungs", 5),
                        "should still answer in-corpus questions")

        # Ungated, the same question does find passages, and says how little
        # of the question the best of them covers, for a reranker to judge.
        hits, best = self.ix.search_ungated("what caused the Bronze Age collapse", 5)
        self.assertTrue(hits)
        self.assertLess(best, Index.MIN_TERM_COVERAGE)

    def test_recognises_a_contents_page_but_spares_a_real_table(self):
        # Shape of the measured contents page: entries followed by page numbers.
        toc = ("Canning Foods 47 ACID FOODS 4, 7 ALTITUDE TABLES 8, 16 ALUM 27 ANSWERS TO "
               "CANNING QUESTIONS Fruits and Tomatoes 7 Jams Jellies Preserves 44 Pickles 21 "
               "Vegetables 18 Meat 32 Poultry 35 Seafood 38 Storage 41 Altitude 8")
        self.assertTrue(is_navigation_page(toc))
        # A dosage table is number-heavy and must survive: it is often the answer.
        table = ("Give 250 mg of amoxicillin three times a day for children weighing under "
                 "10 kg, and 500 mg three times a day for adults, continuing the course for "
                 "7 days even after the fever has gone, because stopping early allows the "
                 "infection to return in a more resistant form")
        self.assertFalse(is_navigation_page(table))
        # Short passages are left alone; there is not enough signal to judge.
        self.assertFalse(is_navigation_page("Water 1 2 3"))

    def test_a_contents_page_sorts_below_the_pages_it_lists(self):
        self.ix.add_document("canning", "c.pdf", "Canning", "", "", Extracted(
            pages=[
                Page(1, "Contents Beans 4 Corn 9 Peas 12 Jars 15 Lids 18 Altitude 21 Pressure 24 "
                        "Botulism 27 Beans 30 Tomatoes 33 Jam 36 Pickles 39"),
                Page(2, f"Process jars of beans at ten pounds of pressure for twenty minutes {FILLER}"),
            ],
            page_count=2, word_count=60))
        hits, _ = self.ix.search_ungated("beans botulism pressure", 5)
        # BM25 alone ranks the contents page first: it names every term.
        self.assertEqual([h.page for h in hits], [2, 1])

    def test_an_all_duplicate_archive_still_counts_as_done(self):
        """An archive whose documents were all duplicates owns no docs rows,
        and used to look unprocessed forever -- re-downloaded and re-extracted
        on every run. Completion is recorded separately from content."""
        body = doc(["boil the water for one full minute before drinking it"])
        self.ix.add_document("post-disaster", "a.pdf", "Water", "", "", body)
        # Same text under another archive: recorded as a duplicate, no docs row.
        self.ix.add_document("water", "b.pdf", "Water", "", "", body)
        self.assertFalse(self.ix.has_book("water"), "no rows, so not done yet")
        self.ix.mark_book_done("water")
        self.assertTrue(self.ix.has_book("water"))
        self.assertEqual(self.ix.processed_books(), ["post-disaster", "water"])
        self.assertEqual(self.ix.books(), [("post-disaster", 1)])
        self.ix.forget_book("water")
        self.assertFalse(self.ix.has_book("water"), "forget must clear the completion mark too")

    def test_hits_carry_the_document_and_the_whole_page(self):
        self.ix.add_document("water", "files/w.pdf", "Water Treatment", "", "WHO",
                             doc(["add household bleach to disinfect the water"]))
        self.add_unrelated()
        (hit,) = self.ix.search("disinfect with bleach", 5)
        self.assertEqual((hit.doc_title, hit.author, hit.book, hit.file, hit.page),
                         ("Water Treatment", "WHO", "water", "files/w.pdf", 1))
        self.assertEqual(hit.body, f"add household bleach to disinfect the water {FILLER}")
        self.assertIn("bleach", hit.snippet)
        self.assertLess(hit.rank, 0, "bm25 scores are negative, best first")
        self.assertEqual(hit.coverage, 1.0)

    def test_limit_caps_the_hits(self):
        self.ix.add_document("w", "w.pdf", "Water", "", "", doc([f"water page {n}" for n in range(8)]))
        self.add_unrelated()
        self.assertEqual(len(self.ix.search("water", 3)), 3)
        hits, _ = self.ix.search_ungated("water", 5)
        self.assertEqual(len(hits), 5)

    def test_forgetting_a_book_removes_its_pages_from_search(self):
        self.ix.add_document("water", "w.pdf", "Water", "", "", doc(["boil the water first"]))
        self.add_unrelated()
        self.ix.forget_book("water")
        self.assertEqual(self.ix.search("boil water", 5), [])
        self.assertEqual(self.ix.books(), [("rope", 1)])
        self.assertEqual(self.ix.stats().documents, 1)

    def test_the_index_survives_reopening(self):
        self.ix.add_document("water", "w.pdf", "Water", "", "", doc(["boil the water first"]))
        self.add_unrelated()
        self.ix.close()
        self.ix = Index(self.path)
        self.assertEqual(self.ix.search("boil water", 5)[0].doc_title, "Water")


class QueryTermsTest(unittest.TestCase):
    def test_drops_stopwords_and_quotes_terms(self):
        self.assertEqual(to_fts_query("How do I purify water?"), '"purify" OR "water"')

    def test_survives_punctuation_that_fts5_would_choke_on(self):
        # Bare "NOT"/quotes/parens are FTS5 syntax; unquoted they are an error.
        q = to_fts_query("wound that won't close (deep) NOT healing")
        self.assertIn('"wound"', q)
        self.assertNotIn("(", q)
        self.assertTrue('"not"' in q or "NOT " not in q)

    def test_empty_for_a_question_of_pure_stopwords(self):
        self.assertEqual(to_fts_query("what is it?"), "")

    def test_splits_words_as_the_rust_version_does(self):
        # Underscores separate words; "can" is a topic, not a stopword.
        self.assertEqual(content_terms("can_beans, CAN beans"), ["can", "beans"])
        # Letters outside ASCII are part of words, and "longer than two" is
        # counted in UTF-8 bytes: "fé" is two letters but three bytes.
        self.assertEqual(content_terms("ab fé café"), ["fé", "café"])
        # Vowel signs are part of their word, not separators.
        self.assertEqual(content_terms("पानी कैसे साफ करें"), ["पानी", "कैसे", "साफ", "करें"])


class RustIndexTest(unittest.TestCase):
    """An index built by the Rust version opens and searches the same here."""

    @classmethod
    def setUpClass(cls):
        # A copy, because opening a database in WAL mode writes beside it.
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "rust-index.db"
        shutil.copyfile(index_corpus.RUST_DB, cls.path)
        cls.ix = Index(cls.path)
        cls.expected = json.loads(index_corpus.RUST_SEARCH.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.ix.close()
        cls.tmp.cleanup()

    def test_the_schema_is_the_same(self):
        def schema(path):
            db = sqlite3.connect(path)
            try:
                rows = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall()
            finally:
                db.close()
            # SQLite keeps each CREATE statement as it was written, so the
            # two differ in indentation; the definitions must not differ.
            return [(t, n, tbl, " ".join(sql.split()) if sql else sql) for t, n, tbl, sql in rows]

        fresh = Path(self.tmp.name) / "fresh.db"
        Index(fresh).close()
        self.assertEqual(schema(fresh), schema(self.path))

    def test_the_full_text_index_is_intact(self):
        # FTS5 checks its own index structures against the table's contents.
        self.ix.conn.execute("INSERT INTO pages(pages) VALUES ('integrity-check')")

    def test_search_returns_what_the_rust_version_returned(self):
        for want in self.expected["results"]:
            query, limit = want["query"], want["limit"]
            with self.subTest(query=query):
                hits, best = self.ix.search_ungated(query, limit)
                self.assertTrue(math.isclose(best, want["best"], rel_tol=1e-9, abs_tol=1e-12))
                self.assertEqual(len(self.ix.search(query, limit)), want["gated"])
                self.assertEqual(len(hits), len(want["hits"]))
                for got, w in zip(hits, want["hits"]):
                    self.assertEqual(
                        (got.doc_title, got.author, got.book, got.file, got.page, got.snippet, got.body),
                        (w["doc_title"], w["author"], w["book"], w["file"], w["page"], w["snippet"], w["body"]),
                    )
                    self.assertTrue(math.isclose(got.rank, w["rank"], rel_tol=1e-9))
                    self.assertTrue(math.isclose(got.coverage, w["coverage"], rel_tol=1e-9, abs_tol=1e-12))

    def test_counts_are_the_same(self):
        s = self.ix.stats()
        self.assertEqual(
            {"documents": s.documents, "pages": s.pages, "words": s.words, "image_only": s.image_only},
            self.expected["stats"],
        )
        self.assertEqual([list(b) for b in self.ix.books()], self.expected["books"])
        self.assertEqual(self.ix.processed_books(), self.expected["processed_books"])


if __name__ == "__main__":
    unittest.main()
