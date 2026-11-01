"""The content, model and tool catalogue.

The files under catalog/, models/ and tools/ say what Museion can fetch, from
where, and the size and SHA-256 each download must have. A tool built for the
moment the network is gone must carry its own knowledge of where everything
came from -- the checksums above all, since those are what let a library on a
drive in a cupboard prove it is still intact years later.

They are TOML, read from the folders beside the package, which travel on
the drive with the code, so a corrected URL or a new model is an edit to a
text file. (The Rust version compiled them into its binary, as YAML.)

Every function reads the files afresh, so an edit shows at once.
"""

from __future__ import annotations

import platform as _platform
import sys
import tomllib
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path

from museion import hardware

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "catalog"
MODELS_DIR = ROOT / "models"
TOOLS_DIR = ROOT / "tools"
# What only a prepared drive carries: the portable Python and opencode.
DRIVE_DIR = ROOT / "drive"

@dataclass
class License:
    name: str
    attribution: str = ""


@dataclass
class Source:
    id: str
    title: str
    category: str
    filename: str
    size_bytes: int
    sha256: str
    urls: list[str]
    description: str = ""
    mirrors: list[str] = field(default_factory=list)
    # BitTorrent alternatives, offered only after every web source fails.
    # Museion never downloads via the swarm itself and never seeds; these are
    # handed to the person for their own client.
    torrent: str | None = None
    magnet: str | None = None
    license: License | None = None

    def short_magnet(self) -> str | None:
        """The magnet link trimmed to what a client needs: infohash, length,
        name. Kiwix's full magnet carries a dozen webseed URLs that are the
        same mirrors that just failed, and they make it unreadable when
        printed."""
        return None if self.magnet is None else self.magnet.split("&as=")[0]

    def all_urls(self) -> list[str]:
        """Every URL to try, primary first."""
        return [*self.urls, *self.mirrors]


@dataclass
class Bundle:
    id: str
    title: str
    sources: list[str]
    description: str = ""


@dataclass
class Requires:
    system_ram_mb: int = 0


@dataclass
class Model:
    id: str
    title: str
    role: str
    filename: str
    size_bytes: int
    sha256: str
    urls: list[str]
    tiers: list[str] = field(default_factory=list)
    server_args: list[str] = field(default_factory=list)
    requires: Requires | None = None
    license: License | None = None
    # The tier's default when more than one model names it. Without this the
    # first file in name order wins, which is alphabetical -- adding a Gemma
    # beside the Qwen would have made Gemma the default.
    preferred: bool = False


@dataclass
class ToolVariant:
    file: str
    url: str
    size_bytes: int
    sha256: str
    accelerator: str | None = None
    platform: str | None = None
    arch: str | None = None
    # A companion archive (the CUDA runtime), never chosen on its own.
    internal: bool = False
    also: list[str] = field(default_factory=list)


@dataclass
class Tool:
    id: str
    name: str
    provides: list[str]
    variants: dict[str, ToolVariant]  # sorted by key, so the choice is stable
    license: License | None = None


# ------------------------------------------------------------------ reading

_NESTED = {"License": License, "Requires": Requires, "ToolVariant": ToolVariant}
_PLAIN = {"str": str, "int": int, "bool": bool}


def _check(value: object, kind: str, where: str) -> object:
    """Checks a value against a field's annotation (written as a string, such
    as "list[str]" or "License | None") and builds nested tables."""
    if kind.endswith(" | None"):
        return None if value is None else _check(value, kind[:-7], where)
    if kind in _NESTED:
        if not isinstance(value, dict):
            raise ValueError(f"{where} should be a table")
        return _build(_NESTED[kind], value, where)
    if kind.startswith("dict[str, "):
        if not isinstance(value, dict):
            raise ValueError(f"{where} should be a table")
        return {k: _check(v, kind[10:-1], f"{where}.{k}") for k, v in sorted(value.items())}
    if kind.startswith("list["):
        if not isinstance(value, list):
            raise ValueError(f"{where} should be a list")
        return [_check(v, kind[5:-1], f"{where}[{i}]") for i, v in enumerate(value)]
    want = _PLAIN[kind]
    # In Python True is also an int; a size of `true` is still a mistake.
    if not isinstance(value, want) or (want is int and isinstance(value, bool)):
        raise ValueError(f"{where} should be {'an' if want is int else 'a'} {kind}, not {value!r}")
    if want is int and value < 0:
        raise ValueError(f"{where} cannot be negative")
    return value


def _build(cls: type, data: dict, where: str = "") -> object:
    """Builds a dataclass from a TOML table. Keys it does not know are
    ignored, as the Rust parser ignored them (the files carry notes such as
    the quantisation that nothing reads)."""
    args, missing = {}, []
    for f in fields(cls):
        name = f"{where}.{f.name}" if where else f.name
        if f.name in data:
            args[f.name] = _check(data[f.name], f.type, name)
        elif f.default is MISSING and f.default_factory is MISSING:
            missing.append(name)
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    return cls(**args)


def _parse_dir(directory: Path, cls: type) -> list:
    out = []
    for path in sorted(directory.glob("*.toml"), key=lambda p: p.name):
        try:
            out.append(_build(cls, tomllib.loads(path.read_text(encoding="utf-8"))))
        except (tomllib.TOMLDecodeError, ValueError, UnicodeDecodeError) as e:
            raise ValueError(f"parsing {path}: {e}") from e
    return out


def sources() -> list[Source]:
    return _parse_dir(CATALOG_DIR / "sources", Source)


def bundles() -> list[Bundle]:
    return _parse_dir(CATALOG_DIR / "bundles", Bundle)


def models() -> list[Model]:
    return _parse_dir(MODELS_DIR, Model)


def tools() -> list[Tool]:
    return _parse_dir(TOOLS_DIR, Tool)


def drive_tools() -> list[Tool]:
    return _parse_dir(DRIVE_DIR, Tool)


# ------------------------------------------------------------------ choosing

def bundle(id: str) -> tuple[Bundle, list[Source]]:
    """A bundle and its sources, in the bundle's order."""
    b = next((b for b in bundles() if b.id == id), None)
    if b is None:
        raise ValueError(f"no bundle named {id!r}")
    all_sources = sources()
    picked = []
    for sid in b.sources:
        s = next((s for s in all_sources if s.id == sid), None)
        if s is None:
            raise ValueError(f"bundle {id} references unknown source {sid!r}")
        picked.append(s)
    return b, picked


def chat_model_for(tier: str, system_ram_mb: int) -> Model | None:
    """The chat model recommended for a tier (hardware.MODEST and so on, the
    names the model files list), honouring its RAM requirement.

    If the tier's model needs more system RAM than the machine has, the next
    tier down is offered rather than a model that will fail to load."""
    if tier not in hardware.TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {', '.join(hardware.TIERS)}")
    all_models = models()
    i = hardware.TIERS.index(tier)
    while i >= 0:
        name = hardware.TIERS[i]
        fits = [m for m in all_models
                if m.role == "chat" and name in m.tiers
                and (m.requires is None or m.requires.system_ram_mb <= system_ram_mb)]
        pick = next((m for m in fits if m.preferred), fits[0] if fits else None)
        if pick is not None:
            return pick
        i -= 1
    return None


def reranker_model() -> Model | None:
    return next((m for m in models() if m.role == "reranker"), None)


def accelerator_name(accel: hardware.Accelerator | str) -> str:
    """"cuda", "metal", "vulkan" or "cpu": the accelerator's kind, or the
    name itself when given one (a drive being prepared names the machines it
    is for)."""
    name = accel if isinstance(accel, str) else accel.kind
    if name not in (hardware.CUDA, hardware.METAL, hardware.VULKAN, hardware.CPU):
        raise ValueError(f"unknown accelerator {accel!r}")
    return name


def tool_variant(tool: Tool, accel: hardware.Accelerator | str, *, platform: str | None = None,
                 arch: str | None = None) -> tuple[str, ToolVariant] | None:
    """Which tool variant suits this machine."""
    return tool_variant_named(tool, accelerator_name(accel), platform=platform, arch=arch)


def current_platform() -> str:
    """The platform name the tool files use."""
    if sys.platform == "win32":
        return "windows"
    return "darwin" if sys.platform == "darwin" else "linux"


def current_arch() -> str:
    """The architecture name the tool files use."""
    return "arm64" if _platform.machine().lower() in ("arm64", "aarch64") else "x64"


def tool_variant_named(tool: Tool, want: str, *, platform: str | None = None,
                       arch: str | None = None) -> tuple[str, ToolVariant] | None:
    """The variant for a named accelerator ("cuda", "vulkan", "cpu", "metal")
    on this platform. A drive being prepared carries builds for machines
    other than the one preparing it, so the accelerator is a name here.
    `platform` and `arch` default to this machine's."""
    platform = platform or current_platform()
    arch = arch or current_arch()

    def fits(v: ToolVariant) -> bool:
        return (not v.internal
                and (v.platform is None or v.platform == platform)
                and (v.arch is None or v.arch == arch))

    # An exact build first, then one for any accelerator, then the CPU build.
    for wanted in (want, None, "cpu"):
        for key, v in tool.variants.items():
            if fits(v) and v.accelerator == wanted:
                return key, v
    return None
