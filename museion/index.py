"""The local full-text index.

Built only for archives whose own index is unusable. This is keyword indexing
over a small curated corpus -- minutes for the Go Bag's 4.7 million words --
not the hours of GPU time that embedding every chunk into a vector database
would cost before the tool became usable at all.

The file is an ordinary SQLite database (museion-index.db) with an FTS5 table
for the text. Its schema and queries are exactly those of the Rust version,
so an index built by either one opens and searches the same in the other.
"""

from __future__ import annotations

import functools
import hashlib
import math
import os
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from museion.pdf import Extracted

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY,
    book        TEXT NOT NULL,
    file        TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    pages       INTEGER NOT NULL DEFAULT 0,
    words       INTEGER NOT NULL DEFAULT 0,
    image_only  INTEGER NOT NULL DEFAULT 0,
    text_sha256 TEXT NOT NULL,
    UNIQUE(book, file)
);

-- Content lives here; docs holds the metadata. Page-level rows keep
-- citations precise: an answer can name the page it came from.
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    title,
    body,
    doc_id UNINDEXED,
    page   UNINDEXED,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Archives that have been fully processed, whether or not they
-- contributed any rows to docs. zimgit-water's seven documents are
-- all duplicates of copies in the post-disaster archive, so it owns
-- no docs rows -- and without this table it looked unprocessed and
-- was re-downloaded and re-extracted on every run.
CREATE TABLE IF NOT EXISTS books (
    book       TEXT PRIMARY KEY,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# The FTS5 match, bm25() and snippet() all happen in a subquery with the table
# named directly, then docs is joined on the outside. Both alternatives are
# rejected by SQLite: auxiliary functions cannot see a table alias ("no such
# column: p"), and naming the table unaliased alongside a JOIN is ambiguous
# ("ambiguous column name: pages").
#
# "rank" is a reserved hidden column on FTS5 tables, so the score is aliased
# to "score" instead.
#
# bm25 weights cover all four columns, including the UNINDEXED ones. Title is
# weighted below body deliberately: the title is copied onto every page row of
# a document, so it cannot discriminate between pages of the same document,
# and over-weighting it floods the results with every page of any
# topically-related work.
SEARCH_SQL = """
SELECT d.title, d.author, d.book, d.file, m.page, m.snip, m.score, m.body
FROM (
    SELECT doc_id, page, body,
           snippet(pages, 1, '', '', ' ... ', 24) AS snip,
           bm25(pages, 0.5, 1.0, 0.0, 0.0) AS score
    FROM pages
    WHERE pages MATCH ?
    ORDER BY score
    LIMIT ?
) m
JOIN docs d ON d.id = m.doc_id
ORDER BY m.score
"""


@dataclass
class Hit:
    doc_title: str
    author: str
    book: str
    file: str
    page: int  # 1-based; 0 for an article from a native archive, which has no pages
    snippet: str
    rank: float
    # Fraction of the question's content terms this passage actually contains.
    coverage: float
    # Full text of the page. This is what gets quoted to the model; the
    # snippet is only for display.
    body: str


@dataclass
class IndexStats:
    documents: int = 0
    pages: int = 0
    words: int = 0
    image_only: int = 0
    duplicates: int = 0


# What add_document did with a document.
# The meta key, before an archive's name, of an archive being indexed.
UNFINISHED = "unfinished:"
# The meta key, before a folder's book id, of the PDFs it held when indexed.
FOLDER_FILES = "files:"

INDEXED = "indexed"
IMAGE_ONLY = "image_only"
DUPLICATE = "duplicate"


@dataclass
class AddOutcome:
    kind: str  # INDEXED, IMAGE_ONLY or DUPLICATE
    duplicate_of: str = ""  # for DUPLICATE: the title of the copy already indexed


@functools.lru_cache(maxsize=None)
def read_only(path: Path | str) -> sqlite3.Connection | None:
    """The index on a drive that cannot be written to -- a locked SD card, a
    Windows drive on a Mac -- opened for reading, or None. The index is kept
    in WAL mode, and SQLite cannot even read such a file normally where it
    cannot make the -shm file beside it; "immutable" tells it nothing else
    will write either, so it needs none."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        conn = sqlite3.connect(p.resolve().as_uri() + "?immutable=1", uri=True, isolation_level=None)
        conn.execute("SELECT COUNT(*) FROM docs").fetchone()
        return conn
    except (sqlite3.Error, ValueError):
        return None


def can_flush(folder: Path) -> bool:
    """Whether files in `folder` can be flushed to the disk. Some USB disks
    refuse (Windows' FlushFileBuffers fails on a Seagate Backup Plus), and
    SQLite then fails every write with "disk I/O error" -- preparing a drive
    on one stopped at building the index. Such a folder gets the index with
    synchronous=OFF: a power cut mid-write can corrupt it, but the index is
    built from the archives and can always be built again. A folder that
    cannot be written at all needs no flushing for reading."""
    probe = folder / ".museion-flush-test"
    try:
        with open(probe, "wb") as f:
            f.write(b"x")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                return False
    except OSError:
        return True
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return True


class Index:
    # How much of a question's *information* the best passage in the library
    # must carry before the library is considered to cover the topic at all.
    #
    # Terms are OR-ed in the FTS query, because a question's phrasing rarely
    # matches a document's. The cost is that an OR query always matches
    # something: "what caused the Bronze Age collapse" pulls up pages on blast
    # injuries collapsing lungs, and one that literally reads "Ice ages;
    # Economic collapse". Two terms of three, so a flat ratio lets it through.
    #
    # Coverage is therefore weighted by inverse document frequency. Missing
    # "age" costs almost nothing because nearly every document has it; missing
    # "bronze" costs almost everything, because that is the term carrying the
    # question. A passage that skips the distinctive words is not an answer,
    # however many common ones it happens to contain.
    #
    # For a tool consulted precisely when the answer cannot be checked
    # anywhere else, returning an irrelevant passage confidently is worse than
    # returning none.
    #
    # Crucially this is a threshold on the *best* passage, not on each one.
    # Applying it per passage looked reasonable and quietly broke retrieval:
    # asked how to can green beans safely, it discarded the pages holding the
    # actual packing steps and processing times -- which describe the method
    # without ever using the words "green", "beans" or "botulism" -- and kept
    # the book's index page, which contains all three and teaches nothing.
    # Deciding topic coverage is this gate's job; deciding which passage
    # answers best is the reranker's.
    MIN_TERM_COVERAGE = 0.5

    def __init__(self, path: Path | str) -> None:
        """Open the index at `path`, creating it if it does not exist."""
        try:
            # Autocommit: transactions are begun and committed explicitly
            # below, as the Rust version does, rather than by the sqlite3
            # module guessing where they should go.
            self.conn = sqlite3.connect(str(path), isolation_level=None)
            # Before journal_mode, which is itself a write.
            sync = "NORMAL" if str(path) == ":memory:" or can_flush(Path(path).parent) else "OFF"
            self.conn.execute(f"PRAGMA synchronous = {sync}")
            self.conn.executescript(SCHEMA)
        except sqlite3.Error as e:
            self.conn = read_only(path)
            if self.conn is None:
                raise RuntimeError(f"opening index at {path}: {e}") from e

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def has_book(self, book: str) -> bool:
        """Has this archive already been processed, to the end? One that was
        started and never finished -- Museion closed, the computer slept --
        has some of its documents, and must not pass for done."""
        (n,) = self.conn.execute(
            "SELECT ((SELECT COUNT(*) FROM books WHERE book = ?1)"
            " + (SELECT COUNT(*) FROM docs WHERE book = ?1))"
            " * NOT EXISTS (SELECT 1 FROM meta WHERE key = ?2)",
            (book, UNFINISHED + book),
        ).fetchone()
        return n > 0

    def start_book(self, book: str) -> None:
        """Begin processing an archive afresh: whatever an earlier,
        unfinished run left of it goes, and it counts as unfinished until
        mark_book_done."""
        self.forget_book(book)
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, '1')", (UNFINISHED + book,))

    def processed_books(self) -> list[str]:
        """Archives this index holds content for. Anything else in the
        library is served by kiwix-serve's own index and searched there."""
        rows = self.conn.execute(
            "SELECT book FROM books UNION SELECT DISTINCT book FROM docs ORDER BY 1"
        ).fetchall()
        return [r[0] for r in rows]

    def mark_book_done(self, book: str) -> None:
        """Record that an archive has been fully processed."""
        with self._transaction():
            self.conn.execute("INSERT OR REPLACE INTO books (book) VALUES (?)", (book,))
            self.conn.execute("DELETE FROM meta WHERE key = ?", (UNFINISHED + book,))

    def forget_book(self, book: str) -> None:
        with self._transaction():
            self.conn.execute(
                "DELETE FROM pages WHERE doc_id IN (SELECT id FROM docs WHERE book = ?)", (book,)
            )
            self.conn.execute("DELETE FROM docs WHERE book = ?", (book,))
            self.conn.execute("DELETE FROM books WHERE book = ?", (book,))
            self.conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (UNFINISHED + book, FOLDER_FILES + book))

    def folder_files(self, book: str) -> str | None:
        """The PDFs a folder held when it was indexed, one name a line."""
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (FOLDER_FILES + book,)).fetchone()
        return row[0] if row else None

    def set_folder_files(self, book: str, files: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (FOLDER_FILES + book, files))

    def _duplicate_of(self, sha: str) -> str | None:
        """The title of a document with identical extracted text, if one is
        already indexed.

        The Go Bag genuinely contains duplicates across archives -- "Where
        There Is No Doctor" appears in both the medicine and post-disaster
        bundles at the same 182,240 words -- and indexing both would put the
        same passage twice into every set of retrieval candidates, crowding
        out other sources."""
        row = self.conn.execute(
            "SELECT title FROM docs WHERE text_sha256 = ? LIMIT 1", (sha,)
        ).fetchone()
        return row[0] if row else None

    def add_document(
        self, book: str, file: str, title: str, description: str, author: str, extracted: Extracted
    ) -> AddOutcome:
        image_only = extracted.is_image_only()

        hasher = hashlib.sha256()
        for p in extracted.pages:
            hasher.update(p.text.encode("utf-8"))
        sha = hasher.hexdigest()

        # Deduplicate only documents that actually yielded text. An image-only
        # scan extracts to zero pages, so every one of them hashes to the hash
        # of nothing and they all collide -- which reported unrelated documents
        # as copies of each other ("foraging guide" as a duplicate of "More
        # Jerky") and hid the scans that genuinely need OCR.
        if not image_only:
            existing = self._duplicate_of(sha)
            if existing is not None:
                return AddOutcome(DUPLICATE, existing)

        with self._transaction():
            cur = self.conn.execute(
                "INSERT OR REPLACE INTO docs"
                " (book, file, title, description, author, pages, words, image_only, text_sha256)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (book, file, title, description, author,
                 extracted.page_count, extracted.word_count, int(image_only), sha),
            )
            doc_id = cur.lastrowid
            if not image_only:
                self.conn.executemany(
                    "INSERT INTO pages (title, body, doc_id, page) VALUES (?,?,?,?)",
                    [(title, p.text, doc_id, p.number) for p in extracted.pages],
                )
        return AddOutcome(IMAGE_ONLY if image_only else INDEXED)

    def search(self, query: str, limit: int) -> list[Hit]:
        """Search with the topic gate applied: nothing comes back unless the
        best passage carries enough of the question's information. This is
        the right call when no reranker is available to judge instead."""
        hits, best = self.search_ungated(query, limit)
        if best + 1e-9 < self.MIN_TERM_COVERAGE:
            return []
        return hits

    def search_ungated(self, query: str, limit: int) -> tuple[list[Hit], float]:
        """Search without the topic gate. Returns the hits and the best
        coverage seen, so the caller can decide -- a reranker's score is a
        far better judge of "does this answer the question" than term
        overlap.

        The gate alone was refusing "we have a lot of green beans and no
        fridge, how do I can them so nobody gets botulism": "fridge" and
        "nobody" do not occur in the corpus, so each carried maximum weight
        and sank the score, though the pressure-canning procedure was right
        there."""
        terms = content_terms(query)
        fts = to_fts_query(query)
        if not fts:
            return [], 0.0

        # Over-fetch, because coverage filtering removes some of what BM25 ranked.
        fetch = max(limit * 4, 40)
        try:
            rows = self.conn.execute(SEARCH_SQL, (fts, fetch)).fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f"running search for {fts!r}: {e}") from e

        weights = self._term_weights(terms)
        # Added up one by one, as the Rust version does: sum() is more careful
        # with rounding, and would make a full match 0.9999999999999999.
        total = 0.0
        for weight in weights:
            total += weight

        out: list[Hit] = []
        for title, author, book, file, page, snip, score, body in rows:
            words = _words(f"{title} {body}".lower())
            carried = 0.0
            for term, weight in zip(terms, weights):
                # Prefix match at a word boundary, so "purify" finds
                # "purifying" but "age" does not find "storage".
                if any(w.startswith(term) for w in words):
                    carried += weight
            out.append(Hit(
                doc_title=title, author=author, book=book, file=file, page=page,
                snippet=snip, rank=score,
                coverage=carried / total if total > 0 else 0.0,
                body=body,
            ))
            if len(out) >= limit:
                break

        best = max((h.coverage for h in out), default=0.0)

        # Contents and index pages list every topic in a book and so match
        # almost any question about it, while containing none of the answer.
        # Asked how to can green beans safely, the top passage was the
        # contents page of a canning guide -- it named the topic and taught
        # nothing.
        #
        # They are demoted rather than dropped, because number-dense pages are
        # not all navigation: dosage charts and altitude tables look similar
        # and are exactly what someone needs. Measured on this corpus, prose
        # sits at 0.03-0.06, real tables reach 0.14 at the 95th percentile,
        # and the contents page measured 0.42. (A stable sort, so the BM25
        # order is kept within each group.)
        out.sort(key=lambda h: is_navigation_page(h.body))
        return out, best

    def _term_weights(self, terms: list[str]) -> list[float]:
        """Inverse document frequency for each term, measured against this
        index.

        A term the corpus has never seen gets the maximum weight: its absence
        is the strongest available evidence that the library does not cover
        the question being asked."""
        (total_pages,) = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()
        total_pages = max(float(total_pages), 1.0)
        weights = []
        for t in terms:
            try:
                (df,) = self.conn.execute(
                    "SELECT COUNT(*) FROM pages WHERE pages MATCH ?", (f'"{t}"',)
                ).fetchone()
            except sqlite3.Error:
                df = 0
            # Standard smoothed IDF; df of 0 yields the largest weight.
            weights.append(max(math.log((total_pages + 1.0) / (df + 1.0)), 0.0))
        return weights

    def stats(self) -> IndexStats:
        documents, pages, words, image_only = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pages),0), COALESCE(SUM(words),0),"
            " COALESCE(SUM(image_only),0) FROM docs"
        ).fetchone()
        return IndexStats(documents=documents, pages=pages, words=words, image_only=image_only)

    def books(self) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT book, COUNT(*) FROM docs GROUP BY book ORDER BY book"
        ).fetchall()
        return [(book, count) for book, count in rows]


def coverage(text: str, terms: list[str], weights: list[float]) -> float:
    """The share of the terms' weight that `text` carries, matched as
    search_ungated matches them (a prefix at a word boundary)."""
    total = 0.0
    for weight in weights:
        total += weight
    if total <= 0:
        return 0.0
    words = _words(text.lower())
    carried = 0.0
    for term, weight in zip(terms, weights):
        if any(w.startswith(term) for w in words):
            carried += weight
    return carried / total


def to_fts_query(text: str) -> str:
    """Turn a natural-language question into an FTS5 query.

    Everything is quoted and OR-ed. Quoting matters: FTS5 treats bare
    punctuation and words like NOT and NEAR as syntax, so an unescaped
    question ("what do I do about a wound that won't close?") is a syntax
    error rather than a search. OR rather than AND because a question's
    phrasing rarely matches a document's; BM25 handles the ranking and the
    reranker fixes the ordering afterwards."""
    return " OR ".join(f'"{w}"' for w in content_terms(text))


# Fraction of tokens above which a page reads as navigation rather than
# content. Chosen from the corpus: comfortably above real tables (0.14 at the
# 95th percentile) and comfortably below a measured contents page (0.42).
NAVIGATION_NUMBER_DENSITY = 0.30


def is_navigation_page(body: str) -> bool:
    """Whether a page looks like a table of contents or an index: mostly
    entries and page numbers. Used as a sort key, so False (content) sorts
    first."""
    tokens = body.split()
    if len(tokens) < 20:
        return False
    numeric = 0
    for t in tokens:
        core = "".join(c for c in t if c not in ".,()[]-:;")
        if core and len(core) <= 6 and all("0" <= c <= "9" for c in core):
            numeric += 1
    return numeric / len(tokens) > NAVIGATION_NUMBER_DENSITY


STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "do", "does", "did", "how",
    "what", "when", "where", "which", "who", "why", "i", "my", "me", "you", "your", "it", "its",
    # "can" is deliberately not here: as a modal verb it is so common that
    # IDF weighting already makes it harmless, and as a verb it is the whole
    # topic of a canning question.
    "of", "to", "in", "on", "for", "with", "and", "or", "if", "should", "would", "about",
    "from", "that", "this", "there", "without", "any", "some", "not", "no", "get", "make",
    "use", "using", "need", "want", "have", "has",
}


def content_terms(text: str) -> list[str]:
    """The words in a question that carry meaning, lowercased and
    de-duplicated, in the order they appear."""
    seen: list[str] = []
    for w in _words(text):
        w = w.lower()
        # "Longer than two" is counted in UTF-8 bytes, as the Rust version
        # counts it, so both pick the same terms from the same question.
        if len(w.encode("utf-8")) > 2 and w not in STOP_WORDS and w not in seen:
            seen.append(w)
    return seen


def _words(text: str) -> list[str]:
    """Runs of letters and digits; everything else separates words.

    The marks that combine with a letter count as part of the word, so that
    "पानी" (water) is one word and not the consonants on either side of its
    vowel signs. The Rust version splits text the same way, apart from a few
    rare marks and symbols that Unicode lists as letters and Python cannot
    tell apart from the rest."""
    words: list[str] = []
    word = ""
    for c in text:
        if c.isalnum() or unicodedata.category(c) in ("Mn", "Mc"):
            word += c
        elif word:
            words.append(word)
            word = ""
    if word:
        words.append(word)
    return words
