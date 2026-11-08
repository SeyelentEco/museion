"""Agents as files: finding them, layering their settings, checking them.

An agent is a folder: agents/<id>/agent.toml (its goal, tools, guards and
limits) and prompt.md (what it is told), plus cases.toml (its tests). The
folders shipped with Museion are in agents/ beside this package. A person's
own agents, and their changes to shipped ones, are in an agents/ folder in
the config folder (beside config.json and tuning.toml), so an update of
Museion never overwrites them.

Settings are layered, each layer changing only the fields it names:

  1. the shipped agent.toml
  2. its [tier.<tier>] table, for this computer's tier (hardware.py)
  3. its [model."<id>"] table, for the chat model that is loaded
  4. the person's agent.toml of the same id, layered the same way

`python -m museion agent check` prints the result for this computer.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from museion import guards, hardware

SHIPPED_DIR = Path(__file__).resolve().parent.parent / "agents"
RULES_FILE = "_rules.md"

MEMORY = ("agent", "chat", "none")
CONFIRM = ("ask", "all")
CASES_BENCHMARK = "benchmark"  # the librarian's cases are the benchmark itself


@dataclass
class AgentSpec:
    id: str
    name: str = ""
    goal: str = ""
    min_tier: str = hardware.MODEST
    tools: list[str] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)
    max_rounds: int = 8
    answer_tokens: int = 1500
    max_continuations: int = 2
    # Earlier messages of the chat carried into a new one, and how much of
    # each reply (characters).
    max_history: int = 4
    history_chars: int = 2500
    # Compaction: when the conversation would not fit, old tool results are
    # replaced by a one-line note, except those of the last keep_rounds rounds.
    compact: bool = True
    keep_rounds: int = 2
    # The librarian's ways: search the question's subjects before the model
    # speaks, and answer bare arithmetic without a model.
    seed_search: bool = False
    arithmetic_shortcut: bool = False
    # Whose notes and files: the agent's, shared by all its chats; each
    # chat's own; or none at all.
    memory: str = "agent"
    # "all" asks before workspace writes too, not only before ask-risk tools.
    confirm: str = "ask"
    # _rules.md goes before every prompt. Only a shipped agent may turn it off.
    shared_rules: bool = True
    # "file": prompt.md. "tuning": tuning.toml's system_prompt (the librarian,
    # so the Settings override of it keeps working).
    prompt_from: str = "file"
    enabled: bool = True
    examples: list[str] = field(default_factory=list)
    cases: str = "cases.toml"

    # Not settings: where it came from, what it says, and what is wrong.
    prompt: str = ""
    folder: Path | None = None
    override: Path | None = None
    problems: list[str] = field(default_factory=list)

    def tools_risk(self) -> str:
        """The riskiest kind of tool this agent has."""
        from museion.agent_tools import READ, RISKS, all_tools
        known = all_tools()
        risks = [known[t].risk for t in self.tools if t in known]
        return max(risks, key=RISKS.index) if risks else READ

    def cases_path(self) -> Path | None:
        if self.cases == CASES_BENCHMARK:
            from museion.evals import BENCHMARK_PATH
            return BENCHMARK_PATH
        # The person's cases when they have their own, else the shipped ones;
        # new cases saved from a run go to the person's folder.
        for folder in (self.override, self.folder):
            if folder and (folder / self.cases).is_file():
                return folder / self.cases
        return None


SETTINGS = [f.name for f in dataclasses.fields(AgentSpec)
            if f.name not in ("id", "prompt", "folder", "override", "problems")]


def override_dir(tuning_path: Path) -> Path:
    """The person's agents: beside tuning.toml in the config folder."""
    return Path(tuning_path).parent / "agents"


def agent_ids(*folders: Path | None) -> list[str]:
    ids: set[str] = set()
    for folder in folders:
        if folder and folder.is_dir():
            ids |= {p.name for p in folder.iterdir() if (p / "agent.toml").is_file()}
    return sorted(ids)


def load_all(overrides: Path | None, tier: str, model_id: str = "",
             shipped: Path = SHIPPED_DIR) -> list[AgentSpec]:
    """Every agent, shipped and the person's own, resolved for this tier and
    model. Ones with problems are included, with the problems listed."""
    return [load(i, overrides, tier, model_id, shipped) for i in agent_ids(shipped, overrides)]


def load(agent_id: str, overrides: Path | None, tier: str, model_id: str = "",
         shipped: Path = SHIPPED_DIR) -> AgentSpec:
    spec = AgentSpec(id=agent_id)
    base = shipped / agent_id
    mine = overrides / agent_id if overrides else None
    values: dict = {}
    for layer, folder in (("shipped", base), ("yours", mine)):
        if folder is None or not (folder / "agent.toml").is_file():
            continue
        if layer == "shipped":
            spec.folder = folder
        else:
            spec.override = folder
        try:
            data = tomllib.loads((folder / "agent.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            spec.problems.append(f"{folder / 'agent.toml'}: {e}")
            continue
        if layer == "yours" and spec.folder is not None:
            strip_locked(data, spec.problems)
        values.update(layered(data, tier, model_id, spec.problems))
    for key, value in values.items():
        setattr(spec, key, value)
    if not spec.name:
        spec.name = agent_id.replace("-", " ").capitalize()
    spec.prompt = read_prompt(spec, tier, shipped)
    spec.problems += problems_of(spec)
    return spec


# Settings the person's copy of a shipped agent may not change: the shared
# rules hold the safety rules (no doses from memory, the calculator only),
# and where the prompt comes from decides whether they are there at all.
LOCKED = ("shared_rules", "prompt_from")


def strip_locked(data: dict, problems: list[str]) -> None:
    """Take the locked settings out of a person's agent.toml -- at the top
    and inside its [tier.*] and [model.*] tables -- saying so for each."""
    tables = [data]
    for group in ("tier", "model"):
        if isinstance(data.get(group), dict):
            tables += [t for t in data[group].values() if isinstance(t, dict)]
    for table in tables:
        for key in LOCKED:
            if key in table:
                table.pop(key)
                problems.append(f"your copy may not change {key}; the shipped agent's stays")


def layered(data: dict, tier: str, model_id: str, problems: list[str]) -> dict:
    """One agent.toml's settings with its tier and model tables applied."""
    data = dict(data)
    tiers = data.pop("tier", {}) or {}
    models = data.pop("model", {}) or {}
    out = {}
    for table in (data, tiers.get(tier, {}), matching_model(models, model_id)):
        for key, value in table.items():
            if key not in SETTINGS:
                problems.append(f"unknown setting {key!r}")
                continue
            out[key] = value
    for name in tiers:
        if name not in hardware.TIERS:
            problems.append(f"[tier.{name}] names no tier; the tiers are {', '.join(hardware.TIERS)}")
    return out


def matching_model(models: dict, model_id: str) -> dict:
    """The [model."<id>"] table for the loaded model. The key may be the
    catalogue's id ("qwen3.6-35b-a3b-q4") or part of the file name the
    server reports ("Qwen3.6-35B-A3B"); the longest match wins."""
    if not model_id:
        return {}
    have = {norm(model_id)}
    try:
        from museion import catalog
        for m in catalog.models():
            if norm(m.filename) == norm(model_id):
                have.add(norm(m.id))
    except Exception:  # noqa: BLE001 -- a broken catalogue only loses this convenience
        pass
    best = ""
    for key in models:
        if any(norm(key) in h for h in have) and len(key) > len(best):
            best = key
    return models[best] if best else {}


def norm(name: str) -> str:
    return re.sub(r"\.gguf$", "", name.strip().lower())


def read_prompt(spec: AgentSpec, tier: str, shipped: Path) -> str:
    """prompt.<tier>.md if there is one for this tier, else prompt.md; the
    person's copy before the shipped one; _rules.md first."""
    text = ""
    for folder in (spec.override, spec.folder):
        if folder is None:
            continue
        for name in (f"prompt.{tier}.md", "prompt.md"):
            if (folder / name).is_file():
                text = (folder / name).read_text(encoding="utf-8").strip()
                break
        if text:
            break
    if spec.shared_rules and (shipped / RULES_FILE).is_file():
        rules = (shipped / RULES_FILE).read_text(encoding="utf-8").strip()
        text = f"{rules}\n\n{text}" if text else rules
    return text


def problems_of(spec: AgentSpec) -> list[str]:
    from museion.agent_tools import all_tools
    out = []
    known = all_tools()
    for t in spec.tools:
        if t not in known:
            out.append(f"no tool is called {t}")
    for g in spec.guards:
        if g not in guards.GUARDS:
            out.append(f"no guard is called {g}; the guards are {', '.join(guards.GUARDS)}")
    if spec.min_tier not in hardware.TIERS:
        out.append(f"min_tier {spec.min_tier!r} is not one of {', '.join(hardware.TIERS)}")
    if spec.memory not in MEMORY:
        out.append(f"memory must be one of {', '.join(MEMORY)}")
    if spec.confirm not in CONFIRM:
        out.append(f"confirm must be one of {', '.join(CONFIRM)}")
    if spec.prompt_from not in ("file", "tuning"):
        out.append("prompt_from must be file or tuning")
    for name in ("max_rounds", "answer_tokens", "max_history", "history_chars", "keep_rounds"):
        value = getattr(spec, name)
        if not isinstance(value, int) or isinstance(value, bool) or value < (1 if name in ("max_rounds", "answer_tokens") else 0):
            out.append(f"{name} must be a whole number")
    for name in ("tools", "guards", "examples"):
        if not isinstance(getattr(spec, name), list):
            out.append(f"{name} must be a list")
    if spec.prompt_from == "file" and not spec.prompt.strip():
        out.append("prompt.md is missing or empty")
    if not spec.goal:
        out.append("goal is empty")
    return out


def tier_problem(spec: AgentSpec, tier: str) -> str | None:
    """Why this agent cannot run on a computer of this tier, or None."""
    if spec.min_tier in hardware.TIERS and hardware.TIERS.index(tier) < hardware.TIERS.index(spec.min_tier):
        return f"Needs a {spec.min_tier} computer or better; this one is {tier}."
    return None


def workspace(root: Path, spec: AgentSpec, chat_id: str | None = None) -> Path | None:
    """The agent's own folder under `root` (the library's agent-work), or a
    chat's own folder inside it when the agent keeps memory per chat."""
    if spec.memory == "none":
        return None
    folder = Path(root) / spec.id
    if spec.memory == "chat" and chat_id:
        folder = folder / "chats" / chat_id
    return folder


def as_settings(spec: AgentSpec) -> dict:
    """The resolved settings, as agent check prints them and the page edits them."""
    return {k: getattr(spec, k) for k in SETTINGS}
