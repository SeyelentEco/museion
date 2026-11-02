"""The harness: runs any agent, on the local model, with its tools and guards.

One loop for every agent, Hypatia included. A turn of the model either calls
tools -- which the harness checks, asks the person about if they are risky,
runs and answers -- or answers, and then the agent's guards may send the
answer back once each (guards.py). The loop ends when an answer stands, or
when the rounds run out and the model is told to answer with what it has.

What the answer may claim is decided here from what actually ran, not from
anything the model says: which passages it cites, whether the calculator ran.

Everything that happens is written to the run's log, one JSON line per step
(runs.py), and passed to `on_step` for the page to show.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from museion import calc, guards, llama
from museion.agent_tools import ASK, WORKSPACE, Tool, ToolError, all_tools, check_args
from museion.agents import AgentSpec
from museion.answer import DISCARD, Answer, Calculation, Exchange, basis_of
from museion.compaction import compact, prompt_chars
from museion.model_text import cited_numbers, looks_like_leaked_tool_call, renumber_citations, strip_citations

# For keeping a conversation inside the model's context: characters per token,
# erring low (English prose runs nearer four), and tokens kept free for the
# tool definitions and the chat template around the messages.
CHARS_PER_TOKEN = 3.5
RESERVED_TOKENS = 1500

# Room kept for the next tool result when it is not a search (whose size the
# library knows: its passages times their length).
NEXT_RESULT_CHARS = 6000

# Longest tool result put in front of the model, in characters.
MAX_RESULT_CHARS = 24000

# How much of the agent's notes.md goes into its prompt.
NOTES_CHARS = 3000


class Stopped(Exception):
    """The person pressed Stop."""


@dataclass
class Approval:
    """A tool call waiting for the person: shown as an Allow / Deny card."""

    agent: str
    tool: str
    args: dict
    summary: str  # what it will do, in words
    reason: str  # what the model said on its way to the call


class Person:
    """Whoever is at the screen. This one is nobody: risky calls are refused
    and questions go unanswered. The page and the terminal supply real ones."""

    def approve(self, request: Approval) -> tuple[str, str]:
        """("allow" | "allow_run" | "deny", a note from the person)."""
        return "deny", "Nobody is here to approve this."

    def ask(self, agent: str, question: str) -> str:
        return "Nobody is here to answer. Carry on with what you have, and say what you assumed."


@dataclass
class Setting:
    """What a run needs from around it."""

    chat: llama.Llama
    library: object | None = None  # ask.Asker: gather, gather_seeded, seed_queries
    library_dir: Path | None = None  # the library folder, for the tools that list it
    person: Person = field(default_factory=Person)
    find_agent: Callable[[str], AgentSpec] | None = None  # for delegate
    workspace_for: Callable[[AgentSpec], Path | None] = lambda spec: None
    open_log: Callable[[AgentSpec], object | None] = lambda spec: None  # -> runs.RunLog
    on_step: Callable[[str, dict], None] = lambda kind, data: None
    stop: threading.Event | None = None


class Harness:
    def __init__(self, spec: AgentSpec, setting: Setting, workspace: Path | None = None,
                 depth: int = 0) -> None:
        self.spec = spec
        self.setting = setting
        self.chat = setting.chat
        self.library = setting.library
        self.person = setting.person
        self.workspace = workspace
        self.depth = depth
        known = all_tools()
        self.tools: dict[str, Tool] = {n: known[n] for n in spec.tools if n in known}
        # What tools add to as they run (see agent_tools/).
        self.sources: list = []
        self.calculations: list[Calculation] = []
        self.files: list[str] = []
        self.used: list[str] = []
        self.reranked = False
        self.round = 0
        self.max_rounds = spec.max_rounds
        self.allowed: set[str] = set()  # calls the person allowed for the rest of the run
        self.log = None
        self.started = time.monotonic()

    # ── the loop ─────────────────────────────────────────────────────────────

    def run(self, goal: str, history: Sequence[Exchange] = (),
            on_token: Callable[[str | None], None] = lambda t: None) -> Answer:
        spec = self.spec
        if spec.arithmetic_shortcut and calc.looks_like_arithmetic(goal):
            return self.compute(goal, on_token)
        self.log = self.setting.open_log(spec)
        self.event("start", agent=spec.id, goal=goal, depth=self.depth,
                   model=self.model_name(), workspace=str(self.workspace or ""))
        stream = self.stoppable(on_token)
        try:
            answer = self.loop(goal, history, stream)
        except Stopped:
            self.event("stopped")
            raise
        except Exception as e:
            self.event("error", error=str(e))
            raise
        answer.log = str(getattr(self.log, "path", "") or "")
        self.event("done", text=answer.text, basis=answer.basis, rounds=answer.rounds,
                   sources=[f"{h.doc_title} p.{h.page} [{h.book}]" for h in answer.sources],
                   calculations=[f"{c.expression} = {c.result}" for c in answer.calculations],
                   files=answer.files, seconds=round(time.monotonic() - self.started, 1))
        return answer

    def loop(self, goal: str, history: Sequence[Exchange], on_token) -> Answer:
        spec = self.spec
        messages = [llama.system(self.system_prompt())]
        # Earlier exchanges give a follow-up its subject. Their citation
        # markers are stripped: those numbers pointed at passages from an
        # earlier search, and would otherwise be repeated as if they named the
        # passages found for this question.
        for x in list(history)[max(len(history) - spec.max_history, 0):]:
            messages.append(llama.user(x.question))
            messages.append(llama.assistant(strip_citations(x.answer[:spec.history_chars])))
        messages.append(llama.user(goal))
        if spec.seed_search and self.library is not None and "search_library" in self.tools:
            self.seed(goal, history, messages)

        fired: set[str] = set()
        room = self.room()
        while True:
            self.check_stop()
            # Another tool result must fit beside everything already here, or
            # the server refuses the whole conversation and the answer is
            # lost: at a 16k context, two or three searches did that. Old
            # results are folded away first, if the agent allows; without the
            # room, the model answers from what it has.
            if self.round < spec.max_rounds and room is not None and prompt_chars(messages) > room:
                if spec.compact:
                    removed = compact(messages, room, spec.keep_rounds)
                    if removed:
                        self.event("compact", removed=removed)
                if prompt_chars(messages) > room:
                    self.mark("context nearly full; answering from what was found")
                    self.round = spec.max_rounds
            # On the last round the tools are withheld so the model must answer.
            last = self.round >= spec.max_rounds or not self.tools
            if self.round == spec.max_rounds and self.tools:
                messages.append(llama.user(guards.NO_MORE_TOOLS))
            self.round += 1
            schemas = None if last else [t.schema() for t in self.tools.values()]
            turn = self.chat.turn(messages, schemas, spec.answer_tokens, on_token)
            self.mark(f"turn {self.round}: {len(turn.tool_calls)} tool call(s), {len(turn.text)} chars")
            self.event("turn", round=self.round, calls=len(turn.tool_calls), chars=len(turn.text))

            if turn.tool_calls:
                # Whatever it narrated on the way to the tool call is not the
                # answer, and may contain numbers it made up. Take it back.
                on_token(DISCARD)
                messages.append(llama.assistant_calls(turn.text, turn.tool_calls))
                for call in turn.tool_calls:
                    messages.append(llama.tool_result(call["id"], self.call(call, turn.text)))
                continue

            text = self.finish(turn, messages, on_token)
            objection = guards.check(self.guard_names(), text, self, fired)
            if objection is not None:
                name, push = objection
                fired.add(name)
                self.event("guard", guard=name, message=push.message)
                on_token(DISCARD)
                messages.append(llama.assistant(text))
                messages.append(llama.user(push.message))
                if push.withhold_tools:
                    self.round = spec.max_rounds + 1
                continue

            if looks_like_leaked_tool_call(text):
                on_token(DISCARD)
                on_token(guards.NO_ANSWER)
                return Answer(guards.NO_ANSWER, [], self.reranked, basis_of(False, False),
                              self.calculations, self.used, self.round, self.files)
            cited = [n for n in cited_numbers(text) if n <= len(self.sources)]
            used = [self.sources[n - 1] for n in cited]
            return Answer(renumber_citations(text, cited), used, self.reranked,
                          basis_of(bool(used), bool(self.calculations), self.used), self.calculations,
                          self.used, self.round, self.files)

    def guard_names(self) -> list[str]:
        """The agent's guards, plus those it gets for having a tool."""
        extra = [g for g, tool in guards.AUTOMATIC.items() if tool in self.tools and g not in self.spec.guards]
        return list(self.spec.guards) + extra

    def seed(self, goal: str, history: Sequence[Exchange], messages: list[dict]) -> None:
        """The library is searched before the model says a word, for the
        question's plain subjects as well as its words (see ask.py for why),
        and the search is replayed as a tool exchange so the model sees it
        exactly as it would its own."""
        from museion.ask import render_search
        queries = self.library.seed_queries(goal, history)
        self.mark(f"rewrite -> {queries}")
        hits, reranked = self.library.gather_seeded(goal, queries, set())
        self.reranked |= reranked
        self.mark(f"seeded search ({len(queries) + 1} queries): {len(hits)} passages")
        result = render_search(hits, self.sources)
        call = {"id": "seed_search", "type": "function", "function": {
            "name": "search_library",
            "arguments": json.dumps({"query": " | ".join(queries + [goal])},
                                    separators=(",", ":"), ensure_ascii=False)}}
        messages.append(llama.assistant_calls("", [call]))
        messages.append(llama.tool_result("seed_search", result))
        self.event("tool", tool="search_library", args={"query": " | ".join(queries + [goal])},
                   result=result[:4000], seeded=True)

    def finish(self, turn: llama.Turn, messages: list[dict], on_token) -> str:
        """The text of a turn that answered, carried on past the token limit.
        An answer that ran into the limit is asked to carry on from the cut;
        if it runs out again after the allowed continuations, it says so
        rather than end mid-sentence as if that were the whole answer."""
        text, limited = turn.text, turn.hit_token_limit
        piece = text
        continuations = 0
        while limited and continuations < self.spec.max_continuations:
            # Only the piece just written: replaying all the text so far put the
            # first part in the context twice, and a garden plan's second
            # continuation started over from an earlier step.
            messages.append(llama.assistant(piece))
            messages.append(llama.user(guards.CONTINUE))
            more = self.chat.turn(messages, None, self.spec.answer_tokens, on_token)
            piece = more.text
            if not text[-1:].isspace() and not more.text[:1].isspace():
                text += " "
                on_token(" ")
            text += more.text
            limited = more.hit_token_limit
            continuations += 1
        if limited:
            text += guards.CUT_SHORT
            on_token(guards.CUT_SHORT)
        return text

    def compute(self, question: str, on_token) -> Answer:
        """The answer to a question that is nothing but arithmetic: no model."""
        expr = calc.normalise(question)
        calculations = []
        try:
            result = calc.format_number(calc.evaluate(expr))
            text = f"{expr} = **{result}**"
            calculations.append(Calculation(expr, result))
        except ValueError as e:
            text = f"Could not evaluate `{expr}`: {e}"
        on_token(text)
        return Answer(text, [], False, basis_of(False, bool(calculations)), calculations)

    # ── tools ────────────────────────────────────────────────────────────────

    def call(self, call: dict, said: str) -> str:
        """Carry out one tool call and return what to tell the model."""
        name = call["function"]["name"]
        raw = call["function"].get("arguments") or ""
        tool = self.tools.get(name)
        if tool is None:
            return f"Unknown tool {name}."
        args = parse_args(tool, raw)
        problem = check_args(tool, args)
        if problem:
            self.event("tool", tool=name, args=args, result=problem, refused=True)
            return problem
        if self.needs_approval(tool):
            key = f"{name}:{json.dumps(args, sort_keys=True)}"
            if key not in self.allowed:
                request = Approval(self.spec.name, name, args, tool.summary(args), said.strip()[:400])
                self.event("waiting", tool=name, args=args, summary=request.summary, reason=request.reason)
                decision, note = self.person.approve(request)
                self.check_stop()
                self.event("approval", tool=name, decision=decision, note=note)
                if decision == "allow_run":
                    self.allowed.add(key)
                elif decision != "allow":
                    return (f"The person declined: {note or 'no reason given'}. Do not try this "
                            "again; carry on another way, or answer with what you have.")
        t0 = time.monotonic()
        try:
            result = tool.run(args, self)
        except ToolError as e:
            result = str(e)
        except Stopped:
            raise
        except Exception as e:  # noqa: BLE001 -- a broken tool is reported to the model, not the end of the run
            result = f"The tool {name} failed: {e}"
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + "\n[... cut: too long to show]"
        self.used.append(name)
        self.event("tool", tool=name, args=args, result=result[:4000], seconds=round(time.monotonic() - t0, 1))
        return result

    def needs_approval(self, tool: Tool) -> bool:
        return tool.risk == ASK or (tool.risk == WORKSPACE and self.spec.confirm == "all")

    def delegate(self, agent_id: str, task: str) -> Answer:
        """Run another agent on a task, in a fresh context, and return its
        answer. Called by the delegate tool; see agent_tools/delegate.py."""
        if self.depth > 0:
            raise ToolError("A helper agent cannot hand work on to another.")
        if self.setting.find_agent is None:
            raise ToolError("No other agents are available here.")
        try:
            child = self.setting.find_agent(agent_id)
        except (KeyError, FileNotFoundError):
            raise ToolError(f"There is no agent called {agent_id}.") from None
        if child.problems:
            raise ToolError(f"The agent {agent_id} cannot run: {child.problems[0]}")
        from museion.agent_tools import RISKS
        ceiling = RISKS.index(self.spec.tools_risk())
        known = all_tools()
        child.tools = [t for t in child.tools if t != "delegate" and t in known
                       and RISKS.index(known[t].risk) <= ceiling]
        helper = Harness(child, self.setting, self.setting.workspace_for(child), depth=self.depth + 1)
        self.event("delegate", agent=agent_id, task=task)
        return helper.run(task)

    # ── around the loop ──────────────────────────────────────────────────────

    def system_prompt(self) -> str:
        prompt = self.spec.prompt
        notes = self.workspace / "notes.md" if self.workspace else None
        if notes and "notes" in self.tools and notes.is_file():
            text = notes.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                if len(text) > NOTES_CHARS:
                    text = "[...]\n" + text[-NOTES_CHARS:]
                prompt += f"\n\nYour notes from earlier runs (notes.md):\n{text}"
        return prompt

    def room(self) -> int | None:
        """How many characters of conversation still leave room for one more
        tool result and the answer, or None if the server does not say how
        much context it has."""
        size = getattr(self.chat, "context_size", lambda: None)()
        if size is None:
            return None
        if "search_library" in self.tools and self.library is not None:
            from museion.ask import CONTEXT_CHARS
            following = self.library.tuning.keep * (CONTEXT_CHARS + 200)
        else:
            following = NEXT_RESULT_CHARS
        return int((size - self.spec.answer_tokens - RESERVED_TOKENS) * CHARS_PER_TOKEN) - following

    def stoppable(self, on_token):
        def wrapped(text):
            self.check_stop()
            on_token(text)
        return wrapped

    def check_stop(self) -> None:
        if self.setting.stop is not None and self.setting.stop.is_set():
            raise Stopped()

    def event(self, kind: str, **data) -> None:
        data = {"agent": self.spec.id, "depth": self.depth, **data}
        if self.log is not None:
            self.log.write(kind, data)
        self.setting.on_step(kind, data)

    def mark(self, what: str) -> None:
        """MUSEION_TRACE=1 prints where the seconds go."""
        if "MUSEION_TRACE" in os.environ:
            print(f"  [{time.monotonic() - self.started:6.1f}s] {what}", file=sys.stderr)

    def model_name(self) -> str:
        try:
            return self.chat.model_id()
        except Exception:  # noqa: BLE001 -- a name for the log only
            return "unknown"


def parse_args(tool: Tool, raw: str) -> dict:
    """A tool call's arguments. A model that wrote them as bare text instead
    of JSON, to a tool that takes one string, gets that text used whole."""
    try:
        args = json.loads(raw) if raw.strip() else {}
    except ValueError:
        args = None
    strings = [n for n, s in tool.parameters.items() if s["type"] == "string"]
    if len(tool.required) == 1 and tool.required[0] in strings:
        only = tool.required[0]
        if not isinstance(args, dict):
            return {only: raw}
        if not isinstance(args.get(only), str):
            return {**args, only: raw}
    return args if isinstance(args, dict) else {}
