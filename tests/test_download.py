"""Resumable, verified downloads, against a local server that can ignore
Range, hang up part-way, or serve the wrong bytes."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from fake_http import FakeServer
from museion import download

BODY = bytes(range(256)) * 1200  # 300 KB
SHA = hashlib.sha256(BODY).hexdigest()


class IsVerifiedTest(unittest.TestCase):
    def test_verified_rejects_wrong_size_before_hashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "x.bin")
            p.write_bytes(b"hello")
            # Right hash for "hello" but wrong declared size: must be false.
            h = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
            self.assertFalse(download.is_verified(p, 99, h))
            self.assertTrue(download.is_verified(p, 5, h))
            self.assertFalse(download.is_verified(p, 5, "0" * 64))

    def test_a_missing_file_is_not_verified(self):
        self.assertFalse(download.is_verified(Path("no-such-file.bin"), 5, SHA))


class DownloadVerifiedTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.files = {"/file": BODY, "/norange": BODY, "/bad": BODY[:-1] + b"x",
                             "/cut": BODY, "/short": BODY[:1000]}
        self.server.ignore_range = {"/norange"}
        self.server.cut = {"/cut": len(BODY) // 2}
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name, "sub", "library.zim")
        self.part = Path(self.tmp.name, "sub", "library.zim.part")
        self.seen: list[download.Progress] = []

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def get(self, *paths: str) -> str:
        spec = download.Spec([self.server.url + p for p in paths], self.dest, len(BODY), SHA)
        return download.download_verified(spec, self.seen.append)

    def write_part(self, data: bytes) -> None:
        self.part.parent.mkdir(parents=True, exist_ok=True)
        self.part.write_bytes(data)

    def test_downloads_and_renames_only_when_the_hash_matches(self):
        self.assertEqual(self.get("/file"), self.server.url + "/file")
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertFalse(self.part.exists())
        self.assertEqual(self.server.requests, [("/file", None)])
        self.assertEqual(self.seen[-1], download.Progress(len(BODY), len(BODY), False))

    def test_resumes_a_partial_file_with_a_range_request(self):
        self.write_part(BODY[:1000])
        self.get("/file")
        self.assertEqual(self.server.requests, [("/file", "bytes=1000-")])
        self.assertEqual(self.dest.read_bytes(), BODY)
        # What was on disk is re-hashed first, and reported as such.
        self.assertEqual(self.seen[0], download.Progress(1000, 1000, True))

    def test_a_server_that_ignores_range_is_detected(self):
        # Appending its 200 reply would put a second copy after the first 1000 bytes.
        self.write_part(BODY[:1000])
        with self.assertRaisesRegex(RuntimeError, "does not support resume \\(replied 200, expected 206\\)"):
            self.get("/norange")
        self.assertFalse(self.part.exists())
        self.assertFalse(self.dest.exists())
        # With the partial file gone, the next try starts from zero and works.
        self.get("/norange")
        self.assertEqual(self.dest.read_bytes(), BODY)

    def test_a_bad_hash_deletes_the_partial_file(self):
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch\n    expected " + SHA):
            self.get("/bad")
        self.assertFalse(self.part.exists())
        self.assertFalse(self.dest.exists())

    def test_mirrors_are_tried_in_order(self):
        self.assertEqual(self.get("/missing", "/bad", "/file", "/norange"), self.server.url + "/file")
        self.assertEqual([p for p, _ in self.server.requests], ["/missing", "/bad", "/file"])
        self.assertEqual(self.dest.read_bytes(), BODY)

    def test_an_interrupted_download_resumes_from_the_next_mirror(self):
        self.get("/cut", "/file")
        self.assertEqual(self.server.requests, [("/cut", None), ("/file", f"bytes={len(BODY) // 2}-")])
        self.assertEqual(self.dest.read_bytes(), BODY)

    def test_a_too_short_download_keeps_its_bytes_for_a_resume(self):
        with self.assertRaisesRegex(RuntimeError, f"size mismatch: expected {len(BODY)} bytes, got 1000"):
            self.get("/short")
        self.assertEqual(self.part.read_bytes(), BODY[:1000])

    def test_an_oversized_partial_file_is_discarded(self):
        self.write_part(BODY + b"extra")
        self.get("/file")
        self.assertEqual(self.server.requests, [("/file", None)])
        self.assertEqual(self.dest.read_bytes(), BODY)

    def test_when_everything_fails_the_file_and_last_error_are_named(self):
        with self.assertRaises(RuntimeError) as caught:
            self.get("/missing", "/bad")
        message = str(caught.exception)
        self.assertIn(f"all sources failed for {self.dest}", message)
        self.assertIn("last error: checksum mismatch", message)


class FetchUnverifiedTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name, "MicrosoftEdgeWebview2Setup.exe")

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def test_a_large_enough_file_is_kept(self):
        body = BODY * 4  # 1.2 MB
        self.server.files["/setup.exe"] = body
        seen = []
        download.fetch_unverified(self.server.url + "/setup.exe", self.dest, lambda r, t: seen.append((r, t)))
        self.assertEqual(self.dest.read_bytes(), body)
        self.assertEqual(seen[-1], (len(body), len(body)))
        self.assertFalse(self.dest.with_suffix(".part").exists())

    def test_a_tiny_reply_is_an_error_page_not_an_installer(self):
        self.server.files["/setup.exe"] = b"<html>moved</html>"
        with self.assertRaisesRegex(RuntimeError, "returned only 18 bytes"):
            download.fetch_unverified(self.server.url + "/setup.exe", self.dest)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix(".part").exists())


if __name__ == "__main__":
    unittest.main()
