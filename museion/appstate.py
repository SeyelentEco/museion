"""What the Museion window remembers between one command and the next: the
configuration, the helper processes, and a few things too slow to work out
every time they are shown.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from museion import config, hardware, sidecar
from museion.config import Config
from museion.index import Index


class UiError(Exception):
    """A problem the person can act on, shown to them exactly as worded."""


class AppState:
    def __init__(self, config_path: Path, tuning_path: Path,
                 emit: Callable[[str, object], None] = lambda name, payload: None) -> None:
        self.config_path = config_path
        # tuning.toml beside config.json: prompts and limits, read on every
        # question so an edit needs no restart.
        self.tuning_path = tuning_path
        # Sends an event to every open page: emit("service-progress", "Ready.").
        self.emit = emit
        self.supervisor = sidecar.Supervisor()
        self._lock = threading.Lock()
        self._config = config.load(config_path)
        self._hardware: hardware.Hardware | None = None
        self._stats: tuple[float, tuple[int, int, int]] | None = None

    def config(self) -> Config:
        """A copy of the configuration, safe to change and then save."""
        with self._lock:
            return self._config.copy()

    def save_config(self, cfg: Config) -> None:
        cfg.save(self.config_path)
        with self._lock:
            self._config = cfg.copy()

    def hardware(self) -> hardware.Hardware:
        """Detected once. Detection shells out to nvidia-smi, and doing that
        every time the Settings tab opened is what made the Rust app's page
        scroll like it was wading."""
        if self._hardware is None:
            self._hardware = hardware.detect()
        return self._hardware

    def index_stats(self, cfg: Config) -> tuple[int, int, int]:
        """(documents, pages, words), worked out at most every 30 seconds or
        when told to forget. Opening SQLite and summing on every refresh for
        three numbers was the other half of that jank."""
        with self._lock:
            if self._stats and time.monotonic() - self._stats[0] < 30:
                return self._stats[1]
        value = (0, 0, 0)
        path = cfg.index_path()
        if path and path.exists():
            try:
                with closing(Index(path)) as index:
                    s = index.stats()
                value = (s.documents, s.pages, s.words)
            except Exception:  # noqa: BLE001 -- a broken index shows as empty, not as a failed page
                pass
        with self._lock:
            self._stats = (time.monotonic(), value)
        return value

    def forget_stats(self) -> None:
        with self._lock:
            self._stats = None


def local(port: int) -> str:
    """The address of a helper on this machine."""
    return f"http://127.0.0.1:{port}"


def file_name(path: Path) -> str:
    return path.name or str(path)
