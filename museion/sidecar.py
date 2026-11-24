"""Supervising the helper processes: kiwix-serve for content, llama-server for
the chat model and again for the reranker.

Shutdown is the part that matters. A loaded chat model holds essentially all
of the GPU's memory for as long as it lives -- 11.5 GB of 12 on the machine
this was developed against -- so a helper that outlives the app does not
merely leak a process, it makes the GPU unusable for everything else until
someone notices and kills it by hand.

The classic way to get this wrong is to launch the server through a wrapper
script and then stop the script: the server is a *grandchild*, survives, and
keeps every byte. Museion therefore executes the server binary directly, so
the handle it holds is the process that actually owns the memory.

Stopping on a graceful quit is not enough either, because a crash or a kill
from Task Manager skips every exit hook. So each helper is also tied to
Museion's life by the operating system where it offers a way:

  * Windows: a Job Object with kill-on-close (winjob.py). When Museion's
    process ends, for any reason, Windows ends everything in the job.
  * Linux: prctl(PR_SET_PDEATHSIG), which kills the helper when the thread
    that started it dies. Helpers are therefore always started from one
    long-lived thread (see Supervisor._spawner).
  * macOS has no equivalent. A graceful quit still stops the helpers; a
    crash leaves them running.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from museion import winjob

# The three kinds of helper, and what to call each in a message to a person.
KIWIX = "kiwix"
CHAT = "chat"
RERANKER = "reranker"
LABELS = {KIWIX: "content server", CHAT: "chat model", RERANKER: "reranker"}

# Stops a console window flashing up on Windows every time a helper starts.
CREATE_NO_WINDOW = 0x0800_0000


@dataclass
class Sidecar:
    role: str
    port: int
    # The process, when Museion started it. None for a server that was
    # already running on the port and was adopted.
    process: subprocess.Popen | None
    # Whether it dies with the app. False for adopted servers -- they were
    # someone else's to begin with -- and for servers started under
    # MUSEION_KEEP_SERVICES, which a developer wants to outlive one run of
    # the app so the next run adopts them instead of reloading a 22 GB model.
    owned: bool
    # The command line it was started with; empty for an adopted server.
    args: list[str] = field(default_factory=list)

    def is_alive(self) -> bool:
        """A model that failed to load exits rather than hanging, and the
        difference between "still loading" and "died" is the difference
        between waiting and reporting an error. An adopted server has no
        process handle; its port is the only witness."""
        if self.process is None:
            return port_open(self.port)
        return self.process.poll() is None

    def kill(self) -> None:
        """Stop it, whatever way is available: the handle if there is one,
        otherwise whoever is listening on the port."""
        if self.process is None:
            kill_listener(self.port)
            return
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def keep_services() -> bool:
    """Set MUSEION_KEEP_SERVICES=1 to leave the servers Museion starts running
    after it exits, so the next launch adopts them. A developer's switch; it
    is never set for users."""
    return os.environ.get("MUSEION_KEEP_SERVICES", "").lower() in ("1", "true")


# How long a port may stay taken after its helper was stopped before starting
# another helper there is refused.
PORT_RELEASE_SECONDS = 5.0


def kiwix_args(exe: Path, dirs: list[Path], port: int) -> list[str]:
    """kiwix-serve's command line, over every archive in the given folders."""
    zims = sorted(p for d in dirs if d.is_dir() for p in d.iterdir() if p.suffix == ".zim")
    if not zims:
        shown = ", ".join(str(d) for d in dirs)
        raise RuntimeError(f"no .zim archives in {shown}. Fetch some content first.")
    return [str(exe), "--port", str(port), *map(str, zims)]


def port_open(port: int) -> bool:
    """Is anything accepting connections on the port? A quick TCP connect; no
    request is made, so a server mid-load still counts as present."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def free_port() -> int:
    """A port nothing is listening on right now, chosen by the OS."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_ok(url: str, timeout: float = 3.0) -> str | None:
    """The body of a GET that answered 200, or None. llama-server answers
    /health with 503 while a model is still loading, which counts as not
    ready."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return res.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def kill_listener(port: int) -> None:
    """Kill whatever process is listening on a local TCP port. Used only for
    adopted servers, where there is no process handle."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        ).stdout
        for line in out.splitlines():
            if "LISTENING" in line and f":{port} " in line:
                pid = line.split()[-1]
                subprocess.run(
                    ["taskkill", "/PID", pid, "/F"],
                    capture_output=True, creationflags=CREATE_NO_WINDOW,
                )
    else:
        try:
            # The server listening, only: "-ti :PORT" also listed every
            # program connected to it, and the browser showing an article
            # from kiwix-serve was sent SIGTERM along with it.
            out = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout
        except FileNotFoundError:
            return
        for pid in out.split():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                pass


class Supervisor:
    """Starts, watches and stops the helpers. Safe to call from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: list[Sidecar] = []
        # Every helper is started from this one thread, which lives as long
        # as Museion. On Linux a helper is killed when the *thread* that
        # started it exits, not the process; started from a web request's
        # thread, llama-server would die the moment the request finished.
        self._spawner = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="museion-spawner"
        )

    def is_running(self, role: str) -> bool:
        self._prune_dead()
        with self._lock:
            return any(s.role == role for s in self._running)

    def started_with(self, role: str) -> list[str] | None:
        """The command line Museion started a helper with, or None when it is
        not running or was adopted."""
        with self._lock:
            return next((list(s.args) for s in self._running if s.role == role and s.args), None)

    def port_for(self, role: str) -> int | None:
        with self._lock:
            return next((s.port for s in self._running if s.role == role), None)

    def pid_for(self, role: str) -> int | None:
        """The process id of a helper Museion started, or None."""
        with self._lock:
            return next((s.process.pid for s in self._running if s.role == role and s.process), None)

    def _prune_dead(self) -> None:
        """Forget servers that are no longer there. Adopted at the wrong
        moment -- a previous instance's servers still shutting down -- a
        server was once listed as running for as long as the app lived, with
        nothing on the port."""
        with self._lock:
            self._running = [s for s in self._running if s.is_alive()]

    def adopt(self, role: str, port: int, health_url: str, expect_model: Path | None = None) -> bool:
        """If a healthy server of this kind is already answering on the port,
        take it as ours to use -- not ours to kill -- and say so. For a
        llama-server, "of this kind" means it reports the expected model
        file; a chat model on the reranker's port, or last week's model, is
        not adopted. This is what makes a restart of the app cost seconds
        instead of a model reload, when the servers were left up."""
        if self.is_running(role):
            if expect_model is None or self.started_with(role) is not None:
                return True
            # Adopted earlier: still the model wanted? If Settings chose
            # another since, this one goes, rather than "Ready." with the old
            # model loaded.
            body = http_ok(health_url)
            if body is not None and expect_model.name.lower() in body.lower():
                return True
            self.stop(role)
        body = http_ok(health_url)
        if body is None:
            return False
        if expect_model is not None:
            want = expect_model.name.lower()
            if not want or want not in body.lower():
                return False
        with self._lock:
            self._running.append(Sidecar(role, port, None, owned=False))
        return True

    def start_kiwix(self, exe: Path, dirs: list[Path], port: int) -> None:
        """Start kiwix-serve over every archive in the given folders."""
        if self.is_running(KIWIX):
            return
        self.start(KIWIX, kiwix_args(exe, dirs, port), port)

    def start_llama(
        self, role: str, exe: Path, model: Path, port: int,
        gpu_layers: int = 0, extra: list[str] | None = None,
    ) -> None:
        """Start a llama-server instance.

        gpu_layers of 0 keeps a model on the CPU, which is how the reranker
        is run: a 0.6B cross-encoder costs a couple of seconds there, and
        that is a far better trade than taking VRAM away from the model
        writing the answer."""
        if self.is_running(role):
            return
        if not model.is_file():
            raise FileNotFoundError(f"model not found: {model}")
        self.start(role, llama_args(exe, model, port, gpu_layers, extra or []), port)

    def start(self, role: str, args: list[str], port: int) -> None:
        """Start any helper from a full command line and supervise it."""
        # Another program on the port would answer the health check in the
        # moment before this helper fails to bind and exits: "Ready." with
        # someone else's server, or a wrong guess about memory. A helper just
        # stopped may hold the port for a moment longer, so wait for that.
        deadline = time.monotonic() + PORT_RELEASE_SECONDS
        while port_open(port):
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"port {port} is in use by another program, so {LABELS.get(role, role)} cannot "
                    f"start there. Close that program, or give Museion another port in config.json."
                )
            time.sleep(0.25)
        owned = not keep_services()
        try:
            process = self._spawner.submit(_spawn, args, owned).result()
        except OSError as e:
            raise RuntimeError(f"starting {LABELS.get(role, role)} ({args[0]}): {e}") from e
        with self._lock:
            self._running.append(Sidecar(role, port, process, owned, list(args)))

    def wait_ready(self, role: str, health_url: str, timeout: float) -> None:
        """Wait until the service answers, or until it gives up.

        A large mixture-of-experts model can take minutes to load -- 200
        seconds for a 20 GB one from a spinning disk -- so callers pass a
        generous timeout. The process is checked as well as the port, so a
        model that fails to load is reported at once instead of waiting out
        the full timeout."""
        label = LABELS.get(role, role)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                sidecar = next((s for s in self._running if s.role == role), None)
            if sidecar is None:
                raise RuntimeError(f"{label} is not running")
            if not sidecar.is_alive():
                raise RuntimeError(
                    f"{label} exited while starting up. The usual causes are a model "
                    "too large for the available memory, or a missing GPU runtime."
                )
            if http_ok(health_url, timeout=2.0) is not None:
                return
            time.sleep(0.75)
        raise RuntimeError(f"{label} did not become ready within {int(timeout)}s")

    def stop(self, role: str) -> None:
        """Stop one service outright, adopted or not: a person pressing Stop,
        or a benchmark swapping the chat model, means it."""
        with self._lock:
            mine = [s for s in self._running if s.role == role]
            self._running = [s for s in self._running if s.role != role]
        for s in mine:
            s.kill()

    def stop_all(self) -> None:
        """Stop everything, adopted or not. The Stop button."""
        with self._lock:
            everything, self._running = self._running, []
        for s in everything:
            s.kill()

    def release_all(self) -> None:
        """The app is exiting: stop what is ours to stop. Adopted servers and
        servers started under MUSEION_KEEP_SERVICES are left running. Called
        when the window closes and again on exit, since leaving a chat model
        resident is the expensive mistake -- for servers Museion owns."""
        with self._lock:
            everything, self._running = self._running, []
        for s in everything:
            if s.owned:
                s.kill()


def llama_args(exe: Path, model: Path, port: int, gpu_layers: int, extra: list[str]) -> list[str]:
    """The command line a llama-server is started with."""
    return [str(exe), "--model", str(model), "--host", "127.0.0.1",
            "--port", str(port), "--n-gpu-layers", str(gpu_layers), *extra]


def _spawn(args: list[str], owned: bool) -> subprocess.Popen:
    """Start one helper with its output discarded, tied to Museion's life
    when it is ours."""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    elif sys.platform.startswith("linux") and owned:
        parent = os.getpid()
        kwargs["preexec_fn"] = lambda: _die_with_parent(parent)
    process = subprocess.Popen(args, **kwargs)
    if sys.platform == "win32" and owned:
        winjob.assign(process.pid)
    return process


# ── Linux: die with the parent ───────────────────────────────────────────────

_PR_SET_PDEATHSIG = 1
# Loaded here, in the parent, because loading a library in the child between
# fork and exec can deadlock when other threads held the loader's lock.
_libc = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None


def _die_with_parent(parent: int) -> None:
    """Runs in the child between fork and exec: ask the kernel to SIGKILL it
    when the thread that started it dies. If the parent already died in the
    instant before the request was made, nobody will send the signal, so the
    child exits by itself instead."""
    _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    if os.getppid() != parent:
        os._exit(1)
