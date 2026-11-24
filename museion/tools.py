"""Installing the helper programs: kiwix-serve, llama-server, pdfium.

The archives and their checksums come from tools/*.toml (see catalog.py);
every download goes through download.py, so nothing unverified is ever
unpacked.

Archives are unpacked with Python's own zipfile and tarfile, which read both
formats on every platform, so nothing outside Python is needed. (The system's
tar is not the same everywhere: GNU tar reads a Windows drive letter as a
remote host.) Every publisher lays its archive out differently, so the result
is flattened into one directory afterwards.
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from museion import catalog, download, hardware

# What a Step reports.
DOWNLOADING = "downloading"
UNPACKING = "unpacking"
DONE = "done"
SKIPPED = "skipped"


@dataclass
class Step:
    kind: str  # DOWNLOADING, UNPACKING, DONE or SKIPPED
    tool: str
    file: str = ""      # DOWNLOADING: the archive
    received: int = 0   # DOWNLOADING
    total: int = 0      # DOWNLOADING
    reason: str = ""    # SKIPPED


@dataclass
class Installed:
    """Where the installed programs ended up."""

    kiwix_serve: Path | None
    llama_server: Path | None
    pdfium_dir: Path


def plan(accel: hardware.Accelerator) -> list[tuple[str, str, int]]:
    """Which archives this machine would fetch, with sizes, for showing a
    plan: (tool name, variant, bytes including its companions)."""
    out = []
    for t in catalog.tools():
        picked = catalog.tool_variant(t, accel)
        if picked is None:
            continue
        key, v = picked
        size = v.size_bytes + sum(t.variants[k].size_bytes for k in v.also if k in t.variants)
        out.append((t.name, key, size))
    return out


def install(accel: hardware.Accelerator, dest: Path, report: Callable[[Step], None] | None = None) -> Installed:
    """Install every tool for this machine's accelerator."""
    return install_selected(catalog.accelerator_name(accel), dest, [], report)


def install_selected(accel: str, dest: Path, only: list[str] | tuple[str, ...] = (),
                     report: Callable[[Step], None] | None = None) -> Installed:
    """Install some of the tools -- those whose names are in `only`, or all of
    them when it is empty -- for a named accelerator. What a drive being
    prepared needs: kiwix-tools and pdfium once, then llama.cpp per build."""
    dest = Path(dest)
    report = report or (lambda step: None)
    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="museion-tools-"))
    try:
        for tool in catalog.tools():
            if only and tool.name not in only:
                continue
            picked = catalog.tool_variant_named(tool, accel)
            if picked is None:
                report(Step(SKIPPED, tool.name, reason="no build for this platform"))
                continue
            variant = picked[1]
            parts = [variant]
            for k in variant.also:
                if k not in tool.variants:
                    raise ValueError(f"{tool.id} names missing companion {k}")
                parts.append(tool.variants[k])
            for part in parts:
                archive = staging / part.file
                if not download.is_verified(archive, part.size_bytes, part.sha256):
                    def on_progress(p: download.Progress, name: str = tool.name, file: str = part.file) -> None:
                        report(Step(DOWNLOADING, name, file=file, received=p.received, total=p.total))
                    download.download_verified(
                        download.Spec([part.url], archive, part.size_bytes, part.sha256), on_progress)
                report(Step(UNPACKING, tool.name))
                extract(archive, dest)
            report(Step(DONE, tool.name))
        flatten(dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return Installed(
        kiwix_serve=find_exe(dest, "kiwix-serve"),
        llama_server=find_exe(dest, "llama-server"),
        pdfium_dir=dest,
    )


def find_exe(dir: Path, stem: str) -> Path | None:
    """The executable for `stem` in `dir`, if present."""
    p = Path(dir) / (f"{stem}.exe" if sys.platform == "win32" else stem)
    return p if p.exists() else None


def extract(archive: Path, into: Path) -> None:
    """Unpack a .zip or a .tar.gz (or any tar) into `into`, refusing members
    that would land outside it."""
    try:
        if archive.name.lower().endswith(".zip"):
            _extract_zip(archive, into)
        else:
            with tarfile.open(archive) as tf:
                _extract_tar(tf, into)
    except Exception as e:
        raise RuntimeError(f"could not unpack {archive}: {e}") from e


def _extract_tar(tf: tarfile.TarFile, into: Path) -> None:
    """Unpack a tar, writing each link in it as a copy of what it points at.
    Linux and macOS builds name one library three ways with symbolic links
    (libllama.so, libllama.so.0, libllama.so.0.3.0), and a drive formatted
    exFAT, which is what moves between systems, cannot hold a link at all."""
    members = tf.getmembers()
    _check_tar(tf, into)
    files = [m for m in members if not (m.issym() or m.islnk())]
    if hasattr(tarfile, "data_filter"):
        tf.extractall(into, members=files, filter="data")
    else:  # Pythons before 3.11.4 have no filter
        tf.extractall(into, members=files)
    root = into.resolve()
    pending = [m for m in members if m.issym() or m.islnk()]
    while pending:
        # A link may point at another link; each pass copies those whose
        # target is there by now. One that points at nothing is left out.
        waiting = []
        for m in pending:
            target = root / m.name
            source = (target.parent / m.linkname if m.issym() else root / m.linkname).resolve()
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                waiting.append(m)
        if len(waiting) == len(pending):
            break
        pending = waiting


def _check_tar(tf: tarfile.TarFile, into: Path) -> None:
    root = into.resolve()
    for m in tf.getmembers():
        target = (root / m.name).resolve()
        points_at = target
        if m.issym():
            points_at = (target.parent / m.linkname).resolve()
        elif m.islnk():
            points_at = (root / m.linkname).resolve()
        if not (target.is_relative_to(root) and points_at.is_relative_to(root)):
            raise ValueError(f"{m.name} would land outside {into}")


def _extract_zip(archive: Path, into: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            # zipfile drops "..", drive letters and leading slashes itself.
            target = Path(zf.extract(info, into))
            # zipfile does not restore permissions. A zip made on Unix keeps
            # them in the top bits of external_attr, and without the execute
            # bit a program unpacked from it will not start.
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                os.chmod(target, mode)


def _wanted(name: str) -> bool:
    """Programs and shared libraries. Unix programs have no extension."""
    return (name.endswith((".exe", ".dll", ".dylib")) or ".so" in name or "." not in name)


def flatten(dir: Path) -> None:
    """Move every executable and shared library up into `dir`, so the
    programs sit beside the libraries they load whatever folders the
    publisher packed them in. A file already at the top is not replaced."""
    root = Path(dir)

    def walk(d: Path, depth: int) -> None:
        if depth > 4:
            return
        for p in sorted(d.iterdir()):
            if p.is_dir():
                walk(p, depth + 1)
            elif d != root and _wanted(p.name):
                target = root / p.name
                if not target.exists():
                    try:
                        p.rename(target)
                    except OSError as e:
                        raise RuntimeError(f"moving {p.name}: {e}") from e

    walk(root, 0)
