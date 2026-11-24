"""What the Museion page asks of this program, apart from the helpers
(services.py) and the library (library.py): status, settings, asking,
the benchmark, and finding files on this computer.

Every command takes the AppState first and the page's arguments by name, and
returns something JSON can carry. app.py lists them all in COMMANDS; a new
command is a function here and a line there.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import string
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from museion import ask as asking
from museion import calc, evals, tuning
from museion.appstate import AppState, UiError, file_name, local
from museion.config import Config
from museion.index import Index
from museion.kiwix import Kiwix, book_id_from_filename
from museion.library import describe_hardware
from museion.llama import Llama
from museion.services import new_chat_model, restore_chat_model, service_state, swap_chat_model
from museion.sidecar import CHAT, KIWIX


def get_hardware(state: AppState) -> dict:
    return describe_hardware(state.hardware())


def get_status(state: AppState) -> dict:
    cfg = state.config()
    documents, pages, words = state.index_stats(cfg)
    return {
        "configured": cfg.is_ready(),
        # The drive's root when running from one; None when installed.
        "portable_root": str(state.drive.root) if state.drive else None,
        "library_dir": str(cfg.library_dir) if cfg.library_dir else None,
        "documents": documents,
        "pages": pages,
        "words": words,
        **service_state(state),
        "chat_model": file_name(cfg.chat_model) if cfg.chat_model else None,
        "reranker_model": file_name(cfg.reranker_model) if cfg.reranker_model else None,
    }


def get_config(state: AppState) -> dict:
    return state.config().to_json()


def set_config(state: AppState, config: dict) -> str | None:
    """Save the page's settings. Returns what the person should do next when
    the chat model changed: it is not loaded until services start again, and
    on a GPU it wants measuring first (a drive does that by itself)."""
    cfg = Config.from_json(config)
    changed = cfg.chat_model is not None and cfg.chat_model != state.config().chat_model
    if changed:
        new_chat_model(cfg, state.hardware())
    state.save_config(cfg)
    if not changed:
        return None
    if cfg.chat_gpu_layers == 0 or state.drive:
        return "Press Start services to load the new model."
    return "Press Fit model to measure the new model against this computer's graphics card, then Start services."


def kiwix_url(state: AppState) -> str:
    return local(state.config().kiwix_port)


# ── asking ───────────────────────────────────────────────────────────────────

def make_asker(state: AppState, cfg: Config, index: Index, chat: Llama) -> asking.Asker:
    """An asker over the index and the running helpers. The reranker is used
    if it answers. Archives the local index does not hold are searched
    through kiwix-serve's own index -- that is how Wikipedia becomes askable."""
    reranker = Llama(local(cfg.reranker_port))
    kiwix = Kiwix(local(cfg.kiwix_port))
    kiwix_up = state.supervisor.is_running(KIWIX) and kiwix.is_up()
    native = []
    if kiwix_up:
        processed = set(index.processed_books())
        for folder in cfg.content_dirs():
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            native += [book_id_from_filename(n) for n in names if n.endswith(".zim")]
        native = [b for b in native if b not in processed]
    return asking.Asker(index, chat, reranker if reranker.is_up() else None,
                        kiwix if kiwix_up else None, native, tuning.load(state.tuning_path))


def ask(state: AppState, question: str, history: list[dict] | None = None) -> dict:
    """Ask a question. The answer streams to the page as "answer-token"
    events, and "answer-reset" takes back what streamed so far when the model
    turns to a tool instead. The result carries the final text -- with its
    citations numbered to match the sources -- and the sources themselves."""
    cfg = state.config()
    db = cfg.index_path()
    if not db:
        raise UiError("no library configured")
    chat = Llama(local(cfg.chat_port))
    # Say so at once rather than letting the question wait on a server that
    # is absent or still loading. The distinction matters to the person: one
    # means press Start, the other means wait. Arithmetic is answered without
    # a model; only insist on one otherwise.
    if not calc.looks_like_arithmetic(question) and not chat.is_up():
        if state.supervisor.is_running(CHAT):
            raise UiError("The chat model is still loading. Give it a minute and try again.")
        raise UiError("The chat model isn't running. Open Settings and press Start services.")

    streamed = False

    def on_token(text: str | None) -> None:
        nonlocal streamed
        if text is asking.DISCARD:
            streamed = False
            state.emit("answer-reset", None)
        else:
            streamed = True
            state.emit("answer-token", text)

    earlier = [asking.Exchange(h.get("question", ""), h.get("answer", "")) for h in history or []]
    with closing(Index(db)) as index:
        from museion.harness import Setting
        setting = Setting(chat=chat, notes_dir=state.notes_dir(cfg))
        answer = make_asker(state, cfg, index, chat).ask(question, earlier, on_token, setting)
    # Nothing streams when retrieval found nothing, and that answer -- the
    # library saying it does not cover this -- must still be shown.
    if not streamed and answer.text:
        state.emit("answer-token", answer.text)
    return {
        "text": answer.text,
        "sources": [
            {"n": n, "title": h.doc_title, "author": h.author, "page": h.page, "book": h.book,
             # PDF filename for a page source; article path for a native
             # archive; the full path for a PDF found in a folder.
             "file": h.file, "snippet": h.snippet}
            for n, h in enumerate(answer.sources, start=1)
        ],
        "basis": answer.basis,
        "calculations": [dataclasses.asdict(c) for c in answer.calculations],
    }


# ── the benchmark ────────────────────────────────────────────────────────────

def list_models(state: AppState) -> list[dict]:
    """GGUF files a benchmark could be run against: the models folder and
    wherever the configured chat model lives. Rerankers are left out."""
    cfg = state.config()
    folders = [d for d in (cfg.models_dir, cfg.chat_model.parent if cfg.chat_model else None) if d]
    out, seen = [], set()
    for folder in folders:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            path = Path(e.path)
            low = e.name.lower()
            if not low.endswith(".gguf") or "rerank" in low or path == cfg.reranker_model or path in seen:
                continue
            seen.add(path)
            out.append({"path": str(path), "name": e.name, "size_bytes": e.stat().st_size,
                        "current": path == cfg.chat_model})
    out.sort(key=lambda m: (not m["current"], m["name"]))
    return out


def run_evals(state: AppState, label: str, only: str | None = None, model: str | None = None) -> dict:
    """Run the benchmark; save and return the run.

    With `model` set to a GGUF other than the configured chat model, that
    model is swapped in for the run -- fitted to the GPU first -- and the
    configured one put back afterwards, whatever happened, so the same set
    can be put to a second model and the two runs compared."""
    cfg = state.config()
    candidate = Path(model) if model and model.strip() and Path(model) != cfg.chat_model else None
    if candidate and not candidate.exists():
        raise UiError(f"{candidate} does not exist")
    cases = evals.builtin_cases()
    if only:
        cases = [c for c in cases if only in (c.capability, c.horizon, c.id)]
    if not cases:
        raise UiError("no cases match")
    db = cfg.index_path()
    if not cfg.library_dir or not db:
        raise UiError("no library configured")
    try:
        if candidate:
            swap_chat_model(state, cfg, candidate)
        chat = Llama(local(cfg.chat_port))
        if not chat.is_up():
            raise UiError("The chat model isn't running. Start services first.")

        def report(index: int, total: int, case_id: str, passed: bool | None) -> None:
            state.emit("eval-progress", {"index": index, "total": total, "id": case_id, "passed": passed})

        with closing(Index(db)) as index:
            run = evals.run(make_asker(state, cfg, index, chat), cases, label, report)
    finally:
        if candidate:
            restore_chat_model(state, cfg)
    evals.save_run(state.evals_dir(cfg), run)
    return dataclasses.asdict(run)


def list_eval_runs(state: AppState) -> list[dict]:
    folder = state.evals_dir(state.config())
    return [dataclasses.asdict(r) for r in evals.list_runs(folder)] if folder else []


def eval_cases(state: AppState) -> list[dict]:
    return [dataclasses.asdict(c) for c in evals.builtin_cases()]


# ── prompts and limits ───────────────────────────────────────────────────────

def tuning_info(state: AppState) -> dict:
    """Where the prompts-and-limits override lives, and whether it exists."""
    return {"path": str(state.tuning_path), "exists": state.tuning_path.exists()}


def write_default_tuning(state: AppState) -> str:
    """Write the built-in defaults out as the override file, if there is
    none yet, so editing starts from the real text with its comments.
    Returns the path either way."""
    if not state.tuning_path.exists():
        state.tuning_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tuning.DEFAULTS_PATH, state.tuning_path)
    return str(state.tuning_path)


# ── files on this computer ───────────────────────────────────────────────────

def open_path(state: AppState, path: str) -> None:
    """Open a file with whatever this computer uses for it: the tuning file
    in a text editor, say."""
    if not Path(path).exists():
        raise UiError(f"{path} does not exist")
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 -- opening the person's own file is the point
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise UiError(f"Nothing on this computer opened it ({opener}: {e.strerror}). "
                          f"Open it yourself in a text editor: {path}") from e


def browse(state: AppState, path: str = "", want: str = "dir") -> dict:
    """One folder's contents, for the page's Choose… window: a browser page
    cannot see this computer's files, so this program lists them.

    `want` is "dir" for a folder, "gguf" for a model, "program" for a helper
    program. A path that is a file, or no longer exists, opens the nearest
    folder that does; an empty one opens the home folder."""
    folder = Path(path.strip()).expanduser() if path.strip() else Path.home()
    while not folder.is_dir() and folder != folder.parent:
        folder = folder.parent
    if not folder.is_dir():
        folder = Path.home()
    entries, error = [], None
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        entries.append({"name": e.name, "dir": True})
                    elif want != "dir" and wanted(e, want):
                        entries.append({"name": e.name, "dir": False, "size_bytes": e.stat().st_size})
                except OSError:
                    continue
    except OSError as e:
        error = str(e)
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {
        "path": str(folder),
        "parent": str(folder.parent) if folder.parent != folder else None,
        "entries": entries[:2000],
        "roots": roots(),
        "error": error,
    }


def wanted(entry: os.DirEntry, want: str) -> bool:
    name = entry.name.lower()
    if want == "gguf":
        return name.endswith(".gguf")
    if want == "zip":
        return name.endswith(".zip")
    if want == "package":  # a Museion download
        return name.endswith((".zip", ".tar.gz", ".tgz"))
    if want == "program":
        return name.endswith(".exe") if sys.platform == "win32" else os.access(entry.path, os.X_OK)
    return True


def roots() -> list[dict]:
    """Where the Choose… window can start: home, and every drive on Windows
    or the filesystem root elsewhere."""
    out = [{"name": "Home", "path": str(Path.home())}]
    if sys.platform == "win32":
        drives = os.listdrives() if hasattr(os, "listdrives") else [
            f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
        out += [{"name": d.rstrip("\\"), "path": d} for d in drives]
    else:
        out.append({"name": "/", "path": "/"})
        if sys.platform == "darwin" and os.path.isdir("/Volumes"):
            out.append({"name": "Drives", "path": "/Volumes"})
        elif os.path.isdir("/media"):
            out.append({"name": "Drives", "path": "/media"})
    return out
