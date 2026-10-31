"""Adding an archive someone copied into the library to the catalogue.

  python -m museion add-source ../library/wikipedia_en_chemistry_nopic_2026-07.zim
      --title "Chemistry by Wikipedia" --category reference

Asked to do this by hand, a coding agent on the local model got the size and
checksum right but copied the generated-file comments from another source,
made up download links and a magnet whose hash was the start of the SHA-256,
and invented a title. This writes the entry from the file itself, with only
what can be known offline: nothing to download from, nothing to check later
that was never true.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from museion import catalog, disk

# The shelves the Library tab has headings for (museion/ui/library.js).
CATEGORIES = ("survival", "medicine", "making", "reference")


def source_id(filename: str) -> str:
    """The catalogue id for an archive: its name without the date, as the
    other sources have it. "wikipedia_en_chemistry_nopic_2026-07.zim" is
    "wikipedia_en_chemistry_nopic"."""
    stem = filename.removesuffix(".zim")
    return re.sub(r"_\d{4}-\d{2}$", "", stem)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def toml_value(value) -> str:
    """One value as TOML, in the shapes the catalogue files use: a list one
    item to a line, a table inline."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # A JSON string is a TOML basic string, escapes and all, as long as
        # nothing outside the ASCII range is escaped: JSON writes an emoji as
        # two \u escapes, which TOML refuses. DEL is the one character TOML
        # wants escaped that JSON leaves alone.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[\n" + "".join(f"  {toml_value(v)},\n" for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"no TOML for {value!r}")


def toml_lines(fields: dict) -> str:
    """A catalogue entry: one `key = value` per field, in the order given."""
    return "".join(f"{k} = {toml_value(v)}\n" for k, v in fields.items())


def source_toml(path: Path, title: str, category: str, description: str = "") -> str:
    """The catalogue entry for one archive file, as TOML."""
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}, not {category!r}")
    if not title.strip():
        raise ValueError("the title is what the Library tab shows, so it cannot be empty")
    return "# Added from the file itself, offline: there is nowhere recorded to download it from.\n" + toml_lines({
        "id": source_id(path.name),
        "title": title.strip(),
        "description": description.strip(),
        "category": category,
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_of(path),
        "urls": [],
    })


def add_source(path: Path, title: str, category: str, description: str = "",
               sources_dir: Path | None = None) -> Path:
    """Write the entry into catalog/sources/ and return where. An id that is
    already there is refused, so an existing source, with its real download
    links, is never overwritten."""
    path = Path(path)
    if path.suffix != ".zim" or not path.is_file():
        raise ValueError(f"{path} is not a .zim archive")
    folder = sources_dir or catalog.CATALOG_DIR / "sources"
    target = folder / f"{source_id(path.name)}.toml"
    if target.exists():
        raise ValueError(f"{target.name} is already in the catalogue")
    text = source_toml(path, title, category, description)
    # Unix line endings on every system, as the other entries have them.
    disk.write_text(target, text)
    return target
