"""A local web server for the download and install tests. It serves bytes by
path, honours Range requests unless told not to, can hang up part-way, and
records every request so a test can see what was asked for."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeServer:
    def __init__(self):
        self.files: dict[str, bytes] = {}         # path -> body
        self.ignore_range: set[str] = set()       # paths that answer 200 to a Range request
        self.cut: dict[str, int] = {}             # path -> bytes sent before hanging up
        self.requests: list[tuple[str, str | None]] = []  # (path, Range header)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server._serve(self)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _serve(self, h: BaseHTTPRequestHandler) -> None:
        wanted = h.headers.get("Range")
        self.requests.append((h.path, wanted))
        body = self.files.get(h.path)
        if body is None:
            h.send_error(404)
            return
        start = 0
        if wanted and h.path not in self.ignore_range:
            start = int(wanted.removeprefix("bytes=").split("-")[0])
            h.send_response(206)
            h.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            h.send_response(200)
        h.send_header("Content-Length", str(len(body) - start))
        h.end_headers()
        data = body[start:]
        if h.path in self.cut:
            data = data[:self.cut[h.path]]
        h.wfile.write(data)
