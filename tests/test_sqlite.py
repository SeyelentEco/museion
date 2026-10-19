"""The local index depends on SQLite FTS5 being built into Python's sqlite3.
If this fails, the Python running Museion cannot build or search the index."""

import sqlite3
import unittest


class Fts5Test(unittest.TestCase):
    def test_fts5_matches_and_ranks(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE pages USING fts5(body)")
        db.executemany(
            "INSERT INTO pages(body) VALUES (?)",
            [("boil the water for one minute",), ("water water water purification",), ("tie a bowline",)],
        )
        rows = db.execute(
            "SELECT body FROM pages WHERE pages MATCH 'water' ORDER BY bm25(pages)"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        # bm25 ranks the page that is mostly about water first.
        self.assertEqual(rows[0][0], "water water water purification")


if __name__ == "__main__":
    unittest.main()
