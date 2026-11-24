"""Asking for the latest release and fetching it, against a local server
answering as GitHub does."""

import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fake_http import FakeServer
from museion import catalog, newer, upgrade_cli, upgrade_commands
from museion.appstate import UiError

PACKAGE = b"a Museion package " * 1000


class NewerTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, name, value in ((catalog, "current_platform", lambda: "linux"), (catalog, "current_arch", lambda: "x64"),
                                    (newer, "LATEST", self.server.url + "/latest"), (newer, "VERSION", "0.2.0")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def release(self, version: str = "0.3.0", digest: bool = True, files=("linux.tar.gz", "windows.zip")) -> None:
        assets = []
        for kind in files:
            name = f"museion-{version}-{kind}"
            self.server.files["/dl/" + name] = PACKAGE
            assets.append({"name": name, "size": len(PACKAGE), "browser_download_url": f"{self.server.url}/dl/{name}",
                           **({"digest": "sha256:" + hashlib.sha256(PACKAGE).hexdigest()} if digest else {})})
        sums = "".join(f"{hashlib.sha256(PACKAGE).hexdigest()}  museion-{version}-{kind}\n" for kind in files)
        self.server.files["/dl/SHA256SUMS.txt"] = sums.encode()
        assets.append({"name": "SHA256SUMS.txt", "size": len(sums), "browser_download_url": f"{self.server.url}/dl/SHA256SUMS.txt"})
        self.server.files["/latest"] = json.dumps({
            "tag_name": f"v{version}", "html_url": "https://example.org/releases/v" + version,
            "body": "- something new\n", "assets": assets}).encode()

    def test_the_latest_release_and_this_systems_download(self):
        self.release()
        r = newer.latest()
        self.assertEqual((r.version, r.file, r.size, r.notes), ("0.3.0", "museion-0.3.0-linux.tar.gz", len(PACKAGE), "- something new"))
        self.assertEqual(r.sha256, hashlib.sha256(PACKAGE).hexdigest())
        self.assertTrue(r.is_newer())
        self.release("0.2.0")
        self.assertFalse(newer.latest().is_newer())

    def test_the_checksum_comes_from_the_sums_file_when_the_asset_has_none(self):
        self.release(digest=False)
        self.assertEqual(newer.latest().sha256, hashlib.sha256(PACKAGE).hexdigest())

    def test_a_release_with_no_download_for_this_system(self):
        self.release(files=("windows.zip",))
        r = newer.latest()
        self.assertEqual((r.version, r.file, r.url), ("0.3.0", "", ""))
        with self.assertRaisesRegex(newer.Unknown, "no download for this kind of computer"):
            newer.fetch(r, Path(self.tmp.name))

    def test_no_release_or_no_internet_is_said_plainly(self):
        with self.assertRaisesRegex(newer.Unknown, "shows no release"):
            newer.latest()
        with mock.patch.object(newer, "LATEST", "http://127.0.0.1:9/latest"), \
                self.assertRaisesRegex(newer.Unknown, "could not reach GitHub"):
            newer.latest(newer.LATEST)

    def test_fetching_checks_the_file_and_does_not_fetch_it_twice(self):
        self.release()
        seen: list[tuple[int, int]] = []
        file = newer.fetch(newer.latest(), Path(self.tmp.name), lambda done, total: seen.append((done, total)))
        self.assertEqual(file.read_bytes(), PACKAGE)
        self.assertEqual(seen[-1], (len(PACKAGE), len(PACKAGE)))
        asked = len(self.server.requests)
        newer.fetch(newer.latest(), Path(self.tmp.name))
        self.assertEqual(len(self.server.requests), asked + 1, "only the question, not the file again")

    def test_a_file_that_does_not_match_its_checksum_is_refused(self):
        self.release()
        r = newer.latest()
        r.sha256 = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "all sources failed"):
            newer.fetch(r, Path(self.tmp.name))
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_the_pages_commands(self):
        root = Path(self.tmp.name) / "copy"
        root.mkdir()
        events: list[str] = []
        state = types.SimpleNamespace(drive=types.SimpleNamespace(root=root),
                                      emit=lambda name, payload: events.append(payload["line"]))
        self.release()
        seen = upgrade_commands.newer_version(state)
        self.assertEqual((seen["version"], seen["newer"], seen["can_fetch"]), ("0.3.0", True, True))
        self.assertFalse(upgrade_commands.newer_version(types.SimpleNamespace(drive=None))["can_fetch"])
        with mock.patch.object(upgrade_commands, "upgrade_from", lambda state, path, port: {"to": "0.3.0", "path": path}):
            done = upgrade_commands.fetch_upgrade(state, 8070)
        self.assertEqual(done["path"], str(root / "upgrade" / "downloads" / "museion-0.3.0-linux.tar.gz"))
        self.assertEqual(Path(done["path"]).read_bytes(), PACKAGE)
        self.assertEqual(events[0], "Downloading museion-0.3.0-linux.tar.gz")
        # Nothing newer: nothing fetched.
        self.release("0.2.0")
        with self.assertRaisesRegex(UiError, "is the latest"):
            upgrade_commands.fetch_upgrade(state, 8070)
        del self.server.files["/latest"]
        with self.assertRaisesRegex(UiError, "shows no release"):
            upgrade_commands.newer_version(state)


if __name__ == "__main__":
    unittest.main()
