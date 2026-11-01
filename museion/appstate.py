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

from museion import config, hardware, portable, sidecar
from museion.config import Config
from museion.index import Index
from museion.portable import MachineProfile, Portable


class UiError(Exception):
    """A problem the person can act on, shown to them exactly as worded."""


class AppState:
    def __init__(self, config_path: Path, tuning_path: Path,
                 emit: Callable[[str, object], None] = lambda name, payload: None,
                 drive: Portable | None = None) -> None:
        self.config_path = config_path
        # tuning.toml beside config.json: prompts and limits, read on every
        # question so an edit needs no restart.
        self.tuning_path = tuning_path
        # Sends an event to every open page: emit("service-progress", "Ready.").
        self.emit = emit
        self.supervisor = sidecar.Supervisor()
        # Set when running from a drive (see portable.py).
        self.drive = drive
        self._lock = threading.Lock()
        self._hardware: hardware.Hardware | None = None
        self._stats: tuple[float, tuple[int, int, int]] | None = None
        cfg = config.load(config_path)
        if drive:
            cfg = cfg.resolved(drive.root)
            # This computer's build, model and split, laid over the drive's
            # config; worked out now if it has never seen this drive before.
            profile = MachineProfile.load(drive.profile_path())
            if profile:
                if profile.apply(cfg, drive.root):
                    try:
                        profile.save(drive.profile_path())
                    except OSError as e:
                        print(f"  (machine profile not updated: {e})")
            else:
                profile = portable.adapt(cfg, self.hardware(), drive.root)
                try:
                    profile.save(drive.profile_path())
                except OSError as e:
                    print(f"  (machine profile not written: {e})")
        self._config = cfg

    def config(self) -> Config:
        """A copy of the configuration, safe to change and then save."""
        with self._lock:
            return self._config.copy()

    def save_config(self, cfg: Config) -> None:
        """Write the config where it lives. From a drive, the machine-specific
        fields go to this computer's profile as well, and a drive that cannot
        be written to is not an error: the profile holds what matters."""
        if self.drive:
            cfg.base = self.drive.root
            path = self.drive.profile_path()
            profile = MachineProfile.load(path) or MachineProfile()
            profile.absorb(cfg)
            profile.save(path)
            # The drive keeps its own build, model and split: what this
            # computer chose was written there too once, and the next
            # computer started from this one's choices.
            shared = cfg.copy()
            own = config.load(self.config_path).resolved(self.drive.root)
            for name in portable.MACHINE_FIELDS:
                setattr(shared, name, getattr(own, name))
            try:
                shared.save(self.config_path)
            except OSError as e:
                print(f"  (drive config not written: {e})")
        else:
            cfg.save(self.config_path)
        with self._lock:
            self._config = cfg.copy()

    def fit_due(self) -> bool:
        """From a drive on a computer whose GPU split has not been measured
        yet: is a fit due before the chat model starts?"""
        if not self.drive:
            return False
        profile = MachineProfile.load(self.drive.profile_path())
        return not (profile and profile.fitted)

    def mark_fitted(self) -> None:
        """Record that the split has been measured on this computer."""
        if self.drive:
            path = self.drive.profile_path()
            profile = MachineProfile.load(path) or MachineProfile()
            profile.fitted = True
            try:
                profile.save(path)
            except OSError:
                pass

    def evals_dir(self, cfg: Config) -> Path | None:
        """Where benchmark runs are kept: the library's evals folder, or on a
        write-protected drive a folder on this computer."""
        if not cfg.library_dir:
            return None
        wanted = cfg.library_dir / "evals"
        return self.drive.writable_or_cache(wanted) if self.drive else wanted

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


def open_state(config_dir: Path | None = None,
               emit: Callable[[str, object], None] = lambda name, payload: None) -> AppState:
    """The state for this run: from the drive Museion is on, if it is on one,
    else from `config_dir` or the computer's own settings folder."""
    drive = None if config_dir else portable.detect()
    if drive:
        return AppState(drive.config_path(), drive.tuning_path(), emit, drive)
    folder = config_dir or config.config_dir()
    return AppState(folder / "config.json", folder / "tuning.toml", emit)


def local(port: int) -> str:
    """The address of a helper on this machine."""
    return f"http://127.0.0.1:{port}"


def file_name(path: Path) -> str:
    return path.name or str(path)
