"""The window's Library side: what a first run would fetch, fetching it, the
catalogue with what is already here, folders of found content, and building
the index over all of it.

Downloads report "fetch-progress" events {item, received, total, note}, and
indexing reports "index-progress" lines, so the page can show which file or
document it is on rather than a frozen button.
"""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

from museion import catalog, disk, download, hardware, indexer, own_programs, tools
from museion.appstate import AppState, UiError, local
from museion.config import Config
from museion.index import Index
from museion.services import restart_kiwix
from museion.sidecar import KIWIX

# Category order on the shelves; anything else comes last.
CATEGORY_ORDER = {"survival": 0, "medicine": 1, "making": 2}


def describe_hardware(hw: hardware.Hardware) -> dict:
    """The machine as the Settings card shows it."""
    a = hw.accelerator
    api = {hardware.CUDA: "CUDA", hardware.METAL: "Metal", hardware.VULKAN: "Vulkan"}.get(a.kind)
    return {
        "accelerator": f"{a.name} ({api})" if api else "No usable GPU — CPU only",
        "memory_mb": hardware.accelerator_memory_mb(a),
        "system_ram_mb": hw.system_ram_mb,
        "tier": hw.tier,
        "tier_detail": hardware.describe_tier(hw.tier),
    }


def plan_item(id: str, title: str, size_bytes: int) -> dict:
    return {"id": id, "title": title, "size_bytes": size_bytes}


def bundle_items() -> list[dict]:
    """Every bundle with the total size of its archives, smallest first."""
    items = [{**plan_item(b.id, b.title, sum(s.size_bytes for s in catalog.bundle(b.id)[1])),
              "description": b.description} for b in catalog.bundles()]
    return sorted(items, key=lambda b: b["size_bytes"])


def setup_plan(state: AppState) -> dict:
    """What a first run on this machine would fetch, and how big it is, so
    the person can decide before anything downloads. The chat model is the
    recommended one; the wizard's model step offers the others
    (model_choice.py)."""
    hw = state.hardware()
    chat = catalog.chat_model_for(hw.tier, hw.system_ram_mb)
    reranker = catalog.reranker_model()
    return {
        "hardware": describe_hardware(hw),
        "chat_model": plan_item(chat.id, chat.title, chat.size_bytes) if chat else None,
        "reranker": plan_item(reranker.id, reranker.title, reranker.size_bytes) if reranker else None,
        "tools": [plan_item(key, name, size) for name, key, size in tools.plan(hw.accelerator)],
        "bundles": bundle_items(),
        # One folder to offer someone who has not chosen any yet; from a
        # drive or an unzipped release the config already names them.
        # Helper programs the catalogue has no build of for this system, for
        # the wizard to ask where the person's own are.
        "own_programs": own_programs.missing(state),
        "suggested_base": str(Path.home() / "Museion"),
        "portable": bool(state.drive),
        # True when there are no helper programs to fetch, so the wizard's
        # list and total leave them out.
        "tools_here": helpers_on_drive(state),
    }


def helpers_on_drive(state: AppState) -> bool:
    """A prepared drive, and an unzipped release, already carry the helper
    programs -- for every kind of graphics card, chosen at launch -- so
    there is nothing to fetch, and set-up must not list them as downloads."""
    cfg = state.config()
    return bool(state.drive and cfg.kiwix_exe and cfg.kiwix_exe.exists()
                and cfg.llama_exe and cfg.llama_exe.exists())


def setup_room(state: AppState, folders: list[str]) -> list[dict]:
    """Free space for each folder the set-up screen is about to fill, and
    which drive it is on, so the wizard can refuse before the first byte
    rather than fail at 98%. A folder that does not exist yet is judged by
    the nearest one above it that does."""
    out = []
    for text in folders:
        entry = {"path": text, "free_bytes": None, "volume": ""}
        if text.strip():
            try:
                entry["free_bytes"] = disk.free_space(Path(text))
                entry["volume"] = disk.volume_name(Path(text))
            except OSError:
                pass
        out.append(entry)
    return out


def progress(state: AppState, item: str, received: int, total: int, note: str = "") -> None:
    state.emit("fetch-progress", {"item": item, "received": received, "total": total, "note": note})


def fetch_tools(state: AppState, dest: str) -> str:
    """Download the helper programs into `dest` and point the config at them."""
    cfg = state.config()
    if helpers_on_drive(state):
        return f"Already on the drive: {cfg.kiwix_exe} and {cfg.llama_exe}"

    def report(step: tools.Step) -> None:
        if step.kind == tools.DOWNLOADING:
            progress(state, step.tool, step.received, step.total, step.file)
        elif step.kind == tools.UNPACKING:
            progress(state, step.tool, 0, 0, "unpacking")
        elif step.kind == tools.DONE:
            progress(state, step.tool, 1, 1, "done")
        elif step.kind == tools.SKIPPED:
            progress(state, step.tool, 0, 0, step.reason)

    installed = tools.install(state.hardware().accelerator, Path(dest), report)
    cfg = state.config()
    # A program the catalogue has no build of for this system is left as it
    # was, rather than forgetting a copy the person pointed at by hand.
    cfg.kiwix_exe = installed.kiwix_serve or cfg.kiwix_exe
    cfg.llama_exe = installed.llama_server or cfg.llama_exe
    if installed.kiwix_serve or installed.llama_server:
        cfg.pdfium_dir = installed.pdfium_dir
    state.save_config(cfg)
    return f"kiwix-serve: {installed.kiwix_serve or 'missing'}  llama-server: {installed.llama_server or 'missing'}"


def fetch_model(state: AppState, id: str, dest: str) -> str:
    """Download a model by catalogue id into the models folder and point the
    config at it, as chat model or reranker according to its role."""
    model = next((m for m in catalog.models() if m.id == id), None)
    if model is None:
        raise UiError(f"no model {id!r} in the catalogue")
    folder = Path(dest)
    target = folder / model.filename
    if not download.is_verified(target, model.size_bytes, model.sha256):
        download.download_verified(
            download.Spec(model.urls, target, model.size_bytes, model.sha256),
            lambda p: progress(state, model.id, p.received, p.total, "resuming" if p.resuming else ""),
        )
    cfg = state.config()
    cfg.models_dir = folder
    if model.role == "reranker":
        cfg.reranker_model = target
    else:
        cfg.chat_model = target
        cfg.chat_args = list(model.server_args)
    state.save_config(cfg)
    return str(target)


def fetch_archive(state: AppState, source: catalog.Source, library: Path) -> bool:
    """Download one archive into the library, unless it is already there.
    Returns whether it downloaded anything.

    When every web source has failed, the torrent is handed over for the
    person's own client rather than Museion joining a swarm on their behalf."""
    target = library / source.filename
    if download.is_verified(target, source.size_bytes, source.sha256):
        progress(state, source.id, source.size_bytes, source.size_bytes, "already present")
        return False
    try:
        download.download_verified(
            download.Spec(source.all_urls(), target, source.size_bytes, source.sha256),
            lambda p: progress(state, source.id, p.received, p.total, "resuming" if p.resuming else ""),
        )
    except (OSError, RuntimeError) as e:
        if not (source.torrent or source.magnet):
            raise
        lines = [str(e), "", "Every web source failed. This archive is also on BitTorrent:"]
        if source.torrent:
            lines.append(f"  torrent: {source.torrent}")
        if source.magnet:
            lines.append(f"  magnet:  {source.short_magnet()}")
        lines.append(f"Download it with your own client, put {source.filename} in {library}, "
                     "and press Fetch again -- it will be verified and skipped.")
        raise UiError("\n".join(lines)) from e
    return True


def fetch_bundle(state: AppState, id: str) -> str:
    """Download every archive in a bundle into the library folder."""
    library = state.config().library_dir
    if not library:
        raise UiError("choose a library folder first")
    _, sources = catalog.bundle(id)
    fetched = sum(fetch_archive(state, source, library) for source in sources)
    return f"{fetched} archive(s) downloaded into {library}"


def measure_space(cfg: Config) -> dict | None:
    """Room on the library's drive, and what the library is using."""
    lib = cfg.library_dir
    if not lib:
        return None
    try:
        free = disk.free_space(lib)
    except OSError:
        return None
    return {
        # Archives plus the index.
        "library_bytes": disk.dir_size(lib),
        "extra_bytes": sum(disk.dir_size(d) for d in cfg.extra_dirs),
        "free_bytes": free,
        "volume": disk.volume_name(lib),
    }


def library_space(state: AppState) -> dict | None:
    return measure_space(state.config())


def list_content(state: AppState) -> dict:
    """Everything that can be fetched, with what is already here."""
    cfg = state.config()
    bundles = catalog.bundles()
    items = []
    for s in catalog.sources():
        try:
            # Present at the right size. A full hash is left to the fetch;
            # this is a fast answer to "do I have it".
            installed = bool(cfg.library_dir) and (cfg.library_dir / s.filename).stat().st_size == s.size_bytes
        except OSError:
            installed = False
        items.append({
            "id": s.id,
            "title": s.title,
            "description": s.description,
            "category": s.category,
            "size_bytes": s.size_bytes,
            "installed": installed,
            "bundles": [b.title for b in bundles if s.id in b.sources],
            # The kiwix book id, for opening it.
            "filename": s.filename.removesuffix(".zim"),
        })
    # Survival first, then medicine, making, the rest; smallest first within.
    items.sort(key=lambda i: (CATEGORY_ORDER.get(i["category"], 3), i["size_bytes"]))
    return {
        "items": items,
        "library_dir": str(cfg.library_dir) if cfg.library_dir else None,
        "space": measure_space(cfg),
    }


def human(n: int) -> str:
    g = n / 1_073_741_824
    return f"{g:.1f} GB" if g >= 1 else f"{n // 1_048_576} MB"


def fetch_source(state: AppState, id: str) -> str:
    """Fetch one archive, then make it askable: the content server is
    restarted so it serves the new file, and the index is brought up to date."""
    cfg = state.config()
    library = cfg.library_dir
    if not library:
        raise UiError("choose a library folder first")
    source = next((s for s in catalog.sources() if s.id == id), None)
    if source is None:
        raise UiError(f"no archive {id!r} in the catalogue")

    # Refuse up front rather than failing at 98%. The partial file would be
    # resumable, but a drive that is already full has nowhere to resume to.
    target = library / source.filename
    if not download.is_verified(target, source.size_bytes, source.sha256):
        try:
            free = disk.free_space(library)
            have = download.part_path(target).stat().st_size if download.part_path(target).exists() else 0
        except OSError:
            free, have = None, 0
        need = max(source.size_bytes - have, 0)
        if free is not None and need > free:
            raise UiError(f"Not enough space: {source.title} needs {human(need)} more "
                          f"and {disk.volume_name(library)} has {human(free)} free.")

    fetch_archive(state, source, library)

    if not state.supervisor.is_running(KIWIX):
        return f"{source.title} is in your library. Start services to browse and ask it."
    progress(state, source.id, 1, 1, "restarting the content server")
    restart_kiwix(state, cfg)
    progress(state, source.id, 1, 1, "indexing")
    return f"{source.title} is in your library. Indexed: {build_index(state, False)}"


def add_content_dir(state: AppState, path: str) -> str:
    """Add a folder of found content. Its archives are served and its PDFs
    are indexed, right away if the services are up."""
    folder = Path(path)
    if not folder.is_dir():
        raise UiError(f"{path} is not a folder")
    cfg = state.config()
    if folder == cfg.library_dir or folder in cfg.extra_dirs:
        raise UiError("that folder is already part of the library")
    cfg.extra_dirs.append(folder)
    state.save_config(cfg)
    if not state.supervisor.is_running(KIWIX):
        return "Folder added. Start services to serve and index it."
    restart_kiwix(state, cfg)
    return f"Folder added and indexed. {build_index(state, False)}"


def remove_content_dir(state: AppState, path: str) -> str:
    """Remove a folder. Its content leaves the index and the content server."""
    folder = Path(path)
    cfg = state.config()
    cfg.extra_dirs = [d for d in cfg.extra_dirs if d != folder]
    state.save_config(cfg)

    # Forget what came from it: the folder's loose PDFs, and any archives
    # that lived there.
    db = cfg.index_path()
    if db and db.exists():
        with closing(Index(db)) as index:
            index.forget_book(indexer.folder_book_id(folder))
            try:
                names = os.listdir(folder)
            except OSError:
                names = []
            for name in names:
                if name.endswith(".zim"):
                    index.forget_book(name.removesuffix(".zim"))
    state.forget_stats()

    if state.supervisor.is_running(KIWIX) and cfg.kiwix_exe:
        restart_kiwix(state, cfg, wait=False)
    return "Folder removed."


def build_index(state: AppState, force: bool = False) -> str:
    """Build the search index over the library. Needs the content server
    running, which start_services arranges. 155 documents take a few
    minutes, and the page shows which one it is on."""
    cfg = state.config()
    db = cfg.index_path()
    if not cfg.library_dir or not db:
        raise UiError("no library configured")
    # pdfium comes in the helper programs' folder unless placed elsewhere.
    pdfium = cfg.pdfium_dir or (cfg.kiwix_exe.parent if cfg.kiwix_exe else None)
    if not pdfium:
        raise UiError("pdfium directory not configured")
    if not state.supervisor.is_running(KIWIX):
        raise UiError("the content server is not running -- start services first")

    def say(p: indexer.Progress) -> None:
        if isinstance(p, indexer.Archive):
            state.emit("index-progress", f"Indexing {p.book} ({p.documents} documents)…")
        elif isinstance(p, indexer.Native):
            state.emit("index-progress", f"{p.book}: uses its own index")
        elif isinstance(p, indexer.Skipped):
            state.emit("index-progress", f"{p.book}: already indexed")
        elif isinstance(p, indexer.Document) and p.outcome != indexer.DUPLICATE:
            state.emit("index-progress", f"{p.done}/{p.total}  {p.title}")
        elif isinstance(p, indexer.ArchiveDone):
            state.emit("index-progress", f"{p.book}: {p.indexed} documents indexed")

    stats = indexer.index_library(cfg.library_dir, cfg.extra_dirs, db, local(cfg.kiwix_port), pdfium, force, say)
    state.forget_stats()
    return f"{stats.documents} documents, {stats.pages} pages, {stats.words} words indexed."
