"""Talking to the kiwix-serve sidecar.

All archive content is read over HTTP rather than through a ZIM library
binding. That is not laziness: `zimdump dump` on Windows writes extracted
files in text mode, turning every 0x0A into 0x0D 0x0A, which silently
corrupts every PDF it touches (a 117,197-byte file comes out at 118,056 and
every PDF parser then reports a zlib error that looks exactly like a damaged
source document). The HTTP route returns byte-exact data, and Museion already
supervises kiwix-serve for the browse UI.
"""

from __future__ import annotations

import http.client
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass
class CatalogedDoc:
    """One document inside a PDF-collection archive, as described by its
    database.js."""

    file: str
    title: str
    description: str
    author: str


# What shape an archive is, which decides how it gets indexed. See classify().
#
# A zimgit-style bundle: a JavaScript viewer wrapped around PDF documents,
# described by database.js. Needs a locally built index.
PDF_COLLECTION = "pdf_collection"
# An mwoffliner-style archive (Wikipedia and friends) whose own full-text
# index works. Queried through kiwix-serve directly, nothing to build.
FULL_TEXT_INDEXED = "full_text_indexed"


@dataclass
class ArchiveKind:
    kind: str  # PDF_COLLECTION or FULL_TEXT_INDEXED
    docs: list[CatalogedDoc]  # for PDF_COLLECTION: the documents inside it


@dataclass
class SearchHit:
    title: str
    link: str
    snippet: str

    def path(self) -> str | None:
        """The entry path inside its book, from a link like
        `/content/<book>/<path>`."""
        if not self.link.startswith("/content/"):
            return None
        rest = self.link[len("/content/"):]
        if "/" not in rest:
            return None
        return rest.split("/", 1)[1]


# Everything a request can fail with: no server or an HTTP error status
# (OSError), a malformed URL (ValueError), or a connection that broke off
# mid-reply (HTTPException).
_HTTP_ERRORS = (OSError, ValueError, http.client.HTTPException)


class Kiwix:
    def __init__(self, base: str, timeout: float = 180.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def _get(self, url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=self.timeout) as res:
            return res.read()

    def is_up(self) -> bool:
        try:
            self._get(self.url("catalog/v2/entries"))
            return True
        except _HTTP_ERRORS:
            return False

    def content(self, book: str, path: str) -> bytes:
        """Raw bytes of one entry. `path` is percent-encoded here, not by the
        caller."""
        url = self.url(f"content/{book}/{encode_path(path)}")
        try:
            return self._get(url)
        except _HTTP_ERRORS as e:
            raise ConnectionError(f"fetching {url}: {e}") from e

    def classify(self, book: str) -> ArchiveKind:
        """Decide how an archive must be indexed by probing it.

        Detection is behavioural rather than name-based so that archives
        which do not exist yet are still classified correctly."""
        try:
            raw = self.content(book, "database.js")
        except ConnectionError:
            return ArchiveKind(FULL_TEXT_INDEXED, [])
        docs = parse_database_js(raw.decode("utf-8", errors="replace"))
        if not docs:
            return ArchiveKind(FULL_TEXT_INDEXED, [])
        return ArchiveKind(PDF_COLLECTION, docs)

    def search(self, book: str, pattern: str, limit: int) -> list[SearchHit]:
        """Full-text search through the archive's own index. Used for
        archives classified as FULL_TEXT_INDEXED."""
        url = self.url(
            f"search?books.name={encode_component(book)}"
            f"&pattern={encode_component(pattern)}&format=xml&pageLength={limit}"
        )
        try:
            body = self._get(url)
        except _HTTP_ERRORS as e:
            raise ConnectionError(f"searching {book}: {e}") from e
        return parse_opensearch(body.decode("utf-8", errors="replace"))

    def article_text(self, book: str, path: str) -> str:
        """An article's readable text, for archives whose own index works.

        Retrieval used to search only the locally built PDF index, so
        Wikipedia and the like were browsable but never askable. This is the
        other half: kiwix-serve finds the article, this reads it."""
        html = self.content(book, path).decode("utf-8", errors="replace")
        return html_to_text(html)


_DROP = re.compile(
    r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>|<nav[^>]*>.*?</nav>"
    r"|<header[^>]*>.*?</header>|<footer[^>]*>.*?</footer>|<table[^>]*>.*?</table>",
    re.IGNORECASE | re.DOTALL,
)
_BODY = re.compile(r"<body[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)
_BLOCK = re.compile(r"</?(p|div|br|li|h[1-6]|tr|section|article)[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """Strip an article down to its prose. Scripts, styles, navigation boxes
    and tags go; entities are unescaped; whitespace collapses."""
    m = _BODY.search(html)
    inner = m.group(1) if m else html
    s = _DROP.sub(" ", inner)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = _unescape(s).replace("&nbsp;", " ").replace("&#160;", " ")
    lines = (" ".join(line.split()) for line in s.split("\n"))
    return "\n".join(line for line in lines if line)


_ITEM = re.compile(r"<item>(.*?)</item>", re.DOTALL)


def parse_opensearch(xml: str) -> list[SearchHit]:
    """The hits in kiwix-serve's OpenSearch XML results."""

    def field(item: str, tag: str) -> str:
        m = re.search(f"<{tag}>(.*?)</{tag}>", item, re.DOTALL)
        return _strip_tags(_unescape(m.group(1))) if m else ""

    return [
        SearchHit(title=field(item, "title"), link=field(item, "link"),
                  snippet=field(item, "description"))
        for item in _ITEM.findall(xml)
    ]


_RECORD = re.compile(r"\{'_id':.*?\}", re.DOTALL)
_FILES = re.compile(r"'fp':\s*\[([^\]]*)\]")
_QUOTED = re.compile(r"'((?:[^'\\]|\\.)*)'")


def parse_database_js(src: str) -> list[CatalogedDoc]:
    """Pull title/description/author/filename out of a zimgit database.js.

    The file is a JavaScript array literal using single quotes, not JSON, so
    it is scraped rather than parsed. The PDF filenames inside these archives
    are useless on their own -- "Emergency Preparedness (1).pdf" through
    "(108).pdf" -- so this is the only source of real titles."""

    def field(rec: str, name: str) -> str:
        m = re.search(rf"'{name}':\s*'((?:[^'\\]|\\.)*)'", rec)
        return m.group(1).replace("\\'", "'").strip() if m else ""

    out = []
    for rec in _RECORD.findall(src):
        title = field(rec, "ti")
        description = field(rec, "dsc")
        author = field(rec, "aut")
        files = _FILES.search(rec)
        if not files:
            continue
        for quoted in _QUOTED.findall(files.group(1)):
            file = quoted.replace("\\'", "'")
            if file.lower().endswith(".pdf"):
                out.append(CatalogedDoc(file=file, title=title, description=description, author=author))
    return out


def _strip_tags(s: str) -> str:
    return " ".join(re.sub(r"<[^>]*>", "", s).split())


def _unescape(s: str) -> str:
    # &amp; last, so "&amp;lt;" becomes "&lt;" and not "<".
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&#39;", "'").replace("&apos;", "'").replace("&amp;", "&"))


def encode_path(path: str) -> str:
    """Percent-encode a path, keeping the separators intact."""
    return "/".join(encode_component(part) for part in path.split("/"))


def encode_component(s: str) -> str:
    """Percent-encode everything but A-Z a-z 0-9 - _ . ~ (with safe="",
    those are exactly the characters quote() leaves alone)."""
    return urllib.parse.quote(s, safe="")


def book_id_from_filename(filename: str) -> str:
    """Book id as kiwix-serve exposes it on /content/, derived from the
    filename."""
    if not filename.endswith(".zim"):
        raise ValueError(f"{filename} is not a .zim file")
    return filename[: -len(".zim")]
