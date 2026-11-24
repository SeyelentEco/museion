"""Helping a person choose the model that writes the answers.

Set-up used to pick the model for the detected tier and show one line about
it. Someone setting up a computer to rely on for years deserves to know why:
what the computer has, which models fit it, how big each is, and what each
can and cannot do -- the 4B cannot run most of the agents or change
Museion's own code, which is worth knowing before the internet goes away.

The words about each model live in its file under models/ (summary,
good_for, limits, speed), so they can be corrected offline like the rest.
What is decided here is only what can be worked out on this computer: the
fit, the room on disk, and which agents a model can run.
"""

from __future__ import annotations

from pathlib import Path

from museion import agents, catalog, disk, hardware, library, services
from museion.appstate import AppState, UiError
from museion.sidecar import CHAT

# What each tier asks of a computer, as the chooser says it.
NEEDS = {
    hardware.MODEST: "any computer, with or without a graphics card",
    hardware.CAPABLE: "a graphics card with 8 GB of its own memory and 24 GB of system memory, "
                      "or a Mac with 32 GB",
    hardware.GENEROUS: "a graphics card with 16 GB or more and 16 GB of system memory, or a Mac with 48 GB",
    hardware.AMPLE: "a graphics card with 32 GB or more, or a Mac with 64 GB",
}


def best_tier(model: catalog.Model) -> str:
    """The largest tier a model is made for (Modest if it names none)."""
    known = [t for t in model.tiers if t in hardware.TIERS]
    return max(known, key=hardware.TIERS.index) if known else hardware.MODEST


def least_tier(model: catalog.Model) -> str:
    """The smallest tier a model is made for: what a computer needs to run it."""
    known = [t for t in model.tiers if t in hardware.TIERS]
    return min(known, key=hardware.TIERS.index) if known else hardware.MODEST


def agent_tier(machine_tier: str, chat_model: Path | None) -> str:
    """The tier agents are resolved for: the computer's, unless the chat
    model chosen is a smaller one. The 4B on a 12 GB card is still the 4B;
    offering it the stores keeper, which it cannot run, only fails later and
    less clearly. A model that is not in the catalogue is taken at the
    computer's word."""
    if chat_model is None:
        return machine_tier
    model = next((m for m in catalog.models() if m.role == "chat" and m.filename == chat_model.name), None)
    if model is None:
        return machine_tier
    cap = best_tier(model)
    return cap if hardware.TIERS.index(cap) < hardware.TIERS.index(machine_tier) else machine_tier


def describe_machine(hw: hardware.Hardware) -> list[str]:
    """The computer in plain words, one line each."""
    a = hw.accelerator
    ram = f"{round(hw.system_ram_mb / 1024)} GB of system memory"
    if a.kind == hardware.METAL:
        return [f"{a.name}, {round(a.memory_mb / 1024)} GB of memory shared by the processor and graphics"]
    if a.kind == hardware.CPU:
        return ["No graphics card Museion can use, so models run on the processor", ram]
    return [f"Graphics card: {a.name}, {round(a.memory_mb / 1024)} GB of its own memory", ram]


def fit(model: catalog.Model, hw: hardware.Hardware) -> str | None:
    """Why this computer is too small for the model, or None if it fits."""
    need = model.requires.system_ram_mb if model.requires else 0
    if need > hw.system_ram_mb:
        # Requirements are written in round thousands of MB (24000 is "24 GB").
        return (f"Needs {round(need / 1000)} GB of system memory; "
                f"this computer has {round(hw.system_ram_mb / 1024)} GB.")
    want = least_tier(model)
    if hardware.TIERS.index(hw.tier) < hardware.TIERS.index(want):
        return f"Made for {NEEDS[want]}. On this computer it would be very slow, or not load at all."
    return None


def helpers(tier: str, overrides: Path | None) -> tuple[list[str], list[str]]:
    """(names of the agents that run at this tier, names of those that do not)."""
    runs, not_runs = [], []
    try:
        specs = agents.load_all(overrides, tier)
    except (OSError, ValueError):
        return runs, not_runs
    for spec in specs:
        (not_runs if spec.problems or agents.tier_problem(spec, tier) else runs).append(spec.name)
    return runs, not_runs


def on_disk(folder: Path | None, model: catalog.Model) -> bool:
    """Is the model's file in the folder at its full size? The size alone:
    hashing 22 GB to draw a list would take a minute. Fetching checks the
    hash."""
    if folder is None:
        return False
    try:
        return (folder / model.filename).stat().st_size == model.size_bytes
    except OSError:
        return False


def models_folder(state: AppState, models_dir: str | None) -> Path | None:
    cfg = state.config()
    if models_dir:
        return Path(models_dir)
    return cfg.models_dir or (cfg.chat_model.parent if cfg.chat_model else None)


def model_choices(state: AppState, models_dir: str | None = None) -> dict:
    """Everything the chooser shows: this computer, the room in the models
    folder, and each chat model with whether it fits and what it can do.
    `models_dir` is the folder typed on the set-up screen, which may not be
    saved yet."""
    hw = state.hardware()
    cfg = state.config()
    folder = models_folder(state, models_dir)
    free = None
    if folder is not None:
        try:
            free = disk.free_space(folder)
        except OSError:
            free = None
    recommended = catalog.chat_model_for(hw.tier, hw.system_ram_mb)
    overrides = agents.override_dir(state.tuning_path)
    chats = [m for m in catalog.models() if m.role == "chat"]
    out = []
    for m in sorted(chats, key=lambda m: (hardware.TIERS.index(least_tier(m)), not m.preferred, m.size_bytes)):
        too_small = fit(m, hw)
        present = on_disk(folder, m)
        runs, not_runs = helpers(agent_tier(hw.tier, Path(m.filename)), overrides)
        out.append({
            "id": m.id, "title": m.title, "size_bytes": m.size_bytes,
            "summary": m.summary, "good_for": m.good_for, "limits": m.limits, "speed": m.speed,
            "needs": NEEDS[least_tier(m)],
            "recommended": recommended is not None and m.id == recommended.id,
            "fits": too_small is None, "why_not": too_small,
            "on_disk": present,
            "no_room": not present and free is not None and m.size_bytes > free,
            "current": cfg.chat_model is not None and cfg.chat_model.name == m.filename,
            "helpers_run": runs, "helpers_not": not_runs,
        })
    reranker = catalog.reranker_model()
    smallest = min(chats, key=lambda m: m.size_bytes) if chats else None
    return {
        "machine": describe_machine(hw),
        "tier": hw.tier,
        "models_dir": str(folder) if folder else None,
        "free_bytes": free,
        "models": out,
        "recommended": recommended.id if recommended else None,
        "reranker_on_disk": reranker is not None and on_disk(folder, reranker),
        # The small model as a spare: if the graphics card dies, another
        # computer's processor can still run it. Offered beside anything
        # bigger, since someone cut off for years cannot fetch it later.
        "spare": smallest.id if smallest else None,
    }


def use_model(state: AppState, id: str) -> str:
    """Make a catalogue model the chat model: fetch it if it is not in the
    models folder, measure it against the graphics card, and load it if the
    chat model was running. Returns a sentence for the person."""
    model = next((m for m in catalog.models() if m.id == id and m.role == "chat"), None)
    if model is None:
        raise UiError(f"no chat model {id!r} in the catalogue")
    folder = models_folder(state, None)
    if folder is None:
        raise UiError("Choose a folder for models first (Settings › Folders, or the set-up screen).")
    if on_disk(folder, model):
        cfg = state.config()
        cfg.models_dir = folder
        cfg.chat_model = folder / model.filename
        state.save_config(cfg)
    else:
        library.fetch_model(state, model.id, str(folder))
    cfg = state.config()
    hw = state.hardware()
    services.new_chat_model(cfg, hw)
    state.save_config(cfg)

    said = f"{model.title} is now the chat model."
    # Stopped before measuring: the fit reads free graphics memory, and with
    # the old model still loaded it sees a full card and picks a split far
    # more cautious than the computer deserves.
    was_running = state.supervisor.is_running(CHAT)
    if was_running:
        state.emit("service-progress", "Stopping the old chat model…")
        state.supervisor.stop(CHAT)
    if hw.accelerator.kind != hardware.CPU and least_tier(model) != hardware.MODEST:
        state.emit("service-progress", f"Measuring {model.title} against the graphics card…")
        try:
            said += f" Fitted: {services.fit_chat_model(state)}"
        except (OSError, RuntimeError, ValueError, UiError) as e:
            said += f" Could not measure it against the graphics card ({e}); press Fit model to try again."
    if was_running:
        state.emit("service-progress", f"Loading {model.title} — this can take a few minutes…")
        cfg = state.config()
        services.start_chat(state, cfg, cfg.chat_model, cfg.chat_args, cfg.chat_gpu_layers)
        state.emit("service-progress", "Ready.")
        return said + " It is loaded and answering."
    return said + " Press Start services to load it."
