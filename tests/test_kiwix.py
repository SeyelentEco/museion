"""Talking to kiwix-serve, with tests/fake_kiwix.py standing in for it so no
archives are needed."""

import unittest

import index_corpus
from fake_kiwix import FakeKiwix
from museion import sidecar
from museion.kiwix import (
    FULL_TEXT_INDEXED,
    PDF_COLLECTION,
    CatalogedDoc,
    Kiwix,
    SearchHit,
    book_id_from_filename,
    encode_component,
    encode_path,
    html_to_text,
    parse_database_js,
    parse_opensearch,
)

# kiwix-serve sends the snippet's highlighting as escaped tags, which is why
# fields are unescaped first and stripped of tags after.
SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <channel>
    <title>Search: soap</title>
    <item>
      <title>Soap</title>
      <link>/content/wikipedia_en_chemistry_nopic_2026-07/Soap</link>
      <description>...a &lt;b&gt;soap&lt;/b&gt; is a salt of a fatty acid &amp; is made from lye...</description>
      <book><title>Chemistry</title></book>
    </item>
    <item>
      <title>Lye</title>
      <link>/content/wikipedia_en_chemistry_nopic_2026-07/Lye</link>
      <description>used in making
        &lt;b&gt;soap&lt;/b&gt;</description>
    </item>
  </channel>
</rss>"""


class TextTest(unittest.TestCase):
    def test_article_html_reduces_to_prose(self):
        html = """<html><head><style>p{}</style><script>x()</script></head>
            <body><nav>Menu</nav><h1>Wood ash</h1>
            <p>Wood ash is the residue &amp; powder left after combustion.</p>
            <table><tr><td>ignored</td></tr></table>
            <p>Lye can be leached from it.</p></body></html>"""
        t = html_to_text(html)
        self.assertIn("Wood ash", t)
        self.assertIn("residue & powder", t)
        self.assertIn("Lye can be leached", t)
        self.assertNotIn("Menu", t)
        self.assertNotIn("ignored", t)
        self.assertNotIn("x()", t)

    def test_each_block_becomes_a_line(self):
        html = "<body><h2>Making  lye</h2><div>Soak&nbsp;the ash<br>in rain water</div></body>"
        self.assertEqual(html_to_text(html), "Making lye\nSoak the ash\nin rain water")

    def test_search_hit_path_strips_the_content_prefix(self):
        h = SearchHit(title="Soap", link="/content/wikipedia_en_chemistry_nopic_2026-07/Soap", snippet="")
        self.assertEqual(h.path(), "Soap")
        self.assertEqual(SearchHit("", "/content/book/A/Soap_(chemistry)", "").path(), "A/Soap_(chemistry)")
        self.assertIsNone(SearchHit("", "/catalog/book/Soap", "").path())
        self.assertIsNone(SearchHit("", "/content/book", "").path())

    def test_opensearch_results_are_read(self):
        hits = parse_opensearch(SEARCH_XML)
        self.assertEqual([(h.title, h.path()) for h in hits], [("Soap", "Soap"), ("Lye", "Lye")])
        self.assertEqual(hits[0].snippet, "...a soap is a salt of a fatty acid & is made from lye...")
        self.assertEqual(hits[1].snippet, "used in making soap")

    def test_database_js_names_the_documents(self):
        docs = parse_database_js(index_corpus.DATABASE_JS)
        self.assertEqual(docs, [
            CatalogedDoc("Emergency Preparedness (1).pdf", "Don't Drink the Water",
                         "Storing and treating water at home", "FEMA"),
            CatalogedDoc("Emergency Preparedness (2).pdf", "", "", ""),
            CatalogedDoc("Emergency Preparedness (3).pdf", "Missing From The Archive",
                         "Listed but not present", "Nobody"),
        ], "the cover image is not a document")
        self.assertEqual(parse_database_js("var DATABASE = [];"), [])

    def test_paths_are_percent_encoded_but_keep_their_separators(self):
        self.assertEqual(encode_path("files/Emergency Preparedness (1).pdf"),
                         "files/Emergency%20Preparedness%20%281%29.pdf")
        self.assertEqual(encode_component("a-b_c.d~e/f&g"), "a-b_c.d~e%2Ff%26g")
        self.assertEqual(encode_component("café"), "caf%C3%A9")

    def test_book_ids_come_from_archive_filenames(self):
        self.assertEqual(book_id_from_filename("wikipedia_en_chemistry_nopic_2026-07.zim"),
                         "wikipedia_en_chemistry_nopic_2026-07")
        with self.assertRaisesRegex(ValueError, "is not a .zim file"):
            book_id_from_filename("notes.pdf")


class KiwixServerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeKiwix()
        self.fake.entries.update(index_corpus.kiwix_entries())
        self.kiwix = Kiwix(self.fake.url + "/")

    def tearDown(self):
        self.fake.close()

    def test_is_up_only_when_kiwix_serve_answers(self):
        self.assertTrue(self.kiwix.is_up())
        self.assertFalse(Kiwix(f"http://127.0.0.1:{sidecar.free_port()}").is_up())

    def test_content_is_byte_exact(self):
        """The reason archives are read over HTTP: zimdump on Windows turned
        every line feed into CR LF and broke every PDF it touched."""
        data = bytes(range(256)) + b"\n\r\n\x00"
        self.fake.entries["book/files/raw data (1).bin"] = data
        self.assertEqual(self.kiwix.content("book", "files/raw data (1).bin"), data)
        self.assertEqual(self.fake.requests[-1], "/content/book/files/raw%20data%20%281%29.bin")

    def test_a_missing_entry_is_an_error(self):
        with self.assertRaisesRegex(ConnectionError, "fetching .*/content/book/nothing"):
            self.kiwix.content("book", "nothing")

    def test_classifies_archives_by_what_is_inside(self):
        kind = self.kiwix.classify(index_corpus.ARCHIVE)
        self.assertEqual(kind.kind, PDF_COLLECTION)
        self.assertEqual(len(kind.docs), 3)
        # No database.js at all, and one that lists no PDFs, both mean the
        # archive's own index is used.
        self.assertEqual(self.kiwix.classify(index_corpus.NATIVE).kind, FULL_TEXT_INDEXED)
        self.fake.entries["viewer/database.js"] = b"var DATABASE = [];"
        self.assertEqual(self.kiwix.classify("viewer").kind, FULL_TEXT_INDEXED)

    def test_searches_an_archive_through_its_own_index(self):
        self.fake.search_xml = SEARCH_XML
        hits = self.kiwix.search("wikipedia_en_chemistry_nopic_2026-07", "soap & lye", 5)
        self.assertEqual([h.title for h in hits], ["Soap", "Lye"])
        self.assertEqual(
            self.fake.requests[-1],
            "/search?books.name=wikipedia_en_chemistry_nopic_2026-07&pattern=soap%20%26%20lye"
            "&format=xml&pageLength=5",
        )

    def test_reads_an_article_as_text(self):
        self.fake.entries["wiki/A/Lye"] = "<body><p>Lye is caustic &amp; café-safe.</p></body>".encode()
        self.assertEqual(self.kiwix.article_text("wiki", "A/Lye"), "Lye is caustic & café-safe.")


if __name__ == "__main__":
    unittest.main()
