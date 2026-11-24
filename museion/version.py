"""Which Museion this is.

One number, here and nowhere else: the page shows it beside the name,
`python -m museion version` prints it, a release's files are named with it,
and an upgrade compares it with the number in the package it is offered.
The release is made by pushing the tag v<VERSION>; the release job refuses a
tag that does not match. CHANGELOG.md says what each version changed.
"""

VERSION = "0.1.0"


def parse(text: str) -> tuple[int, ...]:
    """A version as numbers, for comparing: "0.10.2" is newer than "0.9".
    Anything after the numbers ("-rc1") is ignored; text that is not a
    version at all is older than every version."""
    out: list[int] = []
    for part in text.strip().lstrip("v").split("."):
        digits = ""
        for ch in part:
            if not (ch.isascii() and ch.isdigit()):
                break
            digits += ch
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def newer(a: str, b: str) -> bool:
    """Is version `a` newer than version `b`?"""
    return parse(a) > parse(b)


def notes(changelog: str, version: str = VERSION) -> str:
    """The section of CHANGELOG.md for one version, without its heading: the
    text of that version's release. Empty if the version has no section."""
    out: list[str] = []
    inside = False
    for line in changelog.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip() == version
        elif inside:
            out.append(line)
    return "\n".join(out).strip()
