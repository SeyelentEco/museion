"""Asking whether a newer Museion has been released, and fetching it.

Museion works without the internet, and nothing here runs by itself: the
page has a button, Settings > Version and upgrades > Check for a newer
version, and this is what it does. It asks GitHub for the latest release,
picks the download for this system, and can fetch it into the copy's own
folder, checked against the release's checksum, ready for the upgrade
(upgrade_commands.py). Someone with no internet is handed the same file on a
stick instead.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from museion import catalog, download
from museion.version import VERSION, newer

LATEST = "https://api.github.com/repos/SeyelentEco/museion/releases/latest"
# The release job's file names (.github/workflows/release.yml).
BUILDS = {("windows", "x64"): "windows.zip", ("macos", "arm64"): "macos.zip", ("linux", "x64"): "linux.tar.gz"}


class Unknown(RuntimeError):
    """The latest release could not be learned, worded for the person."""


@dataclass
class Release:
    version: str
    page: str            # the release's page, to read or download from by hand
    notes: str           # what it changed: its section of CHANGELOG.md
    file: str = ""       # this system's download; empty if the release has none
    url: str = ""
    size: int = 0
    sha256: str = ""

    def is_newer(self) -> bool:
        return newer(self.version, VERSION)


def latest(url: str | None = None) -> Release:
    """The latest release and its download for this system."""
    data = _get_json(url or LATEST)
    tag = str(data.get("tag_name") or "")
    release = Release(tag.lstrip("v"), str(data.get("html_url") or ""), str(data.get("body") or "").strip())
    if not release.version:
        raise Unknown("GitHub's answer named no release")
    build = BUILDS.get((catalog.current_platform(), catalog.current_arch()))
    assets = {str(a.get("name")): a for a in data.get("assets") or [] if isinstance(a, dict)}
    asset = assets.get(f"museion-{release.version}-{build}") if build else None
    if asset:
        release.file = str(asset["name"])
        release.url = str(asset.get("browser_download_url") or "")
        release.size = int(asset.get("size") or 0)
        release.sha256 = str(asset.get("digest") or "").removeprefix("sha256:") or _listed_sum(assets, release.file)
    return release


def fetch(release: Release, folder: Path, report: Callable[[int, int], None] = lambda done, total: None) -> Path:
    """Download the release's file for this system into `folder`, resuming
    an earlier try, and return it once its checksum matches."""
    if not (release.url and release.sha256 and release.size):
        raise Unknown(f"version {release.version} has no download for this kind of computer that can be checked; "
                      f"see {release.page}")
    dest = Path(folder) / release.file
    if not download.is_verified(dest, release.size, release.sha256):
        download.download_verified(download.Spec([release.url], dest, release.size, release.sha256),
                                   lambda p: report(p.received, p.total))
    return dest


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": download.USER_AGENT,
                                                   "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=download.CONNECT_TIMEOUT) as res:
            data = json.load(res)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Unknown("GitHub shows no release of Museion (none is published, or they are not public)") from e
        raise Unknown(f"GitHub answered {e.code} {e.reason}") from e
    except (OSError, ValueError) as e:
        raise Unknown(f"could not reach GitHub to ask ({getattr(e, 'reason', e)}). Museion itself needs no internet; "
                      "a newer version can also be brought on a drive.") from e
    if not isinstance(data, dict):
        raise Unknown("GitHub's answer was not a release")
    return data


def _listed_sum(assets: dict[str, dict], file: str) -> str:
    """The file's checksum from the release's SHA256SUMS.txt, for a release
    whose assets carry none of their own."""
    sums = assets.get("SHA256SUMS.txt")
    if not sums or not sums.get("browser_download_url"):
        return ""
    request = urllib.request.Request(str(sums["browser_download_url"]), headers={"User-Agent": download.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=download.CONNECT_TIMEOUT) as res:
            text = res.read(1 << 16).decode("utf-8", "replace")
    except OSError:
        return ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == file and len(parts[0]) == 64:
            return parts[0].lower()
    return ""
