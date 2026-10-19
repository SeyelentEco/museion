"""A stand-in for kiwix-serve, for tests that should not need archives.

It serves whatever entries a test puts in `FakeKiwix.entries`, keyed by the
decoded path after /content/ ("<book>/<path>"), answers the catalogue so the
server reads as up, and answers /search with `FakeKiwix.search_xml`. Anything
else is a 404, as kiwix-serve gives for an entry an archive does not have.
Every request path is kept, still percent-encoded, in `FakeKiwix.requests`.
"""

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeKiwix:
    def __init__(self) -> None:
        self.entries: dict[str, bytes] = {}
        self.search_xml = ""
        self.requests: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                path = urllib.parse.urlsplit(self.path).path
                if path == "/catalog/v2/entries":
                    self._send(200, b"<feed></feed>", "application/atom+xml")
                elif path == "/search":
                    self._send(200, fake.search_xml.encode("utf-8"), "application/rss+xml")
                elif path.startswith("/content/"):
                    entry = fake.entries.get(urllib.parse.unquote(path[len("/content/"):]))
                    if entry is None:
                        self._send(404, b"Not found", "text/plain")
                    else:
                        self._send(200, entry, "application/octet-stream")
                else:
                    self._send(404, b"Not found", "text/plain")

            def _send(self, status, data, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

