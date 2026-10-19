"""museion doctor -- check that everything Museion depends on works here.

Each check prints one line: "ok", "warn" or "FAIL", and what it found. The
exit code is 0 when nothing failed. Run it after moving the drive to a new
computer, after changing the code, or whenever something seems wrong:

    python -m museion.doctor --pdfium bin --llama-server bin/llama-server

With no --model, llama-server is tested with a tiny built-in model, which
proves the plumbing (start, answer, stop, and die with Museion) in seconds.
Give --model to also prove a real chat model loads on this machine, and
--reranker to prove the reranker discriminates.
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from museion import llama, sidecar, tiny_gguf
from museion.pdf import PdfExtractor, sample_pdf

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# The reranker's own server arguments, as models/qwen3-reranker-0.6b.yaml
# gives them: --reranking exposes /v1/rerank, and the large batch is what
# stops long technical passages being rejected as too many tokens.
RERANKER_ARGS = ["--reranking", "--pooling", "rank", "--batch-size", "2048", "--ubatch-size", "2048"]

# A modest context for the check. Left unset, llama-server sizes the context
# to the model's training length -- 256k tokens for some -- and the memory
# for that can fail to allocate on a machine that runs Museion fine.
CHECK_ARGS = ["--ctx-size", "2048", "--parallel", "1"]


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, what: str) -> None:
        print(f"ok    {what}", flush=True)

    def warn(self, what: str) -> None:
        print(f"warn  {what}", flush=True)

    def fail(self, what: str) -> None:
        self.failed += 1
        print(f"FAIL  {what}", flush=True)


def check_python(r: Report) -> None:
    v = sys.version_info
    if v >= (3, 11):
        r.ok(f"Python {v.major}.{v.minor}.{v.micro} at {sys.executable}")
    else:
        r.fail(f"Python {v.major}.{v.minor} is too old; Museion needs 3.11 or newer (for tomllib)")


def check_fts5(r: Report) -> None:
    """The local index is SQLite FTS5. A Python built without it cannot build
    or search the index at all."""
    try:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        db.executemany("INSERT INTO t(body) VALUES (?)", [("boil the water",), ("tie a bowline",)])
        row = db.execute("SELECT body FROM t WHERE t MATCH 'bowline' ORDER BY bm25(t)").fetchone()
        assert row == ("tie a bowline",), row
        r.ok(f"SQLite {sqlite3.sqlite_version} with FTS5")
    except (sqlite3.Error, AssertionError) as e:
        r.fail(f"SQLite FTS5 is not usable: {e}")


def check_pdfium(r: Report, lib_dir: Path, pdf: Path | None) -> None:
    try:
        extractor = PdfExtractor(lib_dir)
        got = extractor.extract(sample_pdf(["purification of water"]))
        assert got.pages and got.pages[0].text == "purification of water", got
        r.ok(f"pdfium extracts text ({lib_dir})")
    except (OSError, RuntimeError, ValueError, AssertionError) as e:
        r.fail(f"pdfium: {e}")
        return
    if pdf is not None:
        try:
            got = extractor.extract(pdf.read_bytes())
            note = " -- looks image-only, needs OCR" if got.is_image_only() else ""
            r.ok(f"{pdf.name}: {got.page_count} pages, {got.word_count} words{note}")
        except (OSError, ValueError) as e:
            r.fail(f"{pdf.name}: {e}")


def check_llama(r: Report, exe: Path, model: Path, label: str, gpu_layers: int = 0) -> None:
    """Start the chat server, ask it something, stop it, and make sure it is
    really gone: a server left behind keeps the whole GPU."""
    sup = sidecar.Supervisor()
    port = sidecar.free_port()
    started = time.monotonic()
    try:
        sup.start_llama(sidecar.CHAT, exe, model, port, gpu_layers=gpu_layers, extra=CHECK_ARGS)
        sup.wait_ready(sidecar.CHAT, f"http://127.0.0.1:{port}/health", timeout=600)
        loaded = time.monotonic() - started
        text = llama.Llama(f"http://127.0.0.1:{port}").chat([llama.user("Reply with one word: ready.")], max_tokens=8)
        r.ok(f"llama-server loaded the {label} model in {loaded:.1f}s and answered {text.strip()[:40]!r}")
    except (OSError, RuntimeError, ConnectionError) as e:
        r.fail(f"llama-server with the {label} model: {e}")
    finally:
        sup.stop_all()
    if sidecar.port_open(port):
        r.fail(f"llama-server is still listening on port {port} after Stop")
    else:
        r.ok("llama-server stopped cleanly")


def check_reranker(r: Report, exe: Path, model: Path) -> None:
    sup = sidecar.Supervisor()
    port = sidecar.free_port()
    try:
        sup.start_llama(sidecar.RERANKER, exe, model, port, gpu_layers=0, extra=RERANKER_ARGS + CHECK_ARGS)
        sup.wait_ready(sidecar.RERANKER, f"http://127.0.0.1:{port}/health", timeout=300)
        docs = ["Tie a bowline by forming a small loop.", "Boil water for one minute to make it safe to drink."]
        scored = llama.Llama(f"http://127.0.0.1:{port}").rerank("how do I make water safe to drink?", docs)
        if scored[0][0] == 1:
            r.ok(f"reranker puts the relevant passage first ({scored[0][1]:.3f} vs {scored[1][1]:.3f})")
        else:
            r.fail(f"reranker ranked the irrelevant passage first: {scored}")
    except (OSError, RuntimeError, ConnectionError) as e:
        r.fail(f"reranker: {e}")
    finally:
        sup.stop_all()


def check_dies_with_museion(r: Report, exe: Path, model: Path) -> None:
    """Start llama-server from a separate Museion process, kill that process
    the hard way (as a crash or Task Manager would), and see whether the
    server goes with it."""
    port = sidecar.free_port()
    child = subprocess.Popen(
        [sys.executable, "-m", "museion.doctor", "--orphan-child", str(exe), str(model), str(port)],
        cwd=PACKAGE_ROOT, stdout=subprocess.PIPE, text=True,
    )
    line = child.stdout.readline().strip()  # the server's pid, once it is ready
    child.stdout.close()
    if not line.isdigit():
        child.kill()
        r.fail(f"could not start llama-server for the crash test: {line or 'no output'}")
        return
    server_pid = int(line)
    child.kill()
    child.wait()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and sidecar.port_open(port):
        time.sleep(0.25)
    if not sidecar.port_open(port):
        r.ok("llama-server dies when Museion is killed")
        return
    _kill_pid(server_pid)
    if sys.platform == "darwin":
        r.warn("llama-server outlives a crashed Museion on macOS (no OS mechanism; a normal quit still stops it)")
    else:
        r.fail("llama-server outlived a crashed Museion and would keep holding the GPU")


def orphan_child(exe: Path, model: Path, port: int) -> None:
    """The process check_dies_with_museion kills. Starts the server, prints
    its pid when ready, then waits to be killed."""
    sup = sidecar.Supervisor()
    sup.start_llama(sidecar.CHAT, exe, model, port, gpu_layers=0, extra=CHECK_ARGS)
    sup.wait_ready(sidecar.CHAT, f"http://127.0.0.1:{port}/health", timeout=600)
    print(sup.pid_for(sidecar.CHAT), flush=True)
    while True:
        time.sleep(60)


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="museion doctor", description=__doc__.split("\n\n")[0])
    p.add_argument("--pdfium", type=Path, help="folder holding the pdfium library")
    p.add_argument("--pdf", type=Path, help="a PDF to try extracting, e.g. one from the library")
    p.add_argument("--llama-server", type=Path, help="path to the llama-server program")
    p.add_argument("--model", type=Path, help="a chat model GGUF to test (default: a tiny built-in one)")
    p.add_argument("--gpu-layers", type=int, default=0,
                   help="layers of --model to put on the GPU (default 0: CPU only, slow but always fits)")
    p.add_argument("--reranker", type=Path, help="the reranker GGUF to test")
    p.add_argument("--orphan-child", nargs=3, help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    if args.orphan_child:
        exe, model, port = args.orphan_child
        orphan_child(Path(exe), Path(model), int(port))
        return 0

    r = Report()
    check_python(r)
    check_fts5(r)
    if args.pdfium:
        check_pdfium(r, args.pdfium, args.pdf)
    else:
        r.warn("pdfium not checked (give --pdfium)")

    if args.llama_server:
        with tempfile.TemporaryDirectory() as tmp:
            tiny = tiny_gguf.write(Path(tmp) / "museion-tiny-test.gguf")
            check_llama(r, args.llama_server, tiny, "tiny")
            check_dies_with_museion(r, args.llama_server, tiny)
            if args.model:
                check_llama(r, args.llama_server, args.model, args.model.name, args.gpu_layers)
            if args.reranker:
                check_reranker(r, args.llama_server, args.reranker)
    else:
        r.warn("llama-server not checked (give --llama-server)")

    print("all checks passed" if r.failed == 0 else f"{r.failed} check(s) failed")
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
