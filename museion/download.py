"""Resumable, verified downloads -- the Python twin of the Rust app's
download.rs and of scripts/lib/download.js.

Same rules, for the same reasons:

  * Bytes land in `<name>.part` and are renamed only after the hash matches,
    so a file under its real name is always a good file.
  * Resume re-hashes what is already on disk before appending, so the final
    digest covers the whole file without a second read.
  * A server that ignores a Range request and answers 200 would silently
    append a second copy of the file from byte zero; that is detected.
  * A bad hash deletes the partial file: the bytes are wrong, not merely
    incomplete, and keeping them poisons every future resume.
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20
# Giving up on a dead mirror quickly lets the next one have its turn; a live
# but slow one may pause far longer between reads.
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 120
USER_AGENT = "museion"


@dataclass
class Progress:
    received: int
    total: int
    # True while re-hashing an existing partial file rather than downloading.
    resuming: bool = False


@dataclass
class Spec:
    urls: list[str]
    dest: Path
    size_bytes: int
    sha256: str


def is_verified(path: Path, size_bytes: int, sha256: str) -> bool:
    """Is the file already present and correct? The size is checked first,
    so a wrong file of a different size costs nothing to reject."""
    try:
        if Path(path).stat().st_size != size_bytes:
            return False
    except OSError:
        return False
    return _hash_file(Path(path), hashlib.sha256()) == sha256.lower()


def _hash_file(path: Path, hasher, on_read: Callable[[int], None] | None = None) -> str:
    read = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            hasher.update(chunk)
            read += len(chunk)
            if on_read:
                on_read(read)
    return hasher.hexdigest()


def part_path(dest: Path) -> Path:
    """Where a download collects until its hash is checked: `<name>.part`."""
    return dest.with_name(dest.name + ".part")


def download_verified(spec: Spec, on_progress: Callable[[Progress], None] | None = None) -> str:
    """Download one file, trying each URL in turn, resuming a prior `.part`.
    Returns the URL that worked."""
    dest = Path(spec.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = part_path(dest)
    report = on_progress or (lambda p: None)

    last: Exception | None = None
    for url in spec.urls:
        try:
            _attempt(url, spec, dest, part, report)
            return url
        except Exception as e:  # any failure, network or disk, moves on to the next URL
            last = e
    raise RuntimeError(f"all sources failed for {dest}\n  last error: {last}")


def _open(url: str, headers: dict[str, str], read_timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        response = urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"requesting {url}: {e}") from e
    # urllib has one timeout for connecting and reading alike; lengthen it
    # for the reads. The socket is not public, so this is best-effort.
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(read_timeout)
    return response


def _attempt(url: str, spec: Spec, dest: Path, part: Path, report: Callable[[Progress], None]) -> None:
    hasher = hashlib.sha256()
    offset = 0

    if part.exists():
        have = part.stat().st_size
        if 0 < have < spec.size_bytes:
            _hash_file(part, hasher, lambda read: report(Progress(read, have, resuming=True)))
            offset = have
        elif have >= spec.size_bytes:
            # Oversized or complete-but-unrenamed: start clean rather than guess.
            part.unlink()

    headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
    with _open(url, headers, READ_TIMEOUT) as response:
        if offset > 0 and response.status != 206:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{url} does not support resume (replied {response.status}, expected 206)")

        received = offset
        last_tick = time.monotonic()
        with open(part, "ab" if offset > 0 else "wb") as out:
            while True:
                try:
                    chunk = response.read(CHUNK)
                except Exception as e:
                    raise RuntimeError(f"reading from {url}: {e}") from e
                if not chunk:
                    break
                hasher.update(chunk)
                out.write(chunk)
                received += len(chunk)
                if time.monotonic() - last_tick > 0.25:
                    last_tick = time.monotonic()
                    report(Progress(received, spec.size_bytes))
    report(Progress(received, spec.size_bytes))

    size = part.stat().st_size
    if size != spec.size_bytes:
        raise RuntimeError(f"size mismatch: expected {spec.size_bytes} bytes, got {size}")
    digest = hasher.hexdigest()
    if digest != spec.sha256.lower():
        part.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch\n    expected {spec.sha256}\n    got      {digest}")
    os.replace(part, dest)


def fetch_unverified(url: str, dest: Path, on_progress: Callable[[int, int], None] | None = None) -> None:
    """Download a file with no known hash or size -- the WebView2 runtime
    installer, which Microsoft republishes under one link -- to a `.part`
    renamed into place when complete. Nothing verified by a checksum should
    go through here. `on_progress(received, total)`; total is 0 when the
    server does not say."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    with _open(url, {}, 60) as response:
        try:
            total = int(response.headers.get("Content-Length", "0"))
        except ValueError:
            total = 0
        received = 0
        with open(part, "wb") as out:
            while chunk := response.read(CHUNK):
                out.write(chunk)
                received += len(chunk)
                if on_progress:
                    on_progress(received, total)
    # Anything this small is an error page, not an installer.
    if received < 1_000_000:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{url} returned only {received} bytes")
    os.replace(part, dest)
