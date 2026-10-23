"""A drive's MANIFEST.yaml: every archive and model on it with its size and
SHA-256, written when the drive is prepared and checked again whenever
someone asks -- years later, on a drive that has sat in a cupboard.

The hashes are the catalogue's, and each file was verified against them on
its way onto the drive. The file is a small, fixed shape of YAML, written
and read here without a YAML library.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from museion import download
from museion.catalog import Model, Source

NAME = "MANIFEST.yaml"


def write(root: Path, bundle: str, accelerators: list[str], carried: list[Source | Model]) -> None:
    lines = ["# Museion drive manifest. Every file below was verified against its sha256 when",
             "# the drive was prepared. Settings > This drive in Museion checks them again, as",
             "# does starting Museion with the word verify (Museion.cmd verify).",
             f"bundle: {bundle}",
             f"accelerators: [{', '.join(accelerators)}]",
             "files:"]
    for item in carried:
        folder = "library" if isinstance(item, Source) else "models"
        lines += [f"  - path: {folder}/{item.filename}", f"    bytes: {item.size_bytes}", f"    sha256: {item.sha256}"]
    (root / NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def read(root: Path) -> list[dict]:
    """The manifest's files, as {"path", "bytes", "sha256"}."""
    entries: list[dict] = []
    for raw in (root / NAME).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("- path: "):
            entries.append({"path": line[len("- path: "):]})
        elif entries and line.startswith(("bytes: ", "sha256: ")):
            key, _, value = line.partition(": ")
            entries[-1][key] = value
    return entries


def verify(root: Path, report: Callable[[str, int, int], None] = lambda item, done, total: None) -> list[str]:
    """Check every file in the manifest against its size and hash. Returns
    the problems, one sentence each; none means the drive is whole."""
    root = Path(root)
    try:
        entries = read(root)
    except OSError as e:
        return [f"There is no manifest to check against ({e})."]
    problems = []
    for i, e in enumerate(entries, 1):
        path = root / e["path"]
        report(f"Checking {e['path']}", i, len(entries))
        if not path.exists():
            problems.append(f"{e['path']} is missing")
        elif path.stat().st_size != int(e["bytes"]):
            problems.append(f"{e['path']} is {path.stat().st_size} bytes, not {e['bytes']}")
        elif not download.is_verified(path, int(e["bytes"]), e["sha256"]):
            problems.append(f"{e['path']} has changed: its checksum no longer matches")
    return problems
