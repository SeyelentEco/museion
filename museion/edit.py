"""Changing Museion with the model it runs on.

`python -m museion edit` (the "Edit Museion" launcher on a drive) starts the
chat model if it is not already running, and opens opencode, a coding agent,
on Museion's own code, talking to that model. Whoever has the drive can then
ask for a change in plain words, years after the last download.

opencode is told to stay off the network: no update checks, no model list
from the web, no plugins (each of which it would otherwise try to install
from npm at start, and wait for). Its own state -- conversations, logs -- is
kept with Museion's per-computer state, not on the drive.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from museion import services
from museion.appstate import AppState, UiError, local
from museion.llama import Llama
from museion.portable import APP_DIR

# opencode's instructions and tool descriptions come to some 12,000 tokens
# before the first word of a request, so the model needs room beyond the
# 16,384 the small chat model answers questions with.
CONTEXT = 32768


def find_opencode(state: AppState) -> Path | None:
    """opencode from the drive's bin/, else from the configured helper
    programs' folder, else wherever this computer has it."""
    name = "opencode.exe" if os.name == "nt" else "opencode"
    cfg = state.config()
    for folder in (state.drive.root / "bin" if state.drive else None,
                   cfg.kiwix_exe.parent if cfg.kiwix_exe else None):
        if folder and (folder / name).exists():
            return folder / name
    found = shutil.which("opencode")
    return Path(found) if found else None


def with_context(args: list[str], context: int) -> list[str]:
    """The chat arguments with a context of at least `context` tokens."""
    out, i, current = [], 0, 0
    while i < len(args):
        if args[i] in ("--ctx-size", "-c") and i + 1 < len(args):
            current = int(args[i + 1]) if args[i + 1].isdigit() else 0
            i += 2
            continue
        out.append(args[i])
        i += 1
    return [*out, "--ctx-size", str(max(current, context))]


def opencode_config(chat_url: str, model_name: str, context: int) -> dict:
    """opencode's settings: one provider, the chat model on this computer."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "provider": {
            "museion": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Museion (this computer)",
                "options": {"baseURL": f"{chat_url}/v1"},
                "models": {"local": {"name": model_name, "tool_call": True,
                                     "limit": {"context": context, "output": 4096}}},
            },
        },
        "model": "museion/local",
        "small_model": "museion/local",
    }


def opencode_env(state: AppState, chat_url: str, model_name: str, context: int) -> dict[str, str]:
    home = (state.drive.cache_dir if state.drive else state.config_path.parent) / "opencode"
    env = dict(os.environ)
    env.update({
        "OPENCODE_CONFIG_CONTENT": json.dumps(opencode_config(chat_url, model_name, context)),
        # Nothing from the network: see the module's docstring.
        "OPENCODE_PURE": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        # Its conversations and logs, kept off the drive.
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        # The Python running Museion first, so `python` in opencode's shell
        # is the drive's own and the tests it runs use what Museion uses.
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")]),
    })
    return env


def chat_context(url: str) -> int:
    """The context the running chat model was started with, or 0 when it
    does not say."""
    try:
        with urllib.request.urlopen(f"{url}/props", timeout=4) as res:
            props = json.load(res)
        return int(props.get("default_generation_settings", {}).get("n_ctx") or props.get("n_ctx") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def run(state: AppState, args: list[str]) -> int:
    """Start the chat model if needed, run opencode until it exits, then
    stop the chat model if this started it. Returns opencode's exit code."""
    exe = find_opencode(state)
    if exe is None:
        raise UiError("opencode is not on this drive or this computer. A drive prepared by "
                      "Museion carries it in bin/; otherwise install it and run this again.")
    cfg = state.config()
    if not cfg.chat_model or not cfg.chat_model.exists():
        raise UiError("No chat model is set up. Open Museion and finish its set-up first.")
    url = local(cfg.chat_port)
    try:
        if Llama(url).is_up():
            have = chat_context(url)
            if have and have < CONTEXT:
                raise UiError(f"The chat model is already running with {have} tokens of context, "
                              f"too few for opencode ({CONTEXT}). Stop services in Museion (or close it), "
                              "then run this again.")
            print("Using the chat model that is already running.")
        else:
            services.fit_if_due(state, cfg, print)
            print(f"Loading the chat model ({cfg.chat_model.name}); this can take a few minutes…")
        services.start_chat(state, cfg, cfg.chat_model, with_context(cfg.chat_args, CONTEXT), cfg.chat_gpu_layers)
        print(f"Opening opencode on {APP_DIR}\n")
        return subprocess.call([str(exe), *args], cwd=APP_DIR,
                               env=opencode_env(state, url, cfg.chat_model.name, CONTEXT))
    finally:
        state.supervisor.release_all()
