"""Preparing a drive: everything Museion needs, laid out to run from wherever
the drive comes up, on a computer with nothing installed (see
docs/portable.md and portable.py for the layout).

Reuses the pieces that exist -- the catalogue, the verified downloader, the
tool installer, the indexer -- and adds only the layout, the choice of what
to carry, and the record of what was carried. Every download is resumable
and hash-checked, so a drive can be prepared over several sittings and a
half-written file is never mistaken for a whole one. A run over a finished
drive touches nothing it does not have to.

Beyond what the Rust version carried, a drive now holds Museion's own code in
app/, the portable Python that runs it, and opencode, so whoever has the
drive can change the program as well as use it.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from museion import catalog, disk, download, indexer, launchers, manifest, tools
from museion.portable import APP_DIR, MARKER

# Port a temporary kiwix-serve uses while the index is built on the drive,
# well away from the running app's.
INDEX_PORT = 8095

# A gigabyte of headroom for the index and whatever the app writes.
HEADROOM = 1 << 30

# What of this program goes on the drive, under app/: the code, the data it
# reads, and what someone changing it needs -- the tests, the docs, the notes
# for the coding agent. A new folder at the top of the repository belongs here
# too, or drives and releases go out without it (the tests check).
APP_PARTS = ["museion", "agents", "catalog", "models", "tools", "drive", "evals", "tests", "docs",
             "AGENTS.md", "README.md", "CHANGELOG.md", "LICENSE"]

# A pristine copy of app/, for undoing every change at once.
ORIGINAL_ZIP = "museion-app-original.zip"

# What a Progress is.
PHASE = "phase"        # a new stage begins; `item` names it
DOWNLOAD = "download"  # bytes arriving, or documents indexed
NOTE = "note"          # something done or skipped


@dataclass
class Progress:
    kind: str
    item: str
    received: int = 0
    total: int = 0


@dataclass
class Plan:
    dest: Path
    # Bundle id from the catalogue.
    bundle: str = "go-bag"
    # Chat model ids from the catalogue. The reranker is always carried.
    models: list[str] = field(default_factory=list)
    # llama.cpp builds to carry: cuda, vulkan, cpu, metal.
    accelerators: list[str] = field(default_factory=lambda: default_accelerators())
    build_index: bool = True
    # Everything except content and models. The first launch from it shows
    # the first-run screen and fetches the bundle and a model into the drive.
    skeleton: bool = False
    # The portable Python, so Museion runs with nothing installed.
    python: bool = True
    # opencode, so Museion can be changed with its own model.
    opencode: bool = True


@dataclass
class Estimate:
    bytes: int
    # Bytes still to fetch, allowing for what is already in place.
    remaining: int
    free_bytes: int
    items: list[tuple[str, int]]


def default_accelerators() -> list[str]:
    """Every llama.cpp build the catalogue has for this system: a drive may
    be plugged into a computer with any kind of graphics card."""
    if sys.platform == "darwin":
        return ["metal"]
    # llama.cpp publishes no CUDA build for Linux.
    return ["cuda", "vulkan", "cpu"] if sys.platform == "win32" else ["vulkan", "cpu"]


def exe(stem: str) -> str:
    return f"{stem}.exe" if sys.platform == "win32" else stem


def python_exe(root: Path) -> Path:
    """The drive's Python, as its archive lays it out."""
    return root / "python" / ("python.exe" if sys.platform == "win32" else "bin/python3")


# ── what goes on the drive ───────────────────────────────────────────────────

def estimate(plan: Plan) -> Estimate:
    """What the drive will hold and whether it fits."""
    root = Path(plan.dest)
    items: list[tuple[str, int]] = []
    remaining = 0

    def add(name: str, size: int, present: bool) -> None:
        nonlocal remaining
        items.append((name, size))
        if not present:
            remaining += size

    _, sources = catalog.bundle(plan.bundle)
    for s in [] if plan.skeleton else sources:
        add(s.title, s.size_bytes, has_size(root / "library" / s.filename, s.size_bytes))
    for m in [] if plan.skeleton else selected_models(plan):
        add(m.title, m.size_bytes, has_size(root / "models" / m.filename, m.size_bytes))
    for t in catalog.tools():
        if t.name == "llama.cpp":
            for a in plan.accelerators:
                there = tools.find_exe(root / "bin" / "llama" / a, "llama-server") is not None
                picked = catalog.tool_variant_named(t, a)
                if picked is None and not there:
                    raise ValueError(f"no llama.cpp build for {a!r} on this platform")
                size = 0 if picked is None else picked[1].size_bytes + sum(
                    t.variants[k].size_bytes for k in picked[1].also if k in t.variants)
                add(f"llama.cpp ({a})", size, there)
        elif (picked := catalog.tool_variant_named(t, "cpu")) is not None:
            add(t.name, picked[1].size_bytes, tools.find_exe(root / "bin", "kiwix-serve") is not None)
    for t in catalog.drive_tools():
        if not carried(plan, t):
            continue
        picked = catalog.tool_variant_named(t, "cpu")
        if picked is None:
            raise ValueError(f"no {t.name} build for this platform")
        add(t.name, picked[1].size_bytes, drive_tool_present(root, t))
    add("Museion's code", app_size(), (root / "app").is_dir())
    total = sum(size for _, size in items)
    return Estimate(total, remaining, disk.free_space(nearest_existing(root)), items)


def has_size(path: Path, size: int) -> bool:
    try:
        return path.stat().st_size == size
    except OSError:
        return False


def nearest_existing(p: Path) -> Path:
    p = Path(p).absolute()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def selected_models(plan: Plan) -> list[catalog.Model]:
    """The chat models named in the plan, checked against the catalogue,
    and the reranker."""
    everything = catalog.models()
    out = []
    for model_id in plan.models:
        m = next((m for m in everything if m.id == model_id), None)
        if m is None:
            raise ValueError(f"no model {model_id!r} in the catalogue")
        if m.role != "chat":
            raise ValueError(f"{model_id} is not a chat model")
        out.append(m)
    if not out and not plan.skeleton:
        raise ValueError("choose at least one chat model")
    reranker = catalog.reranker_model()
    return out + ([reranker] if reranker else [])


def carried(plan: Plan, tool: catalog.Tool) -> bool:
    """Whether the plan carries one of drive/'s programs. ripgrep goes with
    opencode, whose search runs it."""
    return plan.opencode if tool.id == "ripgrep" else bool(getattr(plan, tool.id, False))


def drive_tool_present(root: Path, tool: catalog.Tool) -> bool:
    if tool.id == "python":
        return python_exe(root).exists()
    return tools.find_exe(root / "bin", tool.provides[0]) is not None


def app_parts(source: Path = APP_DIR) -> list[Path]:
    return [source / p for p in APP_PARTS if (source / p).exists()]


def app_files(source: Path = APP_DIR):
    """Every file of the program that goes on the drive, without the caches
    Python leaves behind."""
    for part in app_parts(source):
        files = [part] if part.is_file() else sorted(part.rglob("*"))
        for f in files:
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                yield f


def app_size() -> int:
    return sum(f.stat().st_size for f in app_files())


# ── building it ──────────────────────────────────────────────────────────────

def run(plan: Plan, report: Callable[[Progress], None] = lambda p: None) -> Path:
    """Build the drive. Returns the root. Safe to run again: everything
    already in place and correct is skipped."""
    root = Path(plan.dest)
    est = estimate(plan)
    if est.free_bytes < est.remaining + HEADROOM:
        raise RuntimeError(f"{root} needs {human(est.remaining + HEADROOM)} more; "
                           f"{nearest_existing(root)} has {human(est.free_bytes)} free")

    report(Progress(PHASE, "Laying out the drive"))
    library, models, bin_dir, support = root / "library", root / "models", root / "bin", root / "support"
    for d in (library, models, bin_dir, support / "LICENSES"):
        d.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if not marker.exists():
        marker.write_text(f"drive-{secrets.token_hex(6)}\n", encoding="utf-8")

    report(Progress(PHASE, "Helper programs"))

    def on_tool(s: tools.Step) -> None:
        if s.kind == tools.DOWNLOADING:
            report(Progress(DOWNLOAD, f"{s.tool}: {s.file}", s.received, s.total))
        else:
            report(Progress(NOTE, {tools.UNPACKING: f"Unpacking {s.tool}", tools.DONE: f"{s.tool} ready"}
                            .get(s.kind, f"{s.tool}: {s.reason}")))

    # Each helper is unpacked beside where it goes and moved in only once
    # all of it is there, since its program being present is what counts as
    # installed. Unpacked in place, a drive pulled out between a CUDA build
    # and its runtime counted as done, and the chat model never started.
    if tools.find_exe(bin_dir, "kiwix-serve") is None:
        part = fresh(bin_dir / ".kiwix.part")
        tools.install_selected("cpu", part, ["kiwix-tools", "pdfium"], on_tool)
        server = tools.find_exe(part, "kiwix-serve")
        move_in(part, bin_dir, last=server.name if server else "")
    for a in plan.accelerators:
        folder = bin_dir / "llama" / a
        if tools.find_exe(folder, "llama-server") is None:
            part = fresh(folder.with_name(f"{a}.part"))
            tools.install_selected(a, part, ["llama.cpp"], on_tool)
            shutil.rmtree(folder, ignore_errors=True)
            part.rename(folder)
    for t in catalog.drive_tools():
        if carried(plan, t) and not drive_tool_present(root, t):
            install_drive_tool(t, root, report)

    report(Progress(PHASE, "Models"))
    chosen = selected_models(plan)
    for m in [] if plan.skeleton else chosen:
        fetch_verified(m.title, m.urls, models / m.filename, m.size_bytes, m.sha256, report)

    report(Progress(PHASE, "Archives"))
    bundle, sources = catalog.bundle(plan.bundle)
    for s in [] if plan.skeleton else sources:
        fetch_verified(s.title, s.all_urls(), library / s.filename, s.size_bytes, s.sha256, report)

    report(Progress(PHASE, "Museion itself"))
    copy_app(root, report)
    launchers.write_all(root)

    report(Progress(PHASE, "Configuration"))
    if not (root / "config.json").exists():
        write_config(root, plan, chosen)
    write_licenses(support / "LICENSES", sources, chosen)
    launchers.write_readme(root, bundle.title, plan.skeleton)

    if plan.build_index and not plan.skeleton:
        report(Progress(PHASE, "Building the index"))
        build_index(library, bin_dir, report)

    report(Progress(PHASE, "Verifying"))
    manifest.write(root, plan.bundle, plan.accelerators, [] if plan.skeleton else [*sources, *chosen])
    report(Progress(NOTE, "Done."))
    return root


def fetch_verified(title: str, urls: list[str], target: Path, size: int, sha256: str,
                   report: Callable[[Progress], None]) -> None:
    if download.is_verified(target, size, sha256):
        report(Progress(NOTE, f"{title} already there"))
        return
    download.download_verified(download.Spec(urls, target, size, sha256),
                               lambda p: report(Progress(DOWNLOAD, title, p.received, p.total)))


def install_drive_tool(tool: catalog.Tool, root: Path, report: Callable[[Progress], None]) -> None:
    """The portable Python into python/, or opencode or ripgrep into bin/."""
    picked = catalog.tool_variant_named(tool, "cpu")
    if picked is None:
        raise ValueError(f"no {tool.name} build for this platform")
    v = picked[1]
    with tempfile.TemporaryDirectory(prefix="museion-drive-") as tmp:
        staging = Path(tmp)
        archive = staging / v.file
        fetch_verified(tool.name, [v.url], archive, v.size_bytes, v.sha256, report)
        report(Progress(NOTE, f"Unpacking {tool.name}"))
        unpacked = staging / "unpacked"
        tools.extract(archive, unpacked)
        # Copied to the drive under a temporary name, then renamed: a copy cut
        # off partway must not pass for the real thing.
        if tool.id == "python":
            # The archive holds one folder, python/. It is moved to a name
            # that does not exist yet: moved into a folder, it lands inside.
            part = root / "python.part"
            shutil.rmtree(part, ignore_errors=True)
            shutil.move(str(unpacked / "python"), str(part))
            shutil.rmtree(root / "python", ignore_errors=True)
            part.rename(root / "python")
        else:
            name = exe(tool.provides[0])
            program = next((p for p in unpacked.rglob(name) if p.is_file()), None)
            if program is None:
                raise RuntimeError(f"no {name} in {v.file}")
            target = root / "bin" / program.name
            part = target.with_name(target.name + ".part")
            shutil.move(str(program), str(part))
            part.chmod(0o755)
            part.replace(target)
    report(Progress(NOTE, f"{tool.name} ready"))


def fresh(folder: Path) -> Path:
    """An empty folder, emptied of what an interrupted run left there."""
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    return folder


def move_in(part: Path, into: Path, last: str) -> None:
    """Move what was unpacked in `part` into `into`, the file named `last`
    after everything else, then remove `part`."""
    for entry in sorted(part.iterdir(), key=lambda e: e.name == last):
        target = into / entry.name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        entry.replace(target)
    part.rmdir()


def copy_app(root: Path, report: Callable[[Progress], None]) -> None:
    """This program into app/, and a pristine zip of it into support/. An
    app/ already there is left alone: it may hold changes someone made."""
    target = root / "app"
    if target.resolve() == APP_DIR:
        report(Progress(NOTE, "Running from this drive; its code stays as it is"))
    elif target.exists():
        report(Progress(NOTE, "app/ already there; left as it is"))
    else:
        staging = root / "app.part"
        shutil.rmtree(staging, ignore_errors=True)
        for f in app_files():
            dest = staging / f.relative_to(APP_DIR)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        staging.rename(target)
        report(Progress(NOTE, "Museion's code copied to app/"))
    original = root / "support" / ORIGINAL_ZIP
    if not original.exists():
        with zipfile.ZipFile(original.with_suffix(".part"), "w", zipfile.ZIP_DEFLATED) as z:
            for f in app_files():
                z.write(f, Path("app") / f.relative_to(APP_DIR))
        original.with_suffix(".part").replace(original)


def write_config(root: Path, plan: Plan, chosen: list[catalog.Model]) -> None:
    """The drive's config: relative paths, the smallest chosen model and the
    CPU build as defaults. The first launch on any computer replaces both
    with what fits it (portable mode's machine profile)."""
    from museion.config import Config

    accel = "cpu" if "cpu" in plan.accelerators else (plan.accelerators or ["cpu"])[0]
    chats = [m for m in chosen if m.role == "chat"]
    # A skeleton names the catalogue's smallest chat model so the first run's
    # set-up knows what to fetch; nothing is on the drive yet.
    chat = min(chats or [m for m in catalog.models() if m.role == "chat"], key=lambda m: m.size_bytes)
    reranker = next((m for m in chosen if m.role != "chat"), None)
    cfg = Config(
        library_dir=root / "library",
        kiwix_exe=root / "bin" / exe("kiwix-serve"),
        llama_exe=root / "bin" / "llama" / accel / exe("llama-server"),
        pdfium_dir=root / "bin",
        models_dir=root / "models",
        chat_model=root / "models" / chat.filename,
        reranker_model=root / "models" / reranker.filename if reranker else None,
        chat_args=list(chat.server_args),
        base=root,
    )
    cfg.save(root / "config.json")


def write_licenses(folder: Path, sources: list[catalog.Source], models: list[catalog.Model]) -> None:
    def line(title: str, lic: catalog.License | None, unknown: str) -> str:
        if lic is None:
            return f"  {title} -- {unknown}\n"
        return f"  {title} -- {lic.name}{f' -- {lic.attribution}' if lic.attribution else ''}\n"

    text = "What this drive carries, and under what terms.\n\nContent\n"
    text += "".join(line(s.title, s.license, "(see the archive's own metadata)") for s in sources)
    text += "\nModels\n"
    text += "".join(line(m.title, m.license, "(see the model card)") for m in models)
    text += "\nPrograms\n"
    text += "".join(line(t.name, t.license, "(see the project)") for t in [*catalog.tools(), *catalog.drive_tools()])
    text += "\nMuseion itself -- MIT (see the end of app/README.md).\n"
    (folder / "README.txt").write_text(text, encoding="utf-8")


def build_index(library: Path, bin_dir: Path, report: Callable[[Progress], None]) -> None:
    """Build the index on the drive, with a kiwix-serve of its own for the
    duration: the indexer reads archive content over HTTP, because zimdump
    corrupts binaries on Windows."""
    kiwix = tools.find_exe(bin_dir, "kiwix-serve")
    if kiwix is None:
        raise RuntimeError(f"kiwix-serve missing from {bin_dir}")
    zims = sorted(p for p in library.iterdir() if p.suffix.lower() == ".zim")
    if not zims:
        return
    flags = 0x0800_0000 if sys.platform == "win32" else 0  # no console window
    server = subprocess.Popen([str(kiwix), "--port", str(INDEX_PORT), *map(str, zims)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        url = f"http://127.0.0.1:{INDEX_PORT}"
        started = time.monotonic()
        while True:
            try:
                urllib.request.urlopen(f"{url}/catalog/v2/entries", timeout=3).close()
                break
            except OSError:
                if server.poll() is not None:
                    raise RuntimeError("kiwix-serve exited while starting") from None
                if time.monotonic() - started > 60:
                    raise RuntimeError(f"kiwix-serve did not come up on port {INDEX_PORT}") from None
                time.sleep(0.5)

        def say(p: indexer.Progress) -> None:
            if isinstance(p, indexer.Archive):
                report(Progress(NOTE, f"Indexing {p.book}: {p.documents} documents"))
            elif isinstance(p, indexer.Document):
                report(Progress(DOWNLOAD, f"Indexing {p.book}", p.done, p.total))

        stats = indexer.index_library(library, [], library / "museion-index.db", url, bin_dir, False, say)
        report(Progress(NOTE, f"Indexed {stats.documents} documents, {stats.pages} pages, {stats.words} words"))
    finally:
        server.kill()
        server.wait()


def human(n: int) -> str:
    return f"{n / (1 << 30):.1f} GB" if n >= 1 << 30 else f"{n >> 20} MB"
