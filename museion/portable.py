"""Running from a drive.

A file named `museion-portable` in the folder above the app makes that folder
the portable root: config, library, models and helper programs are found
relative to it, so the drive can come up as E: on one computer and H: on the
next and nothing is lost. What belongs to the *computer* rather than the
drive -- which llama.cpp build to run, which model tier fits, the GPU split
the fit measured -- lives in a machine profile under the computer's local
app data, keyed by the id in the marker, and is laid over the drive's config
at launch. The drive itself is never written for that.

A drive's layout (prepare.py makes it):

  <root>/
    museion-portable   the marker; holds the drive's id
    config.json        paths relative to <root>
    tuning.toml        optional prompts-and-limits override
    Museion.cmd        starts Museion (Museion.command on macOS, museion.sh on Linux)
    app/               this program: museion/, catalog/, tests/, AGENTS.md ...
    python/            the portable Python that runs it
    library/ models/   content and .gguf files
    bin/               kiwix-serve, pdfium, opencode
    bin/llama/<kind>/  a llama.cpp build per kind of graphics card

The Rust app put its Museion.exe at <root> beside the same marker, and the
profile is the same file in the same place, so either app runs a drive the
other prepared.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from museion import catalog, config, hardware
from museion.config import Config

MARKER = "museion-portable"

# The folder holding the museion package: <root>/app on a drive.
APP_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Portable:
    root: Path
    # From the marker; names the profile's folder.
    id: str
    # Per-computer, per-drive state: the profile, and anything Museion
    # writes for itself when the drive is read-only.
    cache_dir: Path

    def config_path(self) -> Path:
        return self.root / "config.json"

    def tuning_path(self) -> Path:
        return self.root / "tuning.toml"

    def profile_path(self) -> Path:
        return self.cache_dir / "machine.json"

    def writable_or_cache(self, wanted: Path) -> Path:
        """`wanted` if it can be created and written to, else a folder of the
        same name in the cache. A write-protected drive still answers
        questions; what Museion writes for itself goes somewhere it can."""
        try:
            wanted.mkdir(parents=True, exist_ok=True)
            if is_writable(wanted):
                return wanted
        except OSError:
            pass
        alt = self.cache_dir / (wanted.name or "data")
        alt.mkdir(parents=True, exist_ok=True)
        return alt


def detect(app_dir: Path = APP_DIR, local_data: Path | None = None) -> Portable | None:
    """Portable if the marker sits in the folder above the app."""
    root = app_dir.parent
    marker = root / MARKER
    if not marker.is_file():
        return None
    try:
        raw = marker.read_text(encoding="utf-8-sig")
    except OSError:
        raw = ""
    # The id names a folder, so only characters safe in one.
    drive_id = "".join(c for c in raw.strip() if c.isascii() and (c.isalnum() or c in "-_"))[:64] or "drive"
    cache_dir = (local_data or local_data_dir()) / "portable" / drive_id
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return Portable(root, drive_id, cache_dir)


def local_data_dir() -> Path:
    """The computer's own folder for Museion's per-machine state: the one the
    Rust app used (Tauri's app_local_data_dir)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / config.APP_ID


def is_writable(folder: Path) -> bool:
    probe = folder / ".museion-write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


@dataclass
class MachineProfile:
    """What this computer runs from the drive, and how. The same JSON the
    Rust app wrote."""

    # For a person reading the file: which computer this was measured on.
    machine: str = ""
    llama_exe: Path | None = None
    chat_model: Path | None = None
    chat_args: list[str] = field(default_factory=list)
    chat_gpu_layers: int = 0
    # Whether llama-fit-params has measured the split on this computer.
    fitted: bool = False

    @classmethod
    def load(cls, path: Path) -> MachineProfile | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        p = cls()
        p.machine = str(data.get("machine") or "")
        for name in ("llama_exe", "chat_model"):
            if data.get(name):
                setattr(p, name, Path(data[name]))
        p.chat_args = [str(a) for a in data.get("chat_args") or []]
        p.chat_gpu_layers = int(data.get("chat_gpu_layers") or 0)
        p.fitted = bool(data.get("fitted"))
        return p

    def save(self, path: Path) -> None:
        out = asdict(self)
        for name in ("llama_exe", "chat_model"):
            out[name] = str(out[name]) if out[name] is not None else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    def absorb(self, cfg: Config) -> None:
        """Take the machine-specific fields from a config."""
        self.llama_exe = cfg.llama_exe
        self.chat_model = cfg.chat_model
        self.chat_args = list(cfg.chat_args)
        self.chat_gpu_layers = cfg.chat_gpu_layers

    def apply(self, cfg: Config) -> None:
        """Lay the machine-specific fields over a config. A path that no
        longer exists -- the build was deleted from the drive -- is left to
        the drive's config."""
        if self.llama_exe and self.llama_exe.exists():
            cfg.llama_exe = self.llama_exe
        if self.chat_model and self.chat_model.exists():
            cfg.chat_model = self.chat_model
            cfg.chat_args = list(self.chat_args)
            cfg.chat_gpu_layers = self.chat_gpu_layers


def adapt(cfg: Config, hw: hardware.Hardware, root: Path) -> MachineProfile:
    """The first launch on a computer: fill in what the drive carries and
    choose the build and the model for this hardware. Returns the profile to
    save; `fitted` is False when a GPU is present and the split still needs
    measuring."""
    bin_dir = root / "bin"

    def missing(p: Path | None) -> bool:
        return p is None or not p.exists()

    if missing(cfg.library_dir) and (root / "library").is_dir():
        cfg.library_dir = root / "library"
    if missing(cfg.models_dir) and (root / "models").is_dir():
        cfg.models_dir = root / "models"
    if missing(cfg.kiwix_exe):
        cfg.kiwix_exe = find_exe(bin_dir, "kiwix-serve")
    if missing(cfg.pdfium_dir) and bin_dir.is_dir():
        cfg.pdfium_dir = bin_dir

    # The llama.cpp build: the drive may carry one per accelerator under
    # bin/llama/<name>, or a single flattened build in bin.
    order = {
        hardware.CUDA: ["cuda", "vulkan", "cpu"],
        hardware.VULKAN: ["vulkan", "cpu"],
        hardware.METAL: ["metal", "cpu"],
    }.get(hw.accelerator.kind, ["cpu", "vulkan"])
    llama = next((p for p in (find_exe(bin_dir / "llama" / v, "llama-server") for v in order) if p), None)
    llama = llama or find_exe(bin_dir, "llama-server")
    if llama:
        cfg.llama_exe = llama

    # The model: the catalogue's pick for this tier if the drive has it,
    # else the largest model on the drive that this tier may run.
    models_dir = cfg.models_dir or root / "models"
    reranker = catalog.reranker_model()
    present = [m for m in catalog.models()
               if (models_dir / m.filename).exists() and (reranker is None or m.id != reranker.id)]
    pick = catalog.chat_model_for(hw.tier, hw.system_ram_mb)
    if pick is None or pick not in present:
        allowed = hardware.TIERS[:hardware.TIERS.index(hw.tier) + 1]
        runnable = [m for m in present if any(t in allowed for t in m.tiers)]
        pick = max(runnable, key=lambda m: m.size_bytes, default=None)
    if pick is not None:
        cfg.chat_model = models_dir / pick.filename
        cfg.chat_args = list(pick.server_args)
    if reranker is not None and (models_dir / reranker.filename).exists():
        cfg.reranker_model = models_dir / reranker.filename
    cpu_only = hw.accelerator.kind == hardware.CPU
    cfg.chat_gpu_layers = 0 if cpu_only else 999

    # Nothing to fit without a GPU: everything runs on the CPU.
    profile = MachineProfile(machine=describe(hw), fitted=cpu_only)
    profile.absorb(cfg)
    return profile


def find_exe(folder: Path, stem: str) -> Path | None:
    p = folder / (f"{stem}.exe" if sys.platform == "win32" else stem)
    return p if p.exists() else None


def describe(hw: hardware.Hardware) -> str:
    host = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or _platform.node() or "this computer"
    a = hw.accelerator
    gpu = "no GPU" if a.kind == hardware.CPU else f"{a.name} ({a.memory_mb} MB, {a.kind})"
    return f"{host}: {gpu}, {hw.system_ram_mb} MB RAM, tier {hw.tier}"
