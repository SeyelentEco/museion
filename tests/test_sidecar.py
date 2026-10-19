"""Supervising helpers, with Python's own http.server standing in for
llama-server so no model is needed. The test that matters most is the last:
a helper must not outlive a Museion that was killed outright."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from museion import sidecar

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def fake_helper(port: int) -> list[str]:
    return [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]


def wait_closed(port: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not sidecar.port_open(port):
            return True
        time.sleep(0.2)
    return False


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.sup = sidecar.Supervisor()

    def tearDown(self):
        self.sup.stop_all()

    def test_start_wait_and_stop(self):
        port = sidecar.free_port()
        self.sup.start(sidecar.KIWIX, fake_helper(port), port)
        self.sup.wait_ready(sidecar.KIWIX, f"http://127.0.0.1:{port}/", timeout=20)
        self.assertTrue(self.sup.is_running(sidecar.KIWIX))
        self.assertEqual(self.sup.port_for(sidecar.KIWIX), port)
        self.sup.stop(sidecar.KIWIX)
        self.assertFalse(self.sup.is_running(sidecar.KIWIX))
        self.assertTrue(wait_closed(port))

    def test_a_helper_that_dies_while_starting_is_reported_at_once(self):
        port = sidecar.free_port()
        self.sup.start(sidecar.CHAT, [sys.executable, "-c", "import sys; sys.exit(3)"], port)
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "exited while starting up"):
            self.sup.wait_ready(sidecar.CHAT, f"http://127.0.0.1:{port}/health", timeout=60)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_missing_model_is_refused_before_anything_starts(self):
        with self.assertRaisesRegex(FileNotFoundError, "model not found"):
            self.sup.start_llama(sidecar.CHAT, Path("llama-server"), Path("no-such-model.gguf"), 1)

    def test_a_missing_program_says_which(self):
        with self.assertRaisesRegex(RuntimeError, "starting chat model"):
            self.sup.start(sidecar.CHAT, ["no-such-program-anywhere"], 1)

    def test_kiwix_needs_archives(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(RuntimeError, "no .zim archives"):
                self.sup.start_kiwix(Path("kiwix-serve"), [Path(empty)], 1)


class AdoptTest(unittest.TestCase):
    """A server already running on the port is used, not restarted -- but
    only if it is serving the expected model -- and never killed on exit."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        Path(self.dir.name, "qwen-chat.gguf").write_text("")
        self.port = sidecar.free_port()
        # http.server's directory listing names the files in its folder, which
        # stands in for llama-server naming its model in /health.
        self.other = subprocess.Popen(fake_helper(self.port), cwd=self.dir.name,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while not sidecar.port_open(self.port) and time.monotonic() < deadline:
            time.sleep(0.1)

    def tearDown(self):
        self.other.kill()
        self.other.wait()
        self.dir.cleanup()

    def test_adopts_only_the_expected_model(self):
        url = f"http://127.0.0.1:{self.port}/"
        self.assertFalse(sidecar.Supervisor().adopt(sidecar.CHAT, self.port, url, Path("other-model.gguf")))
        sup = sidecar.Supervisor()
        self.assertTrue(sup.adopt(sidecar.CHAT, self.port, url, Path("models/qwen-chat.gguf")))
        self.assertTrue(sup.is_running(sidecar.CHAT))
        # Leaving the app does not kill a server that was not ours.
        sup.release_all()
        self.assertTrue(sidecar.port_open(self.port))


@unittest.skipIf(sys.platform == "darwin", "macOS offers no way to tie a child to its parent's life")
class DiesWithMuseionTest(unittest.TestCase):
    def test_helper_dies_when_museion_is_killed(self):
        port = sidecar.free_port()
        # A separate Museion process that starts a helper, says so, and waits.
        script = (
            "import sys, time\n"
            "from museion import sidecar\n"
            "sup = sidecar.Supervisor()\n"
            f"sup.start(sidecar.CHAT, {fake_helper(port)!r}, {port})\n"
            f"sup.wait_ready(sidecar.CHAT, 'http://127.0.0.1:{port}/', timeout=20)\n"
            "print('ready', flush=True)\n"
            "time.sleep(600)\n"
        )
        museion = subprocess.Popen([sys.executable, "-c", script], cwd=PACKAGE_ROOT,
                                   stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(museion.stdout.readline().strip(), "ready")
            self.assertTrue(sidecar.port_open(port))
        finally:
            # The hard way, as a crash or Task Manager would: no exit hooks run.
            museion.kill()
            museion.wait()
            museion.stdout.close()
        survived = not wait_closed(port)
        if survived:
            sidecar.kill_listener(port)
        self.assertFalse(survived, "the helper outlived Museion and would keep holding the GPU")


if __name__ == "__main__":
    unittest.main()
