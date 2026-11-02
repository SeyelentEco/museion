"""What the page's Agents tab asks: the agents and whether they can run here,
one agent's settings to edit, saving the person's copy, making a new one,
its runs and notes, saving a run as a test case, and running its tests.

Edits always go to the person's copy in the config folder
(agents.override_dir); the shipped files are never changed from the page.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import time
import tomllib
import zipfile
from contextlib import closing
from pathlib import Path

from museion import agents, evals, guards, tuning
from museion.agent_run import load_cases, prepared, save_case, tests_dir
from museion.agent_run import test as run_tests
from museion.agent_tools import all_tools
from museion.appstate import AppState, UiError, local
from museion.catalog_add import toml_lines
from museion.chat_commands import GPU, environment
from museion.index import Index
from museion.llama import Llama
from museion.runs import list_logs, read_log

# The settings the page's form edits, in the order it shows them.
EDITABLE = ["name", "goal", "min_tier", "tools", "guards", "max_rounds", "answer_tokens",
            "keep_rounds", "compact", "memory", "confirm", "enabled", "examples"]


def last_test(env, spec) -> dict | None:
    runs = evals.list_runs(tests_dir(env, spec)) if tests_dir(env, spec).is_dir() else []
    mine = [r for r in runs if not env.model_id or r.model == env.model_id] or runs
    if not mine:
        return None
    r = mine[0]
    return {"passed": r.passed, "total": r.total, "model": r.model, "id": r.id,
            "this_model": bool(env.model_id) and r.model == env.model_id}


def list_agents(state: AppState) -> dict:
    env = environment(state)
    out = []
    for spec in env.all():
        out.append({
            "id": spec.id, "name": spec.name, "goal": spec.goal, "min_tier": spec.min_tier,
            "tools": spec.tools, "memory": spec.memory, "enabled": spec.enabled,
            "examples": spec.examples, "problems": spec.problems,
            "cannot_run": spec.problems[0] if spec.problems else agents.tier_problem(spec, env.tier),
            "shipped": spec.folder is not None, "yours": spec.override is not None,
            "last_test": last_test(env, spec),
        })
    return {"agents": out, "tier": env.tier, "model": env.model_id}


def get_agent(state: AppState, id: str) -> dict:
    env = environment(state)
    spec = agent_or_error(env, id)
    shipped_prompt = (spec.folder / "prompt.md").read_text(encoding="utf-8") if spec.folder and (spec.folder / "prompt.md").is_file() else ""
    mine_prompt = (spec.override / "prompt.md").read_text(encoding="utf-8") if spec.override and (spec.override / "prompt.md").is_file() else ""
    notes = env.workspace(spec)
    return {
        "id": spec.id, "settings": agents.as_settings(spec), "problems": spec.problems,
        "prompt": mine_prompt or shipped_prompt, "prompt_from": spec.prompt_from,
        "resolved_prompt": prepared(spec, tuning.load(state.tuning_path)).prompt,
        "shipped": spec.folder is not None, "yours": spec.override is not None,
        "folder": str(spec.folder or ""), "your_folder": str(spec.override or agents.override_dir(state.tuning_path) / id),
        "tier": env.tier, "model": env.model_id,
        "tools": [{"name": t.name, "risk": t.risk, "description": t.description} for t in all_tools().values()],
        "guards": list(guards.GUARDS),
        "tiers": list(agents.hardware.TIERS),
        "notes": (notes / "notes.md").read_text(encoding="utf-8") if notes and (notes / "notes.md").is_file() else "",
        "cases": len(load_cases(spec)) if spec.cases_path() else 0,
        "last_test": last_test(env, spec),
    }


def agent_or_error(env, id: str):
    try:
        return env.get(id)
    except KeyError:
        raise UiError(f"There is no agent called {id}.") from None


def save_agent(state: AppState, id: str, settings: dict, prompt: str | None = None) -> dict:
    """Write the person's copy: only the settings that differ from the
    shipped agent's (so a later update of the shipped one still reaches the
    rest), and the prompt if it differs."""
    env = environment(state)
    spec = agent_or_error(env, id)
    shipped = agents.load(id, None, env.tier, env.model_id) if spec.folder else None
    folder = agents.override_dir(state.tuning_path) / id
    folder.mkdir(parents=True, exist_ok=True)
    # Start from what the person's copy already says, keeping its tier and
    # model tables and any setting the form does not show.
    old = (folder / "agent.toml").read_text(encoding="utf-8") if (folder / "agent.toml").is_file() else ""
    try:
        fields = {k: v for k, v in tomllib.loads(old).items() if k not in ("tier", "model")}
    except ValueError:
        fields = {}
    for key in EDITABLE:
        if key not in settings:
            continue
        value = settings[key]
        if shipped is not None and getattr(shipped, key) == value:
            fields.pop(key, None)
        else:
            fields[key] = value
    tables = old[old.find("\n["):] if "\n[" in old else ""
    text = "# Your changes to this agent; the rest comes from the shipped agent.toml.\n" + toml_lines(fields) + tables
    candidate = folder / "agent.toml.new"
    candidate.write_text(text, encoding="utf-8", newline="\n")
    candidate.replace(folder / "agent.toml")
    if prompt is not None:
        shipped_prompt = (spec.folder / "prompt.md").read_text(encoding="utf-8") if spec.folder and (spec.folder / "prompt.md").is_file() else None
        if prompt.strip() and prompt != shipped_prompt:
            (folder / "prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
        elif (folder / "prompt.md").is_file():
            put_aside(folder / "prompt.md", state)
    return get_agent(state, id)


def put_aside(path: Path, state: AppState) -> Path:
    """Nothing the person made is deleted: it goes to agents/.old/."""
    old = agents.override_dir(state.tuning_path) / ".old"
    old.mkdir(parents=True, exist_ok=True)
    target = old / f"{time.strftime('%Y%m%d-%H%M%S')}-{path.parent.name}-{path.name}"
    shutil.move(str(path), str(target))
    return target


def revert_agent(state: AppState, id: str) -> dict:
    """Back to the shipped agent: the person's copy is put aside, not deleted."""
    env = environment(state)
    spec = agent_or_error(env, id)
    if spec.override is None:
        return get_agent(state, id)
    if spec.folder is None:
        raise UiError("This agent is your own; there is no shipped one to go back to.")
    put_aside(spec.override, state)
    return get_agent(state, id)


def new_agent(state: AppState, id: str, name: str = "", goal: str = "", copy_of: str | None = None) -> dict:
    """A new agent of the person's own, empty or as a copy of another."""
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", id or ""):
        raise UiError("An agent's id is lower-case words joined by -, like seed-keeper.")
    env = environment(state)
    if id in agents.agent_ids(env.shipped, env.overrides):
        raise UiError(f"There is already an agent called {id}.")
    folder = agents.override_dir(state.tuning_path) / id
    folder.mkdir(parents=True)
    if copy_of:
        source = agent_or_error(env, copy_of)
        settings = {k: v for k, v in agents.as_settings(source).items()
                    if k not in ("shared_rules", "prompt_from", "cases")}
        settings["name"] = name or f"{source.name} (copy)"
        settings["goal"] = goal or source.goal
        settings["arithmetic_shortcut"] = False if source.prompt_from == "tuning" else settings["arithmetic_shortcut"]
        prompt = tuning.load(state.tuning_path).system_prompt if source.prompt_from == "tuning" else \
            (source.override or source.folder) and read_first(source, "prompt.md")
        for folder_from in (source.override, source.folder):
            if folder_from and (folder_from / source.cases).is_file():
                shutil.copyfile(folder_from / source.cases, folder / "cases.toml")
                break
    else:
        settings = {"name": name or id.replace("-", " ").capitalize(), "goal": goal or "(What this agent does.)",
                    "min_tier": "Capable", "tools": ["search_library", "calculate"],
                    "guards": ["calculate_first", "call_again", "cite_or_stand"], "max_rounds": 8}
        prompt = f"You are the {settings['name'].lower()}. {settings['goal']}\n\nHow to work:\n1. \n"
    (folder / "agent.toml").write_text(toml_lines(settings), encoding="utf-8", newline="\n")
    (folder / "prompt.md").write_text(prompt or "", encoding="utf-8", newline="\n")
    if not (folder / "cases.toml").exists():
        (folder / "cases.toml").write_text("# Test cases: save one from a run, or write [[cases]] here.\n",
                                           encoding="utf-8", newline="\n")
    return get_agent(state, id)


def read_first(spec, name: str) -> str:
    for folder in (spec.override, spec.folder):
        if folder and (folder / name).is_file():
            return (folder / name).read_text(encoding="utf-8")
    return ""


def save_notes(state: AppState, id: str, text: str) -> None:
    env = environment(state)
    folder = env.workspace(agent_or_error(env, id))
    if folder is None:
        raise UiError("This agent keeps no notes.")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.md").write_text(text, encoding="utf-8", newline="\n")


def agent_runs(state: AppState, id: str) -> list[dict]:
    env = environment(state)
    return list_logs(env.work, agent_or_error(env, id).id)


def read_run(state: AppState, id: str, run: str) -> list[dict]:
    env = environment(state)
    if not re.fullmatch(r"[0-9A-Za-z-]+", run):
        raise UiError("No such run.")
    return read_log(env.work / agent_or_error(env, id).id / "runs" / f"{run}.jsonl")


def save_test_case(state: AppState, id: str, case: dict) -> str:
    """A run made into a test case: the task as its prompt and what the
    person says the answer must (or must not) contain."""
    env = environment(state)
    spec = agent_or_error(env, id)
    if spec.cases == agents.CASES_BENCHMARK:
        raise UiError("The librarian's cases are the benchmark (evals/benchmark.toml); add the case there.")
    clean = {k: v for k, v in case.items() if v not in (None, "", [])}
    if not re.fullmatch(r"[a-z0-9-]+", clean.get("id", "")):
        raise UiError("A case id is lower-case words joined by -, like drum-dose.")
    try:
        return str(save_case(spec, agents.override_dir(state.tuning_path), clean))
    except (ValueError, TypeError) as e:
        raise UiError(str(e)) from None


def test_agent(state: AppState, id: str, only: str | None = None) -> dict:
    """Run the agent's cases; each case's result arrives as an
    "agent-test-progress" event as it is scored."""
    from museion.commands import make_asker
    env = environment(state)
    spec = agent_or_error(env, id)
    cfg = state.config()
    chat = Llama(local(cfg.chat_port))
    if not chat.is_up():
        raise UiError("The chat model isn't running. Start services first.")

    def report(index: int, total: int, case_id: str, passed: bool | None) -> None:
        state.emit("agent-test-progress", {"agent": id, "index": index, "total": total, "id": case_id, "passed": passed})

    if not GPU.acquire(timeout=1):
        raise UiError("A chat is working; test when it has finished.")
    try:
        tune = tuning.load(state.tuning_path)
        with closing(Index(cfg.index_path())) as index:
            run = run_tests(env, prepared(spec, tune), chat, make_asker(state, cfg, index, chat), tune, only, "", report)
    finally:
        GPU.release()
    return dataclasses.asdict(run)


def agent_tests(state: AppState, id: str) -> list[dict]:
    env = environment(state)
    folder = tests_dir(env, agent_or_error(env, id))
    return [dataclasses.asdict(r) for r in evals.list_runs(folder)] if folder.is_dir() else []


def export_agent(state: AppState, id: str) -> str:
    """The agent as a zip in the library's agent-exports folder, to carry to
    another drive: the shipped files with the person's copy over them."""
    env = environment(state)
    spec = agent_or_error(env, id)
    out = env.work.parent / "agent-exports"
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{id}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        names = {}
        for folder in (spec.folder, spec.override):
            if folder:
                for p in folder.iterdir():
                    if p.is_file() and p.suffix in (".toml", ".md"):
                        names[p.name] = p
        for name, p in names.items():
            if name == "agent.toml" and spec.folder and spec.override:
                text = toml_lines({k: v for k, v in agents.as_settings(spec).items() if v is not None})
                z.writestr(f"{id}/agent.toml", text)
            else:
                z.write(p, f"{id}/{name}")
    return str(target)


def import_agent(state: AppState, path: str) -> dict:
    """An exported agent's zip, unpacked as one of the person's own agents."""
    src = Path(path)
    if not zipfile.is_zipfile(src):
        raise UiError(f"{src.name} is not an exported agent (.zip).")
    with zipfile.ZipFile(src) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        ids = {n.split("/")[0] for n in names}
        if len(ids) != 1 or any(n.count("/") != 1 or not n.endswith((".toml", ".md")) for n in names):
            raise UiError("That zip is not an exported agent: it should hold one folder of .toml and .md files.")
        id = ids.pop()
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", id):
            raise UiError(f"{id} is not an agent id.")
        target = agents.override_dir(state.tuning_path) / id
        if target.exists():
            put_aside(target, state)
        target.mkdir(parents=True)
        for n in names:
            (target / n.split("/")[1]).write_bytes(z.read(n))
    return get_agent(state, id)
