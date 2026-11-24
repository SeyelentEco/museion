"""Stand-in copies of Museion for the upgrade tests: a folder laid out as a
drive or a download is, small enough to make by the dozen."""

import io
import json
import zipfile
from pathlib import Path

from museion import catalog
from museion.portable import MARKER

CODE = {
    "museion/__init__.py": "",
    "museion/ask.py": "def ask():\n    return 'old'\n\n\ndef cite():\n    return 1\n",
    "museion/tuning.toml": "passages = 8\nlength = 400\n",
    "AGENTS.md": "# Working on Museion\n",
    "tests/test_ask.py": "",
}


def code(version: str, changes: dict[str, str | None] | None = None) -> dict[str, str]:
    """A version's code: CODE with its version number, and some files
    changed or (with None) removed."""
    files = dict(CODE, **{"museion/version.py": f'VERSION = "{version}"\n'})
    for path, text in (changes or {}).items():
        if text is None:
            files.pop(path, None)
        else:
            files[path] = text
    return files


def make_copy(root: Path, version: str, files: dict[str, str] | None = None, *,
              python: str = "py-1", helpers: dict[str, str] | None = None, platform: str | None = None) -> Path:
    """A whole copy at `root`: marker, app/ with its record of what was
    shipped, a Python, helper programs, and a person's library and settings."""
    files = code(version) if files is None else files
    helpers = {"llama.cpp/cpu": "llama-1", "kiwix-tools": "kiwix-1"} if helpers is None else helpers
    root.mkdir(parents=True)
    (root / MARKER).write_text("drive-1\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, text in files.items():
            write(root / "app" / path, text)
            z.writestr(f"app/{path}", text)
    write(root / "support" / "museion-app-original.zip", buf.getvalue())
    write(root / "support" / "package.json", json.dumps({
        "version": version, "platform": platform or catalog.current_platform(), "arch": catalog.current_arch(),
        "helpers": {"python": python, **helpers}}))
    write(root / "python" / "bin" / "python3", f"the interpreter {python}")
    for name, sha in helpers.items():
        folder = root / "bin" / "llama" / name.split("/")[1] if "/" in name else root / "bin"
        write(folder / (name.split("/")[0] + "-program"), f"the program {sha}")
    write(root / "library" / "wikipedia.zim", "an archive")
    write(root / "library" / "notes" / "stores.csv", "item,count\nrice,20\n")
    write(root / "models" / "chat.gguf", "a model")
    write(root / "config.json", '{"library_dir": "library"}')
    write(root / "tuning.toml", "passages = 12\n")
    write(root / "agents" / "mine" / "prompt.md", "my own agent")
    return root


def write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def tree(root: Path, *skip: str) -> dict[str, bytes]:
    """Every file under a folder, to compare a copy before and after."""
    return {f.relative_to(root).as_posix(): f.read_bytes() for f in sorted(root.rglob("*"))
            if f.is_file() and not any(f.relative_to(root).as_posix().startswith(s) for s in skip)}
