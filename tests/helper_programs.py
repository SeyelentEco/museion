"""Where the tests find the helper programs Museion itself uses.

The tests that read real PDFs need pdfium. They used to run only when
MUSEION_PDFIUM named its folder, so on a drive -- where someone changing the
indexer runs them, and where nothing sets that -- they skipped themselves,
and a bug in the indexer passed every test that ran.
"""

import os
from pathlib import Path

from museion import config, pdf, portable


def pdfium_dir() -> str | None:
    """MUSEION_PDFIUM if it is set, else the folder Museion loads pdfium
    from: a drive's bin/, or the pdfium folder in this computer's settings.
    None when there is none, and the tests that need it skip."""
    if os.environ.get("MUSEION_PDFIUM"):
        return os.environ["MUSEION_PDFIUM"]
    drive = portable.detect()
    if drive:
        folder: Path | None = drive.root / "bin"
    else:
        folder = config.load(config.config_dir() / "config.json").pdfium_dir
    if folder is None:
        return None
    try:
        pdf.find_library(folder)
    except FileNotFoundError:
        return None
    return str(folder)
