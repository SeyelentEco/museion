"""A small library for the index tests, and the record of what the first
version of Museion made of it.

The library has a PDF-collection archive served by kiwix-serve (here, by
tests/fake_kiwix.py), an archive with its own full-text index, and a folder of
loose PDFs, with a duplicate, an image-only scan and two documents that cannot
be read among them. tests/fixtures/old-index.db is the index the first
version built over it, and tests/fixtures/old-search.json is what its search
returned for each of QUERIES on that database. The index must open that file
and return the same hits in the same order, and the indexer must build the
same rows from the same library.

The fixtures are a record, not something to rebuild: the code that made them
is gone, and they are what proves an index a person built with the first
version still works. If the index is
changed on purpose so that they no longer match, say so in the change, and
keep a test that opens the old file.
"""

import textwrap
from pathlib import Path

from museion.pdf import sample_pdf

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
OLD_DB = FIXTURES / "old-index.db"
OLD_SEARCH = FIXTURES / "old-search.json"

ARCHIVE = "zimgit-water_en_2024-01"
NATIVE = "wikipedia_en_chemistry_nopic_2026-07"

STORAGE = [
    "Store at least four litres of water per person per day for drinking and sanitation, "
    "in clean food grade containers kept away from sunlight, fuel and garden chemicals.",
    "Replace stored water every six months, and treat it with bleach before drinking if the "
    "container seal was ever broken or the water smells stale or looks cloudy.",
]

# The archive's documents, by filename, as its database.js lists them. The
# third is listed but missing from the archive, so fetching it fails.
ARCHIVE_PDFS = {
    "Emergency Preparedness (1).pdf": STORAGE,
    "Emergency Preparedness (2).pdf": [
        "Signal for rescue with three fires in a triangle, three whistle blasts or a mirror "
        "flash aimed at aircraft; rescuers everywhere read groups of three as a call for help.",
    ],
}

DATABASE_JS = """var DATABASE = [
{'_id': 'a1', 'ti': 'Don\\'t Drink the Water', 'dsc': 'Storing and treating water at home', 'aut': 'FEMA', 'fp': ['Emergency Preparedness (1).pdf', 'cover.jpg']},
{'_id': 'a2', 'ti': '', 'dsc': '', 'aut': '', 'fp': ['Emergency Preparedness (2).pdf']},
{'_id': 'a3', 'ti': 'Missing From The Archive', 'dsc': 'Listed but not present', 'aut': 'Nobody', 'fp': ['Emergency Preparedness (3).pdf']}
];
"""

# Loose PDFs in the folder "found", one page per string.
FOLDER_PDFS = {
    "Field_First_Aid.pdf": [
        "Contents Bleeding 4 Burns 9 Fractures 12 Shock 15 Wounds 18 Water 21 Canning 24 "
        "Altitude 27 Index 30 Splints 33 Bandages 36 Eyes 39",
        "To stop bleeding from a deep wound press firmly on it with a clean cloth and keep "
        "pressing for at least ten minutes without lifting to look. Raise the injured limb.",
        "A wound that will not close or that shows spreading redness, heat and pus is infected. "
        "Clean it twice a day with boiled water and seek help if a fever develops.",
    ],
    "Home_Canning.pdf": [
        "Canning green beans safely needs a pressure canner, because beans are a low acid food "
        "and boiling water cannot destroy the spores that cause botulism poisoning.",
        "Pack the beans into hot jars leaving one inch of headspace, add boiling water and "
        "process pints for twenty minutes at ten pounds of pressure, adjusting for altitude.",
        "Altitude adjustment for the pressure canner: up to 1000 feet use 10 pounds, 1001 to "
        "2000 feet use 11 pounds, 2001 to 4000 feet use 12 pounds, above that use 13 pounds.",
    ],
    "Scanned_Map.pdf": ["map", "legend"],
    "Water_Storage_copy.pdf": STORAGE,
    "Water_Treatment-WHO.pdf": [
        "Water Treatment Guide. This guide explains how households can make drinking water safe "
        "when the normal supply has failed after a disaster or during a long outage.",
        "To disinfect clear water add two drops of unscented household bleach per litre, stir "
        "well and let it stand for thirty minutes before drinking. Cloudy water needs four drops.",
        "Boiling is the most reliable method. Bring the water to a rolling boil for one full "
        "minute, or three minutes above two thousand metres of altitude, then let it cool.",
    ],
}

NOT_A_PDF = b"%PDF-1.4 this is not really a PDF"


def pdf(pages: list[str]) -> bytes:
    """A PDF with one page per string.

    sample_pdf writes each page as a single line of text, and pdfium does not
    extract text that runs off the edge of the page, so a page long enough to
    count as real text (20 words) would lose most of its words. Each page is
    wrapped onto several lines here instead, by ending the line's text and
    moving down between them in the page's drawing instructions."""
    return sample_pdf([") Tj 0 -16 Td (".join(textwrap.wrap(p, 80, break_on_hyphens=False))
                       for p in pages])

# (question, limit) pairs, chosen to exercise each part of search: the gate,
# prefix coverage, stemming, demotion of a contents page, punctuation and a
# question of nothing but stopwords.
QUERIES = [
    ("how do I disinfect water with bleach?", 5),
    ("how long should I boil water at altitude", 5),
    ("how do I stop bleeding from a deep wound", 3),
    ("we have a lot of green beans and no fridge, how do I can them so nobody gets botulism", 5),
    ("what caused the Bronze Age collapse", 5),
    ("wound that won't close (deep) NOT healing", 5),
    ("bleeding burns fractures shock", 5),
    ("storing water", 10),
    ("signal for rescue", 5),
    ("what is it?", 5),
]


def write_library(root: Path) -> tuple[Path, Path]:
    """Write the library under `root`. Returns the library folder (archives,
    and a stray PDF that must not be indexed) and the folder of loose PDFs."""
    library = root / "library"
    found = root / "found"
    library.mkdir()
    found.mkdir()
    (library / f"{ARCHIVE}.zim").write_bytes(b"")
    (library / f"{NATIVE}.zim").write_bytes(b"")
    (library / "stray.pdf").write_bytes(pdf(FOLDER_PDFS["Home_Canning.pdf"]))
    for name, pages in FOLDER_PDFS.items():
        (found / name).write_bytes(pdf(pages))
    (found / "Broken.pdf").write_bytes(NOT_A_PDF)
    return library, found


def kiwix_entries() -> dict[str, bytes]:
    """What the fake kiwix-serve serves for the archives in the library."""
    entries = {f"{ARCHIVE}/database.js": DATABASE_JS.encode("utf-8")}
    for name, pages in ARCHIVE_PDFS.items():
        entries[f"{ARCHIVE}/files/{name}"] = pdf(pages)
    return entries

