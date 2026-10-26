"""PDF extraction. The normalising tests always run; the extraction tests need
the pdfium library: MUSEION_PDFIUM, a drive's or this computer's (see
helper_programs.py)."""

import unittest
from pathlib import Path

import helper_programs
from museion.pdf import Extracted, PdfExtractor, normalise, sample_pdf

PDFIUM = helper_programs.pdfium_dir()


class NormaliseTest(unittest.TestCase):
    def test_joins_words_split_across_lines(self):
        self.assertEqual(normalise("purifi-\ncation of water"), "purification of water")
        self.assertEqual(normalise("purifi-\r\ncation of water"), "purification of water")

    def test_collapses_runs_of_whitespace(self):
        self.assertEqual(normalise("boil   the\t\twater\n\n"), "boil the water")


class ImageOnlyTest(unittest.TestCase):
    def test_a_scan_with_almost_no_words_is_image_only(self):
        self.assertTrue(Extracted(page_count=10, word_count=50).is_image_only())
        self.assertFalse(Extracted(page_count=10, word_count=2650).is_image_only())
        self.assertFalse(Extracted(page_count=0, word_count=0).is_image_only())


@unittest.skipUnless(PDFIUM, "pdfium not found: set MUSEION_PDFIUM to the folder holding it")
class ExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extractor = PdfExtractor(Path(PDFIUM))

    def test_extracts_each_page_with_its_number(self):
        got = self.extractor.extract(sample_pdf(["boil the water", "tie a bowline"]))
        self.assertEqual(got.page_count, 2)
        self.assertEqual([(p.number, p.text) for p in got.pages], [(1, "boil the water"), (2, "tie a bowline")])
        self.assertEqual(got.word_count, 6)

    def test_empty_pages_are_counted_but_not_kept(self):
        got = self.extractor.extract(sample_pdf(["first", "", "third"]))
        self.assertEqual(got.page_count, 3)
        self.assertEqual([p.number for p in got.pages], [1, 3])

    def test_a_file_that_is_not_a_pdf_says_so(self):
        with self.assertRaisesRegex(ValueError, "not a PDF"):
            self.extractor.extract(b"<html>not a pdf</html>")


if __name__ == "__main__":
    unittest.main()
