"""PDF text extraction, via Chromium's pdfium.

Extraction quality is not a detail here -- it is the product. A retrieval
system can only find what the extractor could read, so this uses the same
engine Chrome does rather than a lighter pure-Python parser that loses text on
multi-column and scanned-with-text-layer documents.

pdfium is a C library. It is called through ctypes, which is part of Python,
so nothing has to be compiled: the library file sits in the helper-programs
folder beside llama-server and kiwix-serve, and this module loads it. Only the
handful of functions below are used.

pdfium is not thread-safe, so every call into it holds one lock.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Page:
    """One page of extracted text. Pages are the natural chunk: they are what
    a citation can point at, and at roughly 265 words each in this corpus they
    are already close to the right size for a retrieval window."""

    number: int  # 1-based, as a person would say it
    text: str


@dataclass
class Extracted:
    pages: list[Page] = field(default_factory=list)
    page_count: int = 0
    word_count: int = 0

    def is_image_only(self) -> bool:
        """True when the document is essentially images -- a scan with no
        text layer. Indexing these adds nothing; they need OCR, which is out
        of scope for now, so they are recorded and reported rather than
        silently dropped."""
        return self.page_count > 0 and self.word_count / self.page_count < 20


def library_filename() -> str:
    """The name pdfium's shared library has on this operating system."""
    if sys.platform == "win32":
        return "pdfium.dll"
    if sys.platform == "darwin":
        return "libpdfium.dylib"
    return "libpdfium.so"


def find_library(lib_dir: Path) -> Path:
    """Find pdfium in a folder. The helper installer flattens every download
    into one folder, but an unflattened pdfium archive keeps the library under
    bin/ (Windows) or lib/ (macOS, Linux), so those are looked in too."""
    name = library_filename()
    for candidate in (lib_dir / name, lib_dir / "bin" / name, lib_dir / "lib" / name):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"pdfium ({name}) is not in {lib_dir}. Museion downloads it alongside "
        "its other helper programs; run setup if it is missing."
    )


class PdfExtractor:
    """Loads pdfium once and extracts text from PDFs given as bytes."""

    def __init__(self, lib_dir: Path) -> None:
        path = find_library(Path(lib_dir))
        try:
            self._lib = ctypes.CDLL(str(path))
        except OSError as e:
            raise RuntimeError(f"loading pdfium from {path}: {e}") from e
        _declare(self._lib)
        self._lock = threading.Lock()
        with self._lock:
            self._lib.FPDF_InitLibrary()

    def extract(self, data: bytes) -> Extracted:
        with self._lock:
            return self._extract_locked(data)

    def _extract_locked(self, data: bytes) -> Extracted:
        lib = self._lib
        # pdfium reads the bytes in place for as long as the document is open,
        # so the buffer must outlive the document. Keeping it in a local that
        # lives until the end of this function is enough.
        buf = ctypes.create_string_buffer(data, len(data))
        doc = lib.FPDF_LoadMemDocument64(buf, len(data), None)
        if not doc:
            code = lib.FPDF_GetLastError()
            raise ValueError(f"parsing PDF: {_ERRORS.get(code, f'pdfium error {code}')}")
        try:
            out = Extracted(page_count=lib.FPDF_GetPageCount(doc))
            for i in range(out.page_count):
                text = normalise(_page_text(lib, doc, i))
                out.word_count += len(text.split())
                if text:
                    out.pages.append(Page(number=i + 1, text=text))
            return out
        finally:
            lib.FPDF_CloseDocument(doc)


def _page_text(lib: ctypes.CDLL, doc: int, index: int) -> str:
    """All the text on one page, in pdfium's reading order. A page pdfium
    cannot open is treated as empty rather than failing the whole document,
    as the Rust version did."""
    page = lib.FPDF_LoadPage(doc, index)
    if not page:
        return ""
    try:
        text_page = lib.FPDFText_LoadPage(page)
        if not text_page:
            return ""
        try:
            count = lib.FPDFText_CountChars(text_page)
            if count <= 0:
                return ""
            # UTF-16 code units, plus room for the terminating zero pdfium
            # always writes.
            raw = ctypes.create_string_buffer((count + 1) * 2)
            written = lib.FPDFText_GetText(text_page, 0, count, raw)
            if written <= 0:
                return ""
            return raw.raw[: (written - 1) * 2].decode("utf-16-le", errors="replace")
        finally:
            lib.FPDFText_ClosePage(text_page)
    finally:
        lib.FPDF_ClosePage(page)


def normalise(raw: str) -> str:
    """Collapse the whitespace and hyphenation artefacts that PDF extraction
    leaves behind, so that indexed terms match what a person would type."""
    dehyphenated = raw.replace("-\r\n", "").replace("-\n", "")
    return " ".join(dehyphenated.split())


# What FPDF_GetLastError's numbers mean, from fpdfview.h.
_ERRORS = {
    1: "unknown error",
    2: "file not found or could not be opened",
    3: "not a PDF, or a damaged one",
    4: "password required or incorrect",
    5: "unsupported security scheme",
    6: "page not found or content error",
}


def _declare(lib: ctypes.CDLL) -> None:
    """Tell ctypes the argument and return types of the pdfium functions in
    use. Without this ctypes assumes every value is a C int, which truncates
    the 64-bit handles pdfium returns and crashes on the next call."""
    handle = ctypes.c_void_p
    lib.FPDF_InitLibrary.argtypes = []
    lib.FPDF_InitLibrary.restype = None
    lib.FPDF_GetLastError.argtypes = []
    lib.FPDF_GetLastError.restype = ctypes.c_ulong
    lib.FPDF_LoadMemDocument64.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    lib.FPDF_LoadMemDocument64.restype = handle
    lib.FPDF_CloseDocument.argtypes = [handle]
    lib.FPDF_CloseDocument.restype = None
    lib.FPDF_GetPageCount.argtypes = [handle]
    lib.FPDF_GetPageCount.restype = ctypes.c_int
    lib.FPDF_LoadPage.argtypes = [handle, ctypes.c_int]
    lib.FPDF_LoadPage.restype = handle
    lib.FPDF_ClosePage.argtypes = [handle]
    lib.FPDF_ClosePage.restype = None
    lib.FPDFText_LoadPage.argtypes = [handle]
    lib.FPDFText_LoadPage.restype = handle
    lib.FPDFText_ClosePage.argtypes = [handle]
    lib.FPDFText_ClosePage.restype = None
    lib.FPDFText_CountChars.argtypes = [handle]
    lib.FPDFText_CountChars.restype = ctypes.c_int
    lib.FPDFText_GetText.argtypes = [handle, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    lib.FPDFText_GetText.restype = ctypes.c_int


def sample_pdf(lines: list[str]) -> bytes:
    """A small, valid PDF with one page per line of text, in a standard font.

    Used by the tests and by `museion doctor` to check pdfium works without
    needing anything from the library. Only plain ASCII without parentheses
    or backslashes is supported, which is all a self-test needs."""
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(lines))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(lines)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, line in enumerate(lines):
        content = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_ids[i] + 1} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)
