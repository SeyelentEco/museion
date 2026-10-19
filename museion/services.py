"""Starting, stopping and fitting the helpers from the window: the content
server, the reranker and the chat model.

Progress goes out as "service-progress" events so the page can say what it
is waiting for -- a large model can take minutes, and silence during that
looks like a hang.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from museion import fit
from museion.appstate import AppState, UiError, file_name, local
from museion.config import Config
from museion.sidecar import CHAT, KIWIX, RERANKER

KIWIX_HEALTH = "/catalog/v2/entries"
LLAMA_HEALTH = "/v1/models"

# The reranker's settings. A cross-encoder reads the question and a passage
# together, so the batch must hold both whole.
RERANKER_ARGS = ["--reranking", "--pooling", "rank", "--batch-size", "2048",
                 "--ubatch-size", "2048", "--ctx-size", "8192"]

# Flags the chat model always runs with, on top of its own: tool calling
# needs the model's own chat template (--jinja), and the KV cache is q8_0
# because harsher quantisation degrades tool calling.
CHAT_FLAGS = ["--flash-attn", "on", "--jinja", "--parallel", "1",
              "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]


def service_state(state: AppState) -> dict:
    """Just the service flags, for the header dots. Cheap enough to poll: it
    touches the supervisor's process list and nothing else."""
    s = state.supervisor
    return {
        "kiwix_running": s.is_running(KIWIX),
        "chat_running": s.is_running(CHAT),
        "reranker_running": s.is_running(RERANKER),
    }


def start_services(state: AppState) -> None:
    """Bring up the helpers, reporting each step.

    A server already answering on the port is used, not replaced. That is
    what makes a restart of the app cost seconds when the servers were left
    up (MUSEION_KEEP_SERVICES=1 while developing), and it also means two
    copies of Museion do not fight over one GPU."""
    cfg = state.config()
    if not cfg.kiwix_exe:
        raise UiError("kiwix-serve path not configured")
    if not cfg.llama_exe:
        raise UiError("llama-server path not configured")
    if not cfg.library_dir:
        raise UiError("no library configured")

    def say(message: str) -> None:
        state.emit("service-progress", message)

    sup = state.supervisor
    kiwix_health = local(cfg.kiwix_port) + KIWIX_HEALTH
    if sup.adopt(KIWIX, cfg.kiwix_port, kiwix_health):
        say("Content server already running; using it.")
    else:
        say("Starting the content server…")
        sup.start_kiwix(cfg.kiwix_exe, cfg.content_dirs(), cfg.kiwix_port)
        sup.wait_ready(KIWIX, kiwix_health, 60)

    reranker_health = local(cfg.reranker_port) + LLAMA_HEALTH
    if cfg.reranker_model:
        if sup.adopt(RERANKER, cfg.reranker_port, reranker_health, cfg.reranker_model):
            say("Reranker already running; using it.")
        else:
            say("Starting the reranker…")
            # GPU layers deliberately 0: on a 12 GB card the chat model wants
            # all of it, and this costs only seconds on CPU.
            sup.start_llama(RERANKER, cfg.llama_exe, cfg.reranker_model, cfg.reranker_port, 0, RERANKER_ARGS)
            sup.wait_ready(RERANKER, reranker_health, 180)

    if not cfg.chat_model:
        raise UiError("no chat model configured")
    say("Loading the chat model — this can take a few minutes…")
    start_chat(state, cfg, cfg.chat_model, cfg.chat_args, cfg.chat_gpu_layers)
    say("Ready.")


def start_chat(state: AppState, cfg: Config, model: Path, extra: list[str], gpu_layers: int) -> None:
    """Start the chat model with Museion's own flags on top of the given
    ones, and wait for it."""
    if not cfg.llama_exe:
        raise UiError("llama-server path not configured")
    health = local(cfg.chat_port) + LLAMA_HEALTH
    if state.supervisor.adopt(CHAT, cfg.chat_port, health, model):
        return
    state.supervisor.start_llama(CHAT, cfg.llama_exe, model, cfg.chat_port, gpu_layers, [*extra, *CHAT_FLAGS])
    state.supervisor.wait_ready(CHAT, health, 600)


def stop_services(state: AppState) -> None:
    state.supervisor.stop_all()


def restart_kiwix(state: AppState, cfg: Config, wait: bool = True) -> None:
    """kiwix-serve only knows the files it was started with, so new or
    removed content means starting it again."""
    if not cfg.kiwix_exe:
        raise UiError("kiwix-serve path not configured")
    state.supervisor.stop(KIWIX)
    state.supervisor.start_kiwix(cfg.kiwix_exe, cfg.content_dirs(), cfg.kiwix_port)
    if wait:
        state.supervisor.wait_ready(KIWIX, local(cfg.kiwix_port) + KIWIX_HEALTH, 60)


def swap_chat_model(state: AppState, cfg: Config, candidate: Path) -> None:
    """Swap the running chat model for another -- fitted to the GPU first --
    for the duration of a benchmark."""

    def say(message: str) -> None:
        state.emit("service-progress", message)

    exe = fit_exe(cfg)
    say("Stopping the current chat model…")
    state.supervisor.stop(CHAT)
    time.sleep(2)

    # Fit the candidate the way set-up fits the configured model. A dense
    # model that fits entirely gets every layer; a MoE gets its split.
    say(f"Fitting {file_name(candidate)} to this GPU…")
    try:
        result = fit.fit_model(exe, candidate, 32768)
        args, layers = without_layers(result.args), result.gpu_layers if result.gpu_layers is not None else 999
    except (OSError, RuntimeError, ValueError) as e:
        say(f"Fit failed ({e}); trying with everything on the GPU.")
        args, layers = ["--ctx-size", "16384"], 999

    say(f"Loading {file_name(candidate)}…")
    start_chat(state, cfg, candidate, args, layers)


def restore_chat_model(state: AppState, cfg: Config) -> None:
    """Put the configured chat model back after a benchmark, whatever
    happened during it."""
    state.emit("service-progress", "Restoring the configured chat model…")
    state.supervisor.stop(CHAT)
    time.sleep(2)
    if not cfg.chat_model:
        return
    try:
        start_chat(state, cfg, cfg.chat_model, cfg.chat_args, cfg.chat_gpu_layers)
        state.emit("service-progress", "Ready.")
    except (OSError, RuntimeError, UiError) as e:
        state.emit("service-progress", f"Could not restore the chat model: {e}")


def fit_chat_model(state: AppState) -> str:
    """Measure the chat model against this machine and store the offload
    split. Returns a sentence for the person."""
    cfg = state.config()
    args, gpu_layers, message = measure_fit(state, cfg)
    cfg.chat_args = args
    if gpu_layers is not None:
        cfg.chat_gpu_layers = gpu_layers
    state.save_config(cfg)
    return message


def measure_fit(state: AppState, cfg: Config) -> tuple[list[str], int | None, str]:
    """Run llama-fit-params for the configured model and return the chat
    arguments to use, the GPU layer count, and a sentence for the person."""
    if not cfg.chat_model:
        raise UiError("no chat model configured")
    exe = fit_exe(cfg)

    # Context comes from whatever --ctx-size is already configured, so the
    # fit is measured for the configuration that will actually run.
    ctx = 32768
    for flag, value in zip(cfg.chat_args, cfg.chat_args[1:]):
        if flag in ("--ctx-size", "-c") and value.isdigit():
            ctx = int(value)
            break

    # The fit measures free GPU memory. With the chat model already resident
    # the card reads as nearly full and the split comes out far too cautious:
    # measured on the development machine, blocks 10-40 offloaded instead of
    # 15-40. So the model is stopped first, and the person is told to start
    # services again.
    if state.supervisor.is_running(CHAT):
        state.supervisor.stop(CHAT)
        time.sleep(2)
    result = fit.fit_model(exe, cfg.chat_model, ctx)

    # Keep any arguments the person added, replace the fitted ones. The fit
    # already includes -c, so ours is dropped to avoid a repeat.
    args = []
    skip = False
    for a in cfg.chat_args:
        if skip:
            skip = False
            continue
        if a in ("--ctx-size", "-c", "-ngl", "--n-gpu-layers", "-ot"):
            skip = True
            continue
        args.append(a)
    args += without_layers(result.args)

    layers = "all" if result.gpu_layers is None else str(result.gpu_layers)
    where = ("expert tensors split between GPU and CPU." if "-ot" in result.args
             else "everything resident on the GPU.")
    return args, result.gpu_layers, f"{layers} layers on the GPU; {where}"


def without_layers(args: list[str]) -> list[str]:
    """The arguments without -ngl and its value: the supervisor passes the
    layer count itself, from chat_gpu_layers."""
    out = []
    i = 0
    while i < len(args):
        if args[i] in ("-ngl", "--n-gpu-layers"):
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out


def fit_exe(cfg: Config) -> Path:
    """llama-fit-params, which comes in the same llama.cpp build as
    llama-server and sits beside it."""
    if not cfg.llama_exe:
        raise UiError("llama-server path not configured")
    return cfg.llama_exe.parent / ("llama-fit-params.exe" if sys.platform == "win32" else "llama-fit-params")

