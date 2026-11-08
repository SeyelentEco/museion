"""What the page asks about chats: listing them, starting one with an agent,
sending a message, answering an agent's question or Allow / Deny card, and
stopping it.

Every chat has one agent; an agent can have any number of chats. A message
runs the chat's agent through the harness while its words stream to the page
as events, each carrying the chat's id:

  agent-token    {chat, text}            a piece of the answer
  agent-reset    {chat}                  take back what streamed (it turned to a tool)
  agent-step     {chat, kind, ...}       a tool call, a guard, a fold, a helper
  agent-waiting  {chat, kind, ...}       an Allow / Deny card or a question
  agent-queued   {chat}                  waiting for another chat to finish

One chat works at a time: there is one GPU and one chat model. A message
sent in a second chat waits, shown as queued.
"""

from __future__ import annotations

import dataclasses
import queue
import threading
from contextlib import closing

from museion import agents, calc, tuning
from museion.agent_run import Agents
from museion.answer import DISCARD, Exchange
from museion.appstate import AppState, UiError, local
from museion.harness import Approval, Person, Stopped
from museion.index import Index
from museion.llama import Llama
from museion.runs import WORK, ChatStore, title_from
from museion.sidecar import CHAT

# The chat model is shared: one run at a time.
GPU = threading.Lock()


class Live:
    """A chat that is working: how to stop it, and the person's replies."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.replies: queue.Queue = queue.Queue()
        self.waiting: dict | None = None


RUNNING: dict[str, Live] = {}
_running_lock = threading.Lock()


def environment(state: AppState) -> Agents:
    """Where agents, their work and chats are, resolved for this computer and
    the chat model that is running."""
    cfg = state.config()
    if not cfg.library_dir:
        raise UiError("No library is set up yet, so there is nowhere to keep chats.")
    work = cfg.library_dir / WORK
    if state.drive:
        work = state.drive.writable_or_cache(work)
    try:
        model = Llama(local(cfg.chat_port), timeout=3).model_id()
    except Exception:  # noqa: BLE001 -- no model running: resolve without its table
        model = ""
    return Agents(cfg.library_dir, agents.override_dir(state.tuning_path), state.hardware().tier,
                  model, work_dir=work)


def store(state: AppState) -> ChatStore:
    return ChatStore(environment(state).work.parent / "chats")


def list_chats(state: AppState, agent: str | None = None) -> dict:
    env = environment(state)
    specs = env.all()
    names = {s.id: s.name for s in specs}
    chats = ChatStore(env.work.parent / "chats").list(agent)
    for c in chats:
        c["agent_name"] = names.get(c["agent"], c["agent"])
        c["working"] = c["id"] in RUNNING
    # Only agents that can run here, and are on, are offered for a new chat.
    return {"chats": chats, "agents": [
        {"id": s.id, "name": s.name,
         "can_start": s.enabled and not s.problems and agents.tier_problem(s, env.tier) is None}
        for s in specs]}


def new_chat(state: AppState, agent: str = "librarian") -> dict:
    env = environment(state)
    try:
        spec = env.get(agent)
    except KeyError:
        raise UiError(f"There is no agent called {agent}.") from None
    why = spec.problems[0] if spec.problems else agents.tier_problem(spec, env.tier)
    if why:
        raise UiError(f"{spec.name} cannot run here: {why}")
    return chat_view(env, ChatStore(env.work.parent / "chats").new(agent))


def get_chat(state: AppState, id: str) -> dict:
    env = environment(state)
    try:
        return chat_view(env, ChatStore(env.work.parent / "chats").get(id))
    except KeyError:
        raise UiError("That chat is gone.") from None


def chat_view(env: Agents, chat) -> dict:
    try:
        spec = env.get(chat.agent)
        agent = {"id": spec.id, "name": spec.name, "goal": spec.goal, "examples": spec.examples,
                 "memory": spec.memory}
    except KeyError:
        agent = {"id": chat.agent, "name": chat.agent, "goal": "(this agent is gone)", "examples": [], "memory": "none"}
    live = RUNNING.get(chat.id)
    return {**dataclasses.asdict(chat), "agent_info": agent, "working": live is not None,
            "waiting": live.waiting if live else None}


def rename_chat(state: AppState, id: str, title: str) -> None:
    s = store(state)
    chat = s.get(id)
    chat.title = " ".join(title.split())[:120]
    s.save(chat)


def delete_chat(state: AppState, id: str) -> None:
    if id in RUNNING:
        raise UiError("Stop the chat before deleting it.")
    store(state).delete(id)


class PagePerson(Person):
    """The person at the page: an Allow / Deny card, or a question, shown in
    the chat, and the run waits for the reply."""

    def __init__(self, state: AppState, chat_id: str, live: Live) -> None:
        self.state, self.chat_id, self.live = state, chat_id, live

    def wait(self, waiting: dict) -> tuple:
        self.live.waiting = waiting
        self.state.emit("agent-waiting", {"chat": self.chat_id, **waiting})
        try:
            while True:
                try:
                    return self.live.replies.get(timeout=1)
                except queue.Empty:
                    if self.live.stop.is_set():
                        raise Stopped() from None
        finally:
            self.live.waiting = None
            self.state.emit("agent-waiting", {"chat": self.chat_id, "kind": None})

    def approve(self, request: Approval) -> tuple[str, str]:
        decision, note, _ = self.wait({"kind": "approve", "agent": request.agent, "tool": request.tool,
                                       "summary": request.summary, "reason": request.reason,
                                       "args": request.args})
        return decision, note

    def ask(self, agent: str, question: str) -> str:
        _, _, answer = self.wait({"kind": "ask", "agent": agent, "question": question})
        return answer or "(no answer)"


def answer_agent(state: AppState, chat: str, decision: str = "", note: str = "", answer: str = "") -> None:
    """The person's reply to a card: decision allow, allow_run or deny (with
    an optional note), or the answer to a question."""
    live = RUNNING.get(chat)
    if live is None or live.waiting is None:
        raise UiError("That chat is not waiting for anything.")
    if live.waiting["kind"] == "approve" and decision not in ("allow", "allow_run", "deny"):
        raise UiError("Allow or Deny?")
    live.replies.put((decision or "deny", note.strip(), answer.strip()))


def stop_run(state: AppState, chat: str) -> None:
    live = RUNNING.get(chat)
    if live:
        live.stop.set()


def history_of(messages: list[dict]) -> list[Exchange]:
    out = []
    for q, a in zip(messages, messages[1:]):
        if q["role"] == "user" and a["role"] == "assistant" and not a.get("stopped"):
            out.append(Exchange(q["text"], a["text"]))
    return out


def send_message(state: AppState, chat: str, text: str) -> dict:
    """Run the chat's agent on the message; returns the answer as saved."""
    text = text.strip()
    if not text:
        raise UiError("Write a message first.")
    env = environment(state)
    chats = ChatStore(env.work.parent / "chats")
    try:
        record = chats.get(chat)
        spec = env.get(record.agent)
    except KeyError:
        raise UiError("That chat or its agent is gone.") from None
    with _running_lock:
        if chat in RUNNING:
            raise UiError("This chat is still working on the last message.")
        live = RUNNING[chat] = Live()
    try:
        history = history_of(record.messages)
        record.messages.append({"role": "user", "text": text})
        if not record.title:
            record.title = title_from(text)
        chats.save(record)
        if not GPU.acquire(blocking=False):
            state.emit("agent-queued", {"chat": chat})
            while not GPU.acquire(timeout=1):
                if live.stop.is_set():
                    raise Stopped()
        try:
            reply = run_agent(state, env, spec, record.id, text, history, live)
        finally:
            GPU.release()
    except Stopped:
        reply = {"role": "assistant", "text": "(Stopped before an answer.)", "stopped": True}
    except Exception:
        record.messages.pop()
        chats.save(record)
        raise
    finally:
        with _running_lock:
            RUNNING.pop(chat, None)
    record = chats.get(chat)
    record.messages.append(reply)
    chats.save(record)
    return reply


def run_agent(state: AppState, env: Agents, spec, chat_id: str, text: str, history, live: Live) -> dict:
    from museion.commands import make_asker
    cfg = state.config()
    db = cfg.index_path()
    chat = Llama(local(cfg.chat_port))
    if not (spec.arithmetic_shortcut and calc.looks_like_arithmetic(text)) and not chat.is_up():
        if state.supervisor.is_running(CHAT):
            raise UiError("The chat model is still loading. Give it a minute and try again.")
        raise UiError("The chat model isn't running. Open Settings and press Start services.")

    def on_token(t: str | None) -> None:
        if t is DISCARD:
            state.emit("agent-reset", {"chat": chat_id})
        else:
            state.emit("agent-token", {"chat": chat_id, "text": t})

    steps: list[dict] = []

    def on_step(kind: str, data: dict) -> None:
        if kind in ("tool", "guard", "compact", "delegate", "approval"):
            step = {"kind": kind, **{k: v for k, v in data.items() if k != "result"}}
            steps.append(step)
            state.emit("agent-step", {"chat": chat_id, **step})

    tune = tuning.load(state.tuning_path)
    with closing(Index(db)) as index:
        asker = make_asker(state, cfg, index, chat)
        setting = env.setting(chat, asker, PagePerson(state, chat_id, live), chat_id, on_step, live.stop, tune)
        answer = env.run(spec, text, setting, history, on_token, chat_id, tune)
    return {
        "role": "assistant", "text": answer.text, "basis": answer.basis,
        "sources": [{"n": n, "title": h.doc_title, "author": h.author, "page": h.page, "book": h.book,
                     "file": h.file, "snippet": h.snippet} for n, h in enumerate(answer.sources, start=1)],
        "calculations": [dataclasses.asdict(c) for c in answer.calculations],
        "tools": answer.tools, "files": answer.files, "log": answer.log, "steps": steps,
    }
