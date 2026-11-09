"""Running an agent: what the page, the terminal and the tests share.

An Agents object knows where agents, their folders and the chats are on this
computer, and which tier and model they are resolved for. run() runs one
agent on one message of a chat; test() runs an agent's own cases.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from museion import agents, evals
from museion.agents import AgentSpec
from museion.answer import Answer, Exchange
from museion.harness import Approval, Harness, Person, Setting
from museion.runs import WORK, ChatStore, RunLog


@dataclass
class Agents:
    library_dir: Path | None  # the library: agent-work/ and chats/ live in it
    overrides: Path | None  # the person's agents folder (agents.override_dir)
    tier: str
    model_id: str = ""
    work_dir: Path | None = None  # where agent-work is, if not in the library
    shipped: Path = agents.SHIPPED_DIR

    @property
    def work(self) -> Path:
        if self.work_dir is not None:
            return self.work_dir
        if self.library_dir is None:
            raise RuntimeError("no library is configured, so agents have nowhere to keep their work")
        return self.library_dir / WORK

    def chats(self) -> ChatStore:
        if self.library_dir is None:
            raise RuntimeError("no library is configured, so there is nowhere to keep chats")
        return ChatStore(self.work.parent / "chats" if self.work_dir else self.library_dir / "chats")

    def all(self) -> list[AgentSpec]:
        return agents.load_all(self.overrides, self.tier, self.model_id, self.shipped)

    def get(self, agent_id: str) -> AgentSpec:
        if agent_id not in agents.agent_ids(self.shipped, self.overrides):
            raise KeyError(agent_id)
        return agents.load(agent_id, self.overrides, self.tier, self.model_id, self.shipped)

    @property
    def notes_dir(self) -> Path:
        """The person's notes and records, beside agent-work and chats."""
        return self.work.parent / "notes"

    def workspace(self, spec: AgentSpec, chat_id: str | None = None) -> Path | None:
        return agents.workspace(self.work, spec, chat_id)

    def setting(self, chat, library=None, person: Person | None = None, chat_id: str | None = None,
                on_step: Callable[[str, dict], None] = lambda k, d: None,
                stop: threading.Event | None = None, tuning=None, log: bool = True) -> Setting:
        def find(agent_id: str) -> AgentSpec:
            # A helper must be one the person could start a chat with here:
            # turned on, and not too big for this computer.
            spec = prepared(self.get(agent_id), tuning)
            if not spec.enabled:
                spec.problems.append("it is turned off")
            why = agents.tier_problem(spec, self.tier)
            if why:
                spec.problems.append(why)
            return spec

        try:
            from museion.notes import adopt_old_files
            adopt_old_files(self.work, self.notes_dir)
        except RuntimeError:
            pass  # no library: nothing to adopt
        return Setting(
            chat=chat, library=library, library_dir=self.library_dir, notes_dir=self.notes_dir,
            person=person or Person(), find_agent=find,
            workspace_for=lambda spec: self.workspace(spec, chat_id),
            open_log=(lambda spec: RunLog(self.work / spec.id / "runs")) if log else (lambda spec: None),
            on_step=on_step, stop=stop,
        )

    def run(self, spec: AgentSpec, goal: str, setting: Setting, history: Sequence[Exchange] = (),
            on_token: Callable[[str | None], None] = lambda t: None, chat_id: str | None = None,
            tuning=None) -> Answer:
        problem = (spec.problems[0] if spec.problems else None) or agents.tier_problem(spec, self.tier)
        if problem:
            raise RuntimeError(f"{spec.name} cannot run: {problem}")
        spec = prepared(spec, tuning)
        harness = Harness(spec, setting, self.workspace(spec, chat_id))
        return harness.run(goal, history, on_token)


def prepared(spec: AgentSpec, tuning) -> AgentSpec:
    """The librarian's prompt and limits come from tuning.toml, which the
    Settings tab edits."""
    if spec.prompt_from == "tuning" and tuning is not None:
        spec.prompt = tuning.system_prompt
        spec.max_rounds = tuning.max_rounds
        spec.answer_tokens = tuning.answer_tokens
        spec.max_continuations = tuning.max_continuations
        spec.max_history = tuning.max_history
        spec.history_chars = tuning.history_chars
    return spec


class ScriptedPerson(Person):
    """The person in a test case: presses what the case says, and answers
    questions from its list."""

    def __init__(self, approve: str = "deny", says: Sequence[str] = ()) -> None:
        self.decision = approve
        self.says = list(says)
        self.asked: list[str] = []

    def approve(self, request: Approval) -> tuple[str, str]:
        return ("allow", "") if self.decision == "allow" else ("deny", "Not now.")

    def ask(self, agent: str, question: str) -> str:
        self.asked.append(question)
        return self.says.pop(0) if self.says else "I don't know."


class CaseRunner:
    """What evals.run calls for an agent: each case in a fresh scratch
    folder, holding the case's given files, so a test never touches the
    agent's real notes."""

    def __init__(self, env: Agents, spec: AgentSpec, chat, library=None, tuning=None) -> None:
        self.env, self.spec, self.chat, self.library, self.tuning = env, spec, chat, library, tuning
        self.reranker = getattr(library, "reranker", None)

    def ask_case(self, case: evals.Case) -> Answer:
        scratch = Path(tempfile.mkdtemp(prefix=f"museion-{self.spec.id}-"))
        try:
            for name, text in case.given_files.items():
                target = (scratch / name).resolve()
                if scratch.resolve() not in target.parents:
                    raise ValueError(f"given file {name} is outside the agent's folder")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            setting = self.env.setting(self.chat, self.library, ScriptedPerson(case.approve, case.person_says),
                                       tuning=self.tuning, log=False)
            # Each case's run log (and its helpers'), beside the test results:
            # what a failed case asked, searched and was told.
            logs = tests_dir(self.env, self.spec) / "logs"
            setting.open_log = lambda spec: RunLog(logs)
            setting.workspace_for = lambda spec: scratch / spec.id
            # Its own notes too: a case's given notes/... files, and never
            # the person's.
            setting.notes_dir = scratch / "notes"
            spec = prepared(self.env.get(self.spec.id), self.tuning)
            return Harness(spec, setting, scratch if spec.memory != "none" else None).run(case.prompt)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def load_cases(spec: AgentSpec) -> list[evals.Case]:
    path = spec.cases_path()
    if path is None or not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    return evals.load_cases(text, agent=spec.cases != agents.CASES_BENCHMARK)


def count_cases(spec: AgentSpec) -> int:
    """How many test cases the agent has; a cases.toml that will not load is
    added to its problems (a typo there must not stop `agent check`)."""
    try:
        return len(load_cases(spec))
    except (OSError, ValueError, TypeError) as e:
        spec.problems.append(f"cases: {e}")
        return 0


def tests_dir(env: Agents, spec: AgentSpec) -> Path:
    return env.work / spec.id / "tests"


def test(env: Agents, spec: AgentSpec, chat, library=None, tuning=None, only: str | None = None,
         label: str = "", report=lambda *a: None) -> evals.Run:
    """Run the agent's cases and save the run beside its others."""
    cases = [c for c in load_cases(spec) if not only or only in (c.id, c.capability, c.horizon)]
    if not cases:
        raise RuntimeError(f"{spec.name} has no test cases" + (f" matching {only}" if only else ""))
    run = evals.run(CaseRunner(env, spec, chat, library, tuning), cases, label or spec.name, report)
    evals.save_run(tests_dir(env, spec), run)
    return run


def save_case(spec: AgentSpec, overrides: Path, case: dict) -> Path:
    """Add a case to the person's copy of the agent's cases (made from the
    shipped one the first time), and return the file."""
    from museion.catalog_add import toml_lines
    folder = overrides / spec.id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / spec.cases
    if not target.exists() and spec.folder and (spec.folder / spec.cases).is_file():
        shutil.copyfile(spec.folder / spec.cases, target)
    existing = {c.id for c in evals.load_cases(target.read_text(encoding="utf-8"), agent=True)} if target.exists() else set()
    if case["id"] in existing:
        raise ValueError(f"there is already a case called {case['id']}")
    entry = "\n[[cases]]\n" + toml_lines(case)
    evals.load_cases(entry, agent=True)  # refuse a case that would not load
    with open(target, "a", encoding="utf-8", newline="\n") as f:
        f.write(entry)
    return target
