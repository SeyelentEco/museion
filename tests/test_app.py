"""The Museion window's server (museion/app.py) and the commands behind it,
spoken to over HTTP the way the page speaks to it.

The helpers are the fakes the other tests use, already running on their
ports, so start_services adopts them as it adopts servers left running. The
index is the one the Rust indexer built, tests/fixtures/rust-index.db.
"""

import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fake_kiwix import FakeKiwix
from fake_llama import FakeLlama, text
from museion import app, sidecar, tiny_gguf
from museion.appstate import AppState
from museion.pdf import sample_pdf

FIXTURE_DB = Path(__file__).resolve().parent / "fixtures" / "rust-index.db"
LLAMA_SERVER = os.environ.get("MUSEION_LLAMA_SERVER")
QUESTION = "how much water should I store per person"


def port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


class Running:
    """A Museion server on its own thread, over a configuration in a
    temporary folder, with fake helpers."""

    def __init__(self, folder: Path, kiwix: FakeKiwix, chat: FakeLlama, rerank: FakeLlama) -> None:
        self.folder = folder
        self.library = folder / "library"
        self.found = folder / "found"
        self.models = folder / "models"
        for d in (self.library, self.found, self.models, folder / "rerank", folder / "config"):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE_DB, self.library / "museion-index.db")
        (self.found / "Guide.pdf").write_bytes(sample_pdf(["Store water in clean containers"]))
        (self.found / "notes.txt").write_text("not a pdf")
        # The fakes report "fake-model.gguf", which is what lets them be
        # adopted as these models.
        (self.models / "fake-model.gguf").write_bytes(b"GGUF")
        (self.models / "other.gguf").write_bytes(b"GGUF")
        (self.models / "qwen3-reranker.gguf").write_bytes(b"GGUF")
        (folder / "rerank" / "fake-model.gguf").write_bytes(b"GGUF")
        config = {
            "library_dir": str(self.library),
            "extra_dirs": [str(self.found)],
            "kiwix_exe": str(folder / "kiwix-serve"),
            "llama_exe": str(folder / "llama-server"),
            "models_dir": str(self.models),
            "chat_model": str(self.models / "fake-model.gguf"),
            "reranker_model": str(folder / "rerank" / "fake-model.gguf"),
            "kiwix_port": port_of(kiwix.url),
            "chat_port": port_of(chat.url),
            "reranker_port": port_of(rerank.url),
        }
        (folder / "config" / "config.json").write_text(json.dumps(config))
        self.events = app.Events()
        self.state = AppState(folder / "config" / "config.json", folder / "config" / "tuning.toml", self.events.emit)
        self.server = app.Server(self.state, self.events, 0)
        self.port = self.server.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method: str, path: str, body: object = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = {"Content-Type": "application/json", "X-Museion": "1"} if method == "POST" else {}
        sent.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=sent)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        return res, payload

    def call(self, command: str, **args):
        """Run a command; return its result, or raise CommandError with
        what it said went wrong."""
        res, payload = self.request("POST", f"/api/{command}", args)
        body = json.loads(payload)
        if res.status != 200 or "error" in body:
            raise CommandError(res.status, body.get("error"))
        return body["ok"]

    def close(self) -> None:
        self.server.quit()
        self.thread.join(10)
        self.server.server_close()


class CommandError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class EventReader:
    """Collects the events the server streams, as the page's EventSource does.
    A plain socket, so that closing it really hangs up, as a closed tab does."""

    def __init__(self, port: int) -> None:
        self.events: list[tuple[str, object]] = []
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.sock.sendall(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        name = None
        try:
            for raw in self.sock.makefile("rb"):
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event: "):
                    name = line[len("event: "):]
                elif line.startswith("data: ") and name:
                    self.events.append((name, json.loads(line[len("data: "):])))
                    name = None
        except (OSError, ValueError):
            pass

    def named(self, name: str) -> list:
        return [payload for n, payload in self.events if n == name]

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class AppServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.kiwix = FakeKiwix()
        cls.chat = FakeLlama()
        cls.rerank = FakeLlama()
        cls.rerank.rerank_scores = [0.9 - i * 0.02 for i in range(40)]
        cls.app = Running(Path(cls.tmp.name), cls.kiwix, cls.chat, cls.rerank)

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        for fake in (cls.kiwix, cls.chat, cls.rerank):
            fake.close()
        cls.tmp.cleanup()

    def test_serves_the_page_and_nothing_outside_it(self):
        res, body = self.app.request("GET", "/")
        self.assertEqual(res.status, 200)
        self.assertIn(b"<title>Museion</title>", body)
        self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        res, _ = self.app.request("GET", "/main.js")
        self.assertEqual(res.getheader("Content-Type"), "text/javascript; charset=utf-8")
        res, _ = self.app.request("GET", "/../app.py")
        self.assertEqual(res.status, 404)
        res, _ = self.app.request("GET", "/%2e%2e/app.py")
        self.assertEqual(res.status, 404)

    def test_refuses_commands_from_anything_but_the_page(self):
        host = f"127.0.0.1:{self.app.port}"
        for headers in ({"X-Museion": ""},                                   # another site's form or fetch
                        {"Host": f"evil.example:{self.app.port}"},           # DNS rebinding
                        {"Origin": "http://evil.example"}):                  # another site's page
            with self.subTest(headers=headers):
                res, _ = self.app.request("POST", "/api/get_status", {}, headers)
                self.assertEqual(res.status, 403)
        res, _ = self.app.request("POST", "/api/get_status", {}, {"Origin": f"http://{host}"})
        self.assertEqual(res.status, 200)
        res, _ = self.app.request("GET", "/api/events", headers={"Host": "evil.example"})
        self.assertEqual(res.status, 403)

    def test_unknown_commands_and_wrong_arguments_are_reported(self):
        with self.assertRaises(CommandError) as e:
            self.app.call("no_such_command")
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(CommandError) as e:
            self.app.call("ask", nonsense=1)
        self.assertEqual(e.exception.status, 400)
        self.assertIn("ask:", e.exception.message)

    def test_status_and_settings(self):
        st = self.app.call("get_status")
        self.assertTrue(st["configured"])
        self.assertEqual(st["documents"], 6)
        self.assertEqual(st["chat_model"], "fake-model.gguf")

        cfg = self.app.call("get_config")
        self.assertEqual(cfg["extra_dirs"], [str(self.app.found)])
        self.assertTrue(cfg["autostart"])
        self.app.call("set_config", config={**cfg, "autostart": False})
        saved = json.loads(self.app.state.config_path.read_text())
        self.assertFalse(saved["autostart"])
        self.assertEqual(saved["library_dir"], str(self.app.library))
        self.app.call("set_config", config=cfg)

    def test_start_services_adopts_helpers_already_running(self):
        events = EventReader(self.app.port)
        try:
            time.sleep(0.2)
            self.app.call("start_services")
            flags = self.app.call("service_state")
            self.assertEqual(flags, {"kiwix_running": True, "chat_running": True, "reranker_running": True})
            time.sleep(0.3)
            said = events.named("service-progress")
            self.assertIn("Content server already running; using it.", said)
            self.assertIn("Reranker already running; using it.", said)
            self.assertEqual(said[-1], "Ready.")
        finally:
            events.close()

    def test_ask_streams_the_answer_and_returns_it_numbered_to_its_sources(self):
        self.chat.script = [[text("water storage\nstoring water")],
                            [text("Store four litres a day "), text("[3].")]]
        events = EventReader(self.app.port)
        try:
            time.sleep(0.2)
            answer = self.app.call("ask", question=QUESTION, history=[])
            time.sleep(0.3)
            # The stream is the model's own words; the result renumbers the
            # citation to match the one source kept.
            self.assertEqual("".join(events.named("answer-token")), "Store four litres a day [3].")
            self.assertEqual(answer["text"], "Store four litres a day [1].")
            self.assertEqual(answer["basis"], "library")
            self.assertEqual([s["n"] for s in answer["sources"]], [1])
            self.assertEqual(set(answer["sources"][0]),
                             {"n", "title", "author", "page", "book", "file", "snippet"})
        finally:
            events.close()

    def test_ask_answers_arithmetic_without_a_model(self):
        answer = self.app.call("ask", question="17 * 23")
        self.assertEqual(answer["basis"], "computed")
        self.assertEqual(answer["calculations"], [{"expression": "17 * 23", "result": "391"}])

    def test_ask_says_when_the_chat_model_is_not_running(self):
        cfg = self.app.state.config()
        moved = cfg.copy()
        moved.chat_port = sidecar.free_port()
        self.app.state.save_config(moved)
        try:
            with self.assertRaises(CommandError) as e:
                self.app.call("ask", question=QUESTION)
        finally:
            self.app.state.save_config(cfg)
        # An expected refusal is an answer, not a failed request: the page
        # shows the sentence, and the browser logs no error.
        self.assertEqual(e.exception.status, 200)
        self.assertEqual(e.exception.message, "The chat model isn't running. Open Settings and press Start services.")

    def test_benchmark_runs_saves_and_lists(self):
        run = self.app.call("run_evals", label="arithmetic only", only="arithmetic")
        self.assertEqual((run["passed"], run["total"], run["label"]), (1, 1, "arithmetic only"))
        runs = self.app.call("list_eval_runs")
        self.assertIn(run["id"], [r["id"] for r in runs])
        with self.assertRaises(CommandError) as e:
            self.app.call("run_evals", label="x", only="no-such-case")
        self.assertEqual(e.exception.message, "no cases match")

    def test_models_for_the_benchmark_leave_out_rerankers(self):
        models = self.app.call("list_models")
        self.assertEqual([m["name"] for m in models], ["fake-model.gguf", "other.gguf"])
        self.assertTrue(models[0]["current"])

    def test_browse_lists_folders_and_the_files_wanted(self):
        listing = self.app.call("browse", path=str(self.app.models), want="gguf")
        self.assertEqual(listing["path"], str(self.app.models))
        names = [e["name"] for e in listing["entries"]]
        self.assertEqual(names, ["fake-model.gguf", "other.gguf", "qwen3-reranker.gguf"])
        listing = self.app.call("browse", path=str(self.app.folder), want="dir")
        self.assertTrue(all(e["dir"] for e in listing["entries"]))
        self.assertIn("found", [e["name"] for e in listing["entries"]])
        # A path that is gone opens the nearest folder that is not.
        listing = self.app.call("browse", path=str(self.app.found / "gone" / "deeper"))
        self.assertEqual(listing["path"], str(self.app.found))

    def test_serves_pdfs_from_content_folders_and_nothing_else(self):
        from urllib.parse import quote
        res, body = self.app.request("GET", "/file?path=" + quote(str(self.app.found / "Guide.pdf")))
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Content-Type"), "application/pdf")
        self.assertTrue(body.startswith(b"%PDF"))
        for path in (self.app.found / "notes.txt",
                     self.app.library / "museion-index.db",
                     self.app.found / ".." / "library" / "museion-index.db"):
            with self.subTest(path=path):
                res, _ = self.app.request("GET", "/file?path=" + quote(str(path)))
                self.assertEqual(res.status, 404)
        # Another site's page may not load one, even with an <img>.
        res, _ = self.app.request("GET", "/file?path=" + quote(str(self.app.found / "Guide.pdf")),
                                  headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status, 404)

    def test_names_on_another_computer_are_refused_without_looking_them_up(self):
        # On Windows, merely looking up \\host\share\x connects to that host
        # and offers it the person's login, so these never reach the disk.
        for name in ("\\\\host\\share\\x.pdf", "//host/share/x.pdf", "C:/Windows/win.ini", "C:x",
                     "../museion/app.py", "ui/../../cli.py"):
            with self.subTest(name=name):
                self.assertFalse(app.plain_relative(name))
        self.assertTrue(app.plain_relative("index.html"))
        self.assertTrue(app.plain_relative("img/logo.svg"))

    def test_a_share_named_in_a_request_is_never_looked_up(self):
        from unittest import mock
        from urllib.parse import quote
        looked_up = []

        def watch(real):
            def look(path, *a, **k):
                if os.fspath(path).replace("\\", "/").startswith("//host"):
                    looked_up.append(path)
                return real(path, *a, **k)
            return look
        with (mock.patch("os.stat", watch(os.stat)), mock.patch("os.lstat", watch(os.lstat)),
              mock.patch("os.path.realpath", watch(os.path.realpath))):
            for url in ("/file?path=" + quote("//host/share/x.pdf"),
                        "/file?path=" + quote("\\\\host\\share\\x.pdf"),
                        "/" + quote("\\\\host\\share\\x"),
                        "/%2F%2Fhost/share/x"):
                with self.subTest(url=url):
                    res, _ = self.app.request("GET", url)
                    self.assertEqual(res.status, 404)
        self.assertEqual(looked_up, [])

    def test_tuning_override_is_written_once_from_the_defaults(self):
        info = self.app.call("tuning_info")
        self.assertFalse(info["exists"])
        path = self.app.call("write_default_tuning")
        self.assertIn("system_prompt", Path(path).read_text(encoding="utf-8"))
        Path(path).write_text("answer_tokens = 100\n", encoding="utf-8")
        self.app.call("write_default_tuning")
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "answer_tokens = 100\n")


class LifetimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fakes = [FakeKiwix(), FakeLlama(), FakeLlama()]
        self.app = Running(Path(self.tmp.name), *self.fakes)

    def tearDown(self):
        self.app.close()
        for fake in self.fakes:
            fake.close()
        self.tmp.cleanup()

    def test_quit_stops_the_server(self):
        self.app.call("quit")
        self.app.thread.join(10)
        self.assertFalse(self.app.thread.is_alive())

    def test_stops_once_the_last_page_has_gone(self):
        grace, poll = app.IDLE_GRACE, app.IDLE_POLL
        app.IDLE_GRACE, app.IDLE_POLL = 0.3, 0.05
        try:
            threading.Thread(target=self.app.server.watch_idle, daemon=True).start()
            time.sleep(0.5)
            # Nothing stops before a page has ever connected.
            self.assertTrue(self.app.thread.is_alive())
            page = EventReader(self.app.port)
            time.sleep(0.5)
            self.assertTrue(self.app.thread.is_alive())
            page.close()
            # Noticed without anything being sent to the page.
            self.app.thread.join(5)
            self.assertFalse(self.app.thread.is_alive())
        finally:
            app.IDLE_GRACE, app.IDLE_POLL = grace, poll


@unittest.skipUnless(LLAMA_SERVER, "set MUSEION_LLAMA_SERVER to the llama-server program")
class RealChatModelTest(unittest.TestCase):
    """start_services starting the real llama-server with the flags the chat
    model always runs with, on the tiny model, then a question through it."""

    def test_starts_asks_and_stops_the_chat_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            fakes = [FakeKiwix(), FakeLlama(), FakeLlama()]
            running = Running(Path(tmp), *fakes)
            try:
                cfg = running.state.config()
                cfg.llama_exe = Path(LLAMA_SERVER)
                cfg.chat_model = tiny_gguf.write(Path(tmp) / "museion-tiny-test.gguf")
                cfg.chat_args = ["--ctx-size", "16384"]
                cfg.chat_gpu_layers = 0
                cfg.chat_port = sidecar.free_port()
                cfg.reranker_model = None
                running.state.save_config(cfg)
                # The tiny model writes until it is stopped; keep that short.
                running.state.tuning_path.write_text("answer_tokens = 32\n", encoding="utf-8")

                running.call("start_services")
                self.assertIsNotNone(running.state.supervisor.pid_for(sidecar.CHAT))
                answer = running.call("ask", question=QUESTION)
                # It cites nothing, so nothing it says passes as the library's.
                self.assertEqual(answer["basis"], "general_knowledge")
                self.assertTrue(answer["text"])

                # Only the chat model: stop_services would also stop the
                # adopted fake content server, by killing whatever listens on
                # its port -- this test.
                running.state.supervisor.stop(sidecar.CHAT)
                flags = running.call("service_state")
                self.assertFalse(flags["chat_running"])
                self.assertFalse(sidecar.port_open(cfg.chat_port))
            finally:
                running.close()
                for fake in fakes:
                    fake.close()


if __name__ == "__main__":
    unittest.main()
