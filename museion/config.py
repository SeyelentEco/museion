"""Where everything lives on this machine.

There is deliberately no default library location. Content runs from under a
gigabyte to well over 250 GB, and a tool that silently fills someone's system
drive with Wikipedia has done something rude. The path is asked for once and
then remembered.

The file is the same config.json the Rust app reads and writes, in the same
folder, so a machine set up by one is set up for the other.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The Rust app's identifier, which names its folder. Kept so both apps share
# one configuration while the Rust one is still around.
APP_ID = "org.museion.desktop"

# Fields holding a path, stored as text in the file. extra_dirs is a list of
# them and handled on its own.
PATH_FIELDS = ("library_dir", "kiwix_exe", "llama_exe", "pdfium_dir", "models_dir", "chat_model", "reranker_model")


@dataclass
class Config:
    # Directory holding .zim archives and the search index.
    library_dir: Path | None = None
    # Other folders of content someone has found: .zim archives are served
    # and indexed like catalogue ones, loose PDFs are indexed by page. The
    # catalogue is a starting point, not a boundary.
    extra_dirs: list[Path] = field(default_factory=list)
    # The kiwix-serve program. A full path rather than a directory, because
    # kiwix-tools and llama.cpp come from different upstreams and many people
    # already have one or both installed somewhere of their own choosing.
    kiwix_exe: Path | None = None
    # The llama-server program.
    llama_exe: Path | None = None
    # Directory holding the pdfium library, used when indexing PDF archives.
    pdfium_dir: Path | None = None
    # Directory the setup flow downloads models into.
    models_dir: Path | None = None
    # GGUF for the chat model.
    chat_model: Path | None = None
    # GGUF for the reranker.
    reranker_model: Path | None = None
    # Extra llama-server arguments for the chat model, normally produced by
    # llama-fit-params against the actual card.
    chat_args: list[str] = field(default_factory=list)
    # How many layers of the chat model to put on the GPU.
    chat_gpu_layers: int = 999
    kiwix_port: int = 8090
    chat_port: int = 8080
    reranker_port: int = 8081
    # Start the services when the app opens. On by default: a person opening
    # Museion wants to ask something. Off for those who would rather the GPU
    # stay free until they choose -- the model holds nearly all of it.
    autostart: bool = True
    # Set when running from a drive: paths are stored relative to this
    # folder and resolved against it, so the drive's letter can change.
    # Not part of the file.
    base: Path | None = field(default=None, compare=False, repr=False)

    def copy(self) -> Config:
        return dataclasses.replace(self, extra_dirs=list(self.extra_dirs), chat_args=list(self.chat_args))

    def content_dirs(self) -> list[Path]:
        """Every folder content is read from: the library first, then extras."""
        return ([self.library_dir] if self.library_dir else []) + list(self.extra_dirs)

    def index_path(self) -> Path | None:
        return self.library_dir / "museion-index.db" if self.library_dir else None

    def is_ready(self) -> bool:
        """True once there is enough here to actually answer a question."""
        return bool(self.library_dir and self.library_dir.exists() and self.chat_model and self.chat_model.exists())

    def to_json(self) -> dict:
        """The config as the page and the file see it: paths as text."""
        out = dataclasses.asdict(self)
        del out["base"]
        for name in PATH_FIELDS:
            out[name] = str(out[name]) if out[name] is not None else None
        out["extra_dirs"] = [str(d) for d in self.extra_dirs]
        return out

    @classmethod
    def from_json(cls, data: dict) -> Config:
        """Build a config from what the page sent or the file held. Unknown
        fields are ignored and missing ones keep their defaults, as the Rust
        version did, so a file from an older or newer build still loads."""
        c = cls()
        for f in dataclasses.fields(cls):
            if f.name == "base" or f.name not in data or data[f.name] is None:
                continue
            value = data[f.name]
            if f.name in PATH_FIELDS:
                value = Path(value) if str(value).strip() else None
            elif f.name == "extra_dirs":
                value = [Path(d) for d in value]
            elif f.name == "chat_args":
                value = [str(a) for a in value]
            elif f.name == "autostart":
                value = bool(value)
            else:
                value = int(value)
            setattr(c, f.name, value)
        # A config written by an older build may be missing ports, or hold 0.
        for name, default in (("kiwix_port", 8090), ("chat_port", 8080), ("reranker_port", 8081)):
            if not getattr(c, name):
                setattr(c, name, default)
        return c

    def save(self, path: Path) -> None:
        out = self.to_json()
        if self.base:
            # On a drive, anything under the root is written relative to it,
            # with forward slashes, which every system reads.
            def rel(p: str | None) -> str | None:
                if p is None or not Path(p).is_relative_to(self.base):
                    return p
                return Path(p).relative_to(self.base).as_posix()
            for name in PATH_FIELDS:
                out[name] = rel(out[name])
            out["extra_dirs"] = [rel(d) for d in out["extra_dirs"]]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    def resolved(self, base: Path) -> Config:
        """This config with every relative path made absolute against `base`:
        a drive's config says `library`, and means the library folder beside
        it wherever the drive is."""
        c = self.copy()
        for name in PATH_FIELDS:
            p = getattr(c, name)
            if p is not None and not p.is_absolute():
                setattr(c, name, base / p)
        c.extra_dirs = [d if d.is_absolute() else base / d for d in c.extra_dirs]
        c.base = base
        return c


def load(path: Path) -> Config:
    """The config at `path`, or the defaults if there is none or it is broken.

    Notepad and PowerShell 5 write a UTF-8 byte-order mark, which a JSON
    reader rejects. A config that fails to parse becomes an empty one, and an
    empty one shows the first-run screen as if the library had never existed;
    that is not a fair price for editing the file by hand. utf-8-sig reads
    the file with or without the mark."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return Config.from_json(data) if isinstance(data, dict) else Config()
    except (OSError, ValueError, TypeError):
        return Config()


def config_dir() -> Path:
    """The folder config.json and the tuning override live in: the one the
    Rust app uses on each system."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_ID
