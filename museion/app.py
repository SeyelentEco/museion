"""The Museion window: a page in the web browser, served by this program.

  python -m museion                       opens Museion in the default browser
  python -m museion app --no-browser      serves it without opening a browser

The page (museion/ui/) is plain HTML, CSS and JavaScript with no build step:
edit a file, reload the page, and the change is there. It talks to this
program over HTTP on 127.0.0.1:

  POST /api/<command>   runs one of COMMANDS below with the JSON body as its
                        arguments, and answers {"ok": <its result>}, or
                        {"error": "<a sentence for the person>"}.
  GET  /api/events      a stream of events (Server-Sent Events) -- answer
                        tokens, progress -- so the page can show work as it
                        happens. These are the events the Rust app's window
                        listened to, with the same names.
  GET  /file?path=...   a PDF from one of the extra content folders, so a
                        citation of one opens in the reader at its page.
  GET  anything else    the page's own files.

Only this computer can reach it, and only this page can use it: the server
listens on 127.0.0.1 alone, refuses a Host header that is not its own (which
stops a web page from reaching it through DNS rebinding), and refuses a
command without the X-Museion header, which another site's page cannot send
without this server's permission.

Closing the last Museion tab ends Museion a minute later, and stops the
helpers with it: a chat model left running holds nearly all of the GPU.
"""

from __future__ import annotations

import argparse
import inspect
import json
import queue
import select
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from museion import commands, config, library, services, sidecar
from museion.appstate import AppState, UiError

UI_DIR = Path(__file__).with_name("ui")
DEFAULT_PORT = 8070

# How long Museion waits after the last tab closes before it stops. A reload
# reconnects within a second or two; a laptop waking from sleep, a little
# longer.
IDLE_GRACE = 60
IDLE_POLL = 5

# Every command the page can call, by the name it calls it. The same names as
# the Rust app's commands, so the page is a straight port of that one.
COMMANDS = {f.__name__: f for f in [
    commands.get_hardware, commands.get_status, commands.get_config, commands.set_config,
    commands.kiwix_url, commands.ask, commands.list_models, commands.run_evals,
    commands.list_eval_runs, commands.eval_cases, commands.tuning_info,
    commands.write_default_tuning, commands.open_path, commands.browse,
    services.service_state, services.start_services, services.stop_services, services.fit_chat_model,
    library.setup_plan, library.fetch_tools, library.fetch_model, library.fetch_bundle,
    library.list_content, library.library_space, library.fetch_source, library.add_content_dir,
    library.remove_content_dir, library.build_index,
]}

# The page's kinds of file. Spelled out rather than asked of the mimetypes
# module, which on Windows reads the registry, and some machines' registries
# call .js text/plain -- a type browsers refuse to run as a module.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

# What a command can raise and still be the person's problem rather than
# Museion's: a missing file, a server that is not running, bad input. These
# are shown as they are worded. Anything else is a bug, and its traceback is
# printed in the console so it can be found.
EXPECTED_ERRORS = (UiError, OSError, RuntimeError, ValueError, sqlite3.Error)


class Events:
    """Sends events to every open page. Each page's stream has a queue; an
    event is put on all of them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: list[queue.Queue] = []
        self.ever_connected = False
        self.last_left = time.monotonic()

    def emit(self, name: str, payload: object) -> None:
        data = f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        with self._lock:
            for q in self._queues:
                q.put(data)

    def join(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._queues.append(q)
            self.ever_connected = True
        return q

    def leave(self, q: queue.Queue) -> None:
        with self._lock:
            self._queues.remove(q)
            self.last_left = time.monotonic()

    def pages(self) -> int:
        with self._lock:
            return len(self._queues)


class Server(ThreadingHTTPServer):
    # Request threads are not waited for at exit: an event stream never ends
    # by itself.
    daemon_threads = True

    def __init__(self, state: AppState, events: Events, port: int) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.state = state
        self.events = events
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.stopping = threading.Event()
        self._busy = 0
        self._busy_lock = threading.Lock()

    def busy(self, delta: int) -> None:
        with self._busy_lock:
            self._busy += delta

    def is_busy(self) -> bool:
        with self._busy_lock:
            return self._busy > 0

    def quit(self) -> None:
        """Stop the helpers Museion owns, then the server. Runs on its own
        thread: shutdown() waits for serve_forever() to return."""
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.state.supervisor.release_all()
        threading.Thread(target=self.shutdown, daemon=True).start()

    def watch_idle(self) -> None:
        """End Museion when no page has been open for IDLE_GRACE seconds and
        nothing is running. Nothing ends before the first page connects."""
        while not self.stopping.wait(IDLE_POLL):
            ev = self.events
            if ev.ever_connected and ev.pages() == 0 and not self.is_busy() \
                    and time.monotonic() - ev.last_left > IDLE_GRACE:
                print("No Museion page is open any more; stopping.")
                self.quit()


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format: str, *args) -> None:
        """Quiet: a line per request would bury what matters in the console."""

    def do_GET(self) -> None:
        if not self.host_ok():
            return self.send_json(403, {"error": "not this server's address"})
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/api/events":
            return self.stream_events()
        if url.path == "/api/hello":
            return self.send_json(200, {"app": "museion"})
        if url.path == "/file":
            return self.send_content_file(urllib.parse.parse_qs(url.query).get("path", [""])[0])
        self.send_ui_file(url.path)

    def do_POST(self) -> None:
        if not self.host_ok() or self.headers.get("X-Museion") != "1" or not self.origin_ok():
            return self.send_json(403, {"error": "refused: not from the Museion page"})
        name = urllib.parse.urlsplit(self.path).path.removeprefix("/api/")
        if name == "quit":
            self.send_json(200, {"ok": None})
            return self.server.quit()
        fn = COMMANDS.get(name)
        if fn is None:
            return self.send_json(404, {"error": f"no command {name!r}"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(length) or b"{}")
            inspect.signature(fn).bind(self.server.state, **args)
        except (ValueError, TypeError) as e:
            return self.send_json(400, {"error": f"{name}: {e}"})
        self.server.busy(+1)
        try:
            result = fn(self.server.state, **args)
        except EXPECTED_ERRORS as e:
            # An answer, not a failure of the request: the command ran and
            # has something to tell the person.
            return self.send_json(200, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 -- a bug is reported to the page, not hidden
            traceback.print_exc()
            return self.send_json(500, {"error": f"{type(e).__name__}: {e} (a bug; details in the console)"})
        finally:
            self.server.busy(-1)
        self.send_json(200, {"ok": result})

    # ── checks ──

    def host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.hosts

    def origin_ok(self) -> bool:
        """A browser names the page a request came from. No Origin at all
        is a program on this computer, such as a test, not a page."""
        origin = self.headers.get("Origin")
        return origin is None or urllib.parse.urlsplit(origin).netloc in self.server.hosts

    # ── answers ──

    def send_json(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = self.server.events.join()
        try:
            # Reconnect quickly after a reload or a dropped connection.
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            wrote = time.monotonic()
            while not self.server.stopping.is_set():
                try:
                    data = q.get(timeout=1)
                except queue.Empty:
                    # A closed tab is noticed within a second, so the idle
                    # timer starts when the person leaves, not when the next
                    # write happens to fail.
                    if self.hung_up():
                        break
                    if time.monotonic() - wrote < 15:
                        continue
                    # A comment line, so nothing in between drops a quiet
                    # connection as dead.
                    data = b": still here\n\n"
                self.wfile.write(data)
                self.wfile.flush()
                wrote = time.monotonic()
        except OSError:
            pass
        finally:
            self.server.events.leave(q)

    def hung_up(self) -> bool:
        """Has the page closed its end of the event stream? It sends nothing
        after asking for the stream, so anything to read is the end."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def send_ui_file(self, path: str) -> None:
        name = urllib.parse.unquote(path).lstrip("/") or "index.html"
        file = (UI_DIR / name).resolve()
        if not file.is_relative_to(UI_DIR.resolve()) or not file.is_file():
            return self.send_json(404, {"error": f"no such file: {path}"})
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        # Never cached: someone changing the page should see the change on
        # the next reload, not yesterday's copy.
        self.send_header("Cache-Control", "no-store")
        if file.suffix == ".html":
            # The reader frame shows kiwix-serve and the PDFs served below;
            # nothing else may be framed, and no other site may frame this.
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; frame-src 'self' http://127.0.0.1:*; "
                             "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def send_content_file(self, path: str) -> None:
        """A PDF from an extra content folder -- the only files served from
        outside museion/ui -- so it opens in the reader at the cited page."""
        file = Path(path)
        folders = [d.resolve() for d in self.server.state.config().extra_dirs]
        try:
            ok = file.suffix.lower() == ".pdf" and file.resolve().parent in folders and file.is_file()
        except OSError:
            ok = False
        if not ok:
            return self.send_json(404, {"error": "not a PDF in a content folder"})
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(file.stat().st_size))
        # Names the document in the browser's PDF viewer, which otherwise
        # calls it "file" after the address.
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(file.name)}")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        try:
            with file.open("rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
        except OSError:
            pass


def already_running(port: int) -> bool:
    """Is a Museion already serving on this port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=2) as res:
            return json.load(res).get("app") == "museion"
    except (OSError, ValueError, AttributeError):
        return False


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, stay: bool = False,
          config_dir: Path | None = None) -> None:
    """Run Museion until it is quit, its last page closes, or Ctrl+C."""
    if already_running(port):
        url = f"http://127.0.0.1:{port}/"
        print(f"Museion is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return
    if sidecar.port_open(port):
        port = sidecar.free_port()
    folder = config_dir or config.config_dir()
    events = Events()
    state = AppState(folder / "config.json", folder / "tuning.toml", events.emit)
    server = Server(state, events, port)
    url = f"http://127.0.0.1:{server.port}/"
    print(f"\nMuseion is at {url}\n"
          f"Settings: {state.config_path}\n"
          "Close this window or press Ctrl+C to stop it"
          f"{'' if stay else ', or close the last Museion tab'}.\n")
    if not stay:
        threading.Thread(target=server.watch_idle, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        state.supervisor.release_all()
        server.server_close()


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to serve the page on (default {DEFAULT_PORT})")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser; just print the address")
    p.add_argument("--stay", action="store_true", help="keep running after the last Museion tab closes")
    p.add_argument("--config-dir", type=Path, help="folder holding config.json (default: the Rust app's)")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="museion app", description="Open the Museion window in the browser.")
    add_arguments(p)
    args = p.parse_args(argv)
    serve(args.port, not args.no_browser, args.stay, args.config_dir)


if __name__ == "__main__":
    main(sys.argv[1:])
