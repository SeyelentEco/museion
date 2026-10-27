"""Catalogue entries for Kiwix archives, written from what Kiwix publishes
beside each one. Needs the internet; for an archive already copied into the
library, with no internet, see catalog_add.py.

  python -m museion sync-source https://download.kiwix.org/zim/other/zimgit-water_en_2024-08.zim
      --category survival --id zimgit-water
  python -m museion check-sources        # any newer versions upstream?

Kiwix publishes a .sha256, a .magnet (carrying the BitTorrent infohash and
the exact byte length) and a .torrent next to every ZIM. So an entry can be
made with complete, authentic integrity data without downloading the content
itself -- which matters when the content is 115 GB.

Hand-typed hashes rot. Always regenerate.
"""

from __future__ import annotations

import re
import shlex
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from museion import catalog, disk
from museion.catalog_add import CATEGORIES, toml_lines

TIMEOUT = 60
# The catalogue serves an interstitial to clients it does not recognise.
USER_AGENT = "Mozilla/5.0 (Museion sync-source)"
KIWIX_CATALOGUE = "https://library.kiwix.org/catalog/v2/entries"
HEADER = "# Written by python -m museion sync-source -- do not hand-edit size_bytes or sha256.\n"
RECIPE = "# Regenerate with: "

# fetch(url, method): for GET the body, for HEAD the Content-Length ("" when
# the server gives none). Raises on any failure, a 404 included.
Fetch = Callable[[str, str], str]


def fetch(url: str, method: str = "GET") -> str:
    request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if method == "HEAD":
            return response.headers.get("Content-Length") or ""
        return response.read().decode("utf-8", errors="replace")


@dataclass
class Probe:
    sha256: str
    size_bytes: int
    magnet: str | None
    torrent: str | None


def probe(url: str, get: Fetch = fetch) -> Probe:
    """The sha256, byte length and magnet of a ZIM, from its sidecar files."""
    text = get(f"{url}.sha256", "GET").strip()
    sha256 = text.split()[0] if text else ""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError(f"{url}.sha256 did not contain a sha256 (got {text[:80]!r})")

    magnet, size = None, None
    try:
        magnet = get(f"{url}.magnet", "GET").strip()
        xl = re.search(r"[?&]xl=(\d+)", magnet)
        size = int(xl.group(1)) if xl else None
    except Exception:
        pass  # not every mirror carries magnets; the size then comes from a HEAD
    if size is None:
        length = get(url, "HEAD")
        if not length.isdigit():
            raise ValueError(f"could not determine the size of {url}")
        size = int(length)

    torrent = None
    try:
        get(f"{url}.torrent", "HEAD")
        torrent = f"{url}.torrent"
    except Exception:
        pass  # optional
    return Probe(sha256, size, magnet, torrent)


def catalogue_metadata(stem: str, get: Fetch = fetch) -> dict | None:
    """Ask the Kiwix catalogue what an archive actually is.

    Filenames are not descriptions. "survivors_en_all_maxi" reads, in a
    preparedness catalogue, exactly like the Survivor Library -- a large
    collection of pre-industrial technical works. It is in fact a YouTube
    archive for the video game Vampire Survivors: 13,780 entries of gameplay
    teasers. That very nearly shipped, labelled as public-domain technical
    works, into a bundle people would download expecting to learn how to make
    things. So the title and description come from upstream rather than from
    whoever is adding the entry, and both are printed for a person to look at.
    """
    # The catalogue's free-text search is unreliable here -- "zimgit-water"
    # and "wikipedia_en_all" both return nothing at all -- but its exact
    # name= filter works. Try the full stem first, then drop trailing
    # segments, since a flavour suffix like "_maxi" is sometimes part of the
    # catalogue name and sometimes not.
    parts = stem.split("_")
    for n in range(len(parts), 1, -1):
        name = "_".join(parts[:n])
        try:
            xml = get(f"{KIWIX_CATALOGUE}?name={urllib.parse.quote(name)}&count=5", "GET")
        except Exception:
            continue
        for entry in xml.split("<entry>")[1:]:
            title = _field(entry, "title")
            if title:
                return {"name": name, "title": title, "description": _field(entry, "summary"),
                        "tags": _field(entry, "tags")}
    return None


def _field(entry: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", entry, re.S)
    if not m:
        return ""
    text = m.group(1).strip()
    for escaped, plain in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        text = text.replace(escaped, plain)
    return text


def parse_zim_name(filename: str) -> tuple[str, str | None]:
    """A Kiwix filename's stable part and its release date.

      zimgit-post-disaster_en_2024-05.zim       -> zimgit-post-disaster_en, 2024-05
      wikipedia_en_chemistry_nopic_2026-07.zim  -> wikipedia_en_chemistry_nopic, 2026-07

    Kiwix names are project_lang_selection_flavour_date, and the number of
    segments varies, so only the trailing YYYY-MM is parsed. Finding the
    language segment by position gets "nopic" wrong and leaks the date into
    the id, which then breaks the staleness check -- the one thing the date
    is for."""
    m = re.fullmatch(r"(.+)_(\d{4}-\d{2})\.zim", filename)
    return (m.group(1), m.group(2)) if m else (filename.removesuffix(".zim"), None)


def mirrors_for(url: str, magnet: str | None) -> list[str]:
    """Mirrors, harvested from the magnet's webseed (ws=) list rather than
    guessed. Upstream writes those with a doubled scheme --
    "https://https://host/..." -- so they are repaired here. Getting this
    right is the difference between a library that downloads when
    download.kiwix.org is unreachable and one that does not, which is rather
    the point of the project."""
    path = re.sub(r"^https?://[^/]+", "", url)
    found = [f"https://lb.download.kiwix.org{path}",
             f"https://www.mirrorservice.org/sites/download.kiwix.org{path}"]
    for ws in re.findall(r"[?&]ws=([^&]+)", magnet or ""):
        repaired = re.sub(r"^https?://(?=https?://)", "", urllib.parse.unquote(ws))
        parsed = urllib.parse.urlsplit(repaired)
        # Anything still malformed is skipped rather than poisoning the list.
        if parsed.scheme in ("http", "https") and parsed.netloc:
            found.append(repaired)
    seen, unique = {url}, []
    for u in found:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def recipe(url: str, category: str, id: str, title: str | None) -> str:
    """The command that writes the entry again. It names --id, so a change
    to how ids are made from filenames can never turn a regeneration into a
    second entry beside a stale one. A --title given by hand is part of it
    too: upstream calls both the 12 GB summaries edition and the 49 GB
    full-text edition of Wikipedia simply "Wikipedia", and regenerating from
    upstream alone turned two distinguishable entries into two identical ones."""
    words = ["python -m museion sync-source", url, "--category", category, "--id", id]
    if title:
        words += ["--title", f'"{title}"' if '"' not in title else shlex.quote(title)]
    return " ".join(words)


def source_entry(url: str, category: str, id: str | None = None, title: str | None = None,
                 license: str = "CC-BY-SA-4.0", attribution: str = "Kiwix and contributors",
                 get: Fetch = fetch, say: Callable[[str], None] = print) -> tuple[str, str]:
    """The catalogue id and TOML text for the archive at `url`."""
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}, not {category!r}")
    filename = Path(urllib.parse.urlsplit(url).path).name
    stem, date = parse_zim_name(filename)
    id = id or stem

    say(f"probing {filename} ...")
    p = probe(url, get)
    say(f"  {disk.gb(p.size_bytes)}, sha256 {p.sha256[:12]}...")
    upstream = catalogue_metadata(stem, get)
    if upstream:
        say(f"  upstream title : {upstream['title']}")
        if upstream["description"]:
            say(f"  upstream says  : {upstream['description']}")
        if upstream["tags"]:
            say(f"  tags           : {upstream['tags']}")
    else:
        say("  !! not in the Kiwix catalogue -- check by hand what this archive contains")

    fields: dict = {"id": id,
                    # Upstream's title beats a guess from the filename; --title
                    # still wins, for archives the catalogue does not know.
                    "title": title or (upstream or {}).get("title") or stem}
    if upstream and upstream["description"]:
        fields["description"] = upstream["description"]
    if upstream and "_ftindex:" in upstream["tags"]:
        # Whether upstream says the archive carries a usable full-text index.
        # Kiwix is honest about this -- the zimgit archives declare
        # _ftindex:no, which matches what measuring them showed -- but the
        # indexer still probes behaviour, because the tag is absent for
        # anything not in the catalogue. See docs/retrieval.md.
        fields["claims_fulltext_index"] = "_ftindex:yes" in upstream["tags"]
    fields |= {"category": category, "filename": filename}
    if date:
        fields["version"] = date
    fields |= {"size_bytes": p.size_bytes, "sha256": p.sha256, "urls": [url],
               "mirrors": mirrors_for(url, p.magnet)}
    if p.torrent:
        fields["torrent"] = p.torrent
    if p.magnet:
        fields["magnet"] = p.magnet
    fields["license"] = {"name": license, "attribution": attribution}
    text = HEADER + RECIPE + recipe(url, category, id, title) + "\n" + toml_lines(fields)
    return id, text


def sync_source(url: str, category: str, id: str | None = None, title: str | None = None,
                license: str = "CC-BY-SA-4.0", attribution: str = "Kiwix and contributors",
                sources_dir: Path | None = None, get: Fetch = fetch,
                say: Callable[[str], None] = print) -> Path:
    """Write (or rewrite) catalog/sources/<id>.toml and return where."""
    id, text = source_entry(url, category, id, title, license, attribution, get, say)
    target = (sources_dir or catalog.CATALOG_DIR / "sources") / f"{id}.toml"
    # Unix line endings on every system, as the repository keeps them: on
    # Windows write_text would turn each one into CRLF.
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def check(sources_dir: Path | None = None, get: Fetch = fetch, say: Callable[[str], None] = print) -> int:
    """Look upstream for newer dated releases of every catalogue source that
    came from a URL. Returns how many need attention."""
    listings: dict[str, str] = {}
    current = stale = 0
    for path in sorted((sources_dir or catalog.CATALOG_DIR / "sources").glob("*.toml")):
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        if not data.get("urls"):
            continue  # added offline, from the file itself: nowhere to look
        url = data["urls"][0]
        folder = url[: url.rindex("/") + 1]
        if folder not in listings:
            try:
                listings[folder] = get(folder, "GET")
            except Exception as e:
                say(f"  ERROR    {data['id']}  could not list {folder}: {e}")
                stale += 1
                continue
        stem, _ = parse_zim_name(data["filename"])
        found = re.findall(rf"{re.escape(stem)}_(\d{{4}}-\d{{2}})\.zim(?!\.)", listings[folder])
        latest = max(found, default=None)
        have = data.get("version")
        if latest and have and latest > have:
            say(f"  STALE    {data['id']}  have {have}, upstream has {latest}")
            say(f"           {_recipe_for(text, url, f'{folder}{stem}_{latest}.zim', data)}")
            stale += 1
        elif not latest:
            say(f"  GONE?    {data['id']}  no matching file found at {folder}")
            stale += 1
        else:
            say(f"  current  {data['id']}  {have}")
            current += 1
    say(f"\n{current} current, {stale} needing attention")
    return stale


def _recipe_for(text: str, old_url: str, new_url: str, data: dict) -> str:
    """The entry's own recipe pointed at the newer file, so its --id and
    --title carry over."""
    for line in text.splitlines():
        if line.startswith(RECIPE):
            return line[len(RECIPE):].replace(old_url, new_url)
    return recipe(new_url, data["category"], data["id"], None)

