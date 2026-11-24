"""What a prepared drive or a downloaded package holds, written down.

`support/package.json` at the top of a drive records which Museion its app/
was copied from, which system its Python and helper programs are built for,
and exactly which build of each helper it carries (the checksum of the
archive it was unpacked from). An upgrade reads two of these, the copy being
upgraded and the newer one, to see which parts differ: the code nearly
always, the helpers and the Python only when their pinned builds changed.

  {"version": "0.1.0", "platform": "windows", "arch": "x64",
   "helpers": {"python": "<sha256>", "llama.cpp/cuda": "<sha256>", ...}}

A drive prepared before this file existed has none, and counts as holding
nothing known: every part of it differs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from museion import catalog, disk

NAME = "package.json"


@dataclass
class Package:
    version: str = ""
    platform: str = ""
    arch: str = ""
    # Helper name -> the sha256 of the archive it came from. A llama.cpp
    # build is named with its kind: "llama.cpp/cuda".
    helpers: dict[str, str] = field(default_factory=dict)


def path(root: Path) -> Path:
    return Path(root) / "support" / NAME


def read(root: Path) -> Package:
    """A drive's record, or an empty one if it has none or it cannot be read."""
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Package()
    if not isinstance(data, dict):
        return Package()
    helpers = data.get("helpers")
    return Package(str(data.get("version") or ""), str(data.get("platform") or ""), str(data.get("arch") or ""),
                   {str(k): str(v) for k, v in helpers.items()} if isinstance(helpers, dict) else {})


def write(root: Path, package: Package) -> None:
    path(root).parent.mkdir(parents=True, exist_ok=True)
    disk.write_text(path(root), json.dumps({
        "version": package.version, "platform": package.platform, "arch": package.arch,
        "helpers": dict(sorted(package.helpers.items())),
    }, indent=2) + "\n")


def record_helper(root: Path, name: str, sha256: str) -> None:
    """Note that `name` on this drive was unpacked from the archive with this
    checksum, on this system."""
    package = read(root)
    package.platform, package.arch = catalog.current_platform(), catalog.current_arch()
    package.helpers[name] = sha256
    write(root, package)


def record_version(root: Path, version: str) -> None:
    package = read(root)
    package.platform, package.arch = catalog.current_platform(), catalog.current_arch()
    package.version = version
    write(root, package)


def variant_sha(tool: catalog.Tool, variant: catalog.ToolVariant) -> str:
    """One checksum for a variant and its companions: the CUDA build is two
    archives, and a change to either is a different build."""
    parts = [variant.sha256, *(tool.variants[k].sha256 for k in variant.also if k in tool.variants)]
    return "+".join(parts)


def llama_build(root: Path) -> str:
    """A short name for the llama.cpp builds a drive holds, which changes
    when an upgrade replaces them. Empty for a drive with no record."""
    builds = sorted(f"{name}={sha}" for name, sha in read(root).helpers.items() if name.startswith("llama.cpp"))
    return hashlib.sha256("\n".join(builds).encode()).hexdigest()[:16] if builds else ""


def made_for(root: Path) -> tuple[str, str]:
    """The system a copy's Python and helpers are built for: (platform,
    arch). From its record, or for a copy from before the record, from the
    launcher it was given. ("", "") if neither says."""
    held = read(root)
    if held.platform:
        return held.platform, held.arch
    for launcher, platform in (("Museion.cmd", "windows"), ("Museion.command", "macos"), ("museion.sh", "linux")):
        if (Path(root) / launcher).is_file():
            # Before the record there was one build per system.
            return platform, "arm64" if platform == "macos" else "x64"
    return "", ""


def code_version(app: Path) -> str:
    """The version of the Museion code in a folder (a drive's app/, or a
    checkout), read from its version.py without running it. Empty if it has
    none: a copy from before versions, or not Museion at all."""
    try:
        text = (Path(app) / "museion" / "version.py").read_text(encoding="utf-8")
    except OSError:
        # Before versions there was still a museion package to recognise.
        return ""
    found = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return found.group(1) if found else ""


def app_version(root: Path) -> str:
    """The version of the code in a drive's app/."""
    return code_version(Path(root) / "app")
