"""A stand-in for llama-server, for tests that should not need a model.

It answers /v1/chat/completions with a scripted stream of server-sent events
and /v1/rerank with scripted scores, the same shapes the real server sends.
Each test sets `FakeLlama.chunks` or `FakeLlama.rerank_scores` before calling.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLlama:
    def __init__(self) -> None:
        self.chunks: list[dict] = []
        self.rerank_scores: list[float] = []
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/v1/models":
                    self._json({"data": [{"id": "fake-model.gguf"}]})
                else:
                    self._json({"status": "ok"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if self.path == "/v1/rerank":
                    results = [{"index": i, "relevance_score": s} for i, s in enumerate(fake.rerank_scores)]
                    self._json({"results": results})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in fake.chunks:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def _json(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def text(content: str, finish: str | None = None) -> dict:
    return {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}


def call_fragment(index: int, id: str = "", name: str = "", arguments: str = "") -> dict:
    function = {}
    if name:
        function["name"] = name
    if arguments:
        function["arguments"] = arguments
    return {"choices": [{"delta": {"tool_calls": [{"index": index, "id": id, "function": function}]}, "finish_reason": None}]}
