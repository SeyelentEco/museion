"""Where agents keep what they did: run logs, and chats.

Both live in the library, so they travel with a drive:

  <library>/agent-work/<agent>/          the agent's own folder (notes.md, files)
  <library>/agent-work/<agent>/runs/     one log per run, <time>-<id>.jsonl
  <library>/chats/<chat>.json            one file per chat

A run log is JSON Lines: one line per step -- start, each model turn, each
tool call and its result, each approval, each guard, each fold of the
conversation, and done -- so a run can be read in any text editor and opened
in the page. A chat is one JSON file holding its agent, title and messages;
each answer links to the log of the run that made it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

WORK = "agent-work"
CHATS = "chats"


def new_id() -> str:
    """Sortable by time, unique enough for one person's chats."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


class RunLog:
    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.id = new_id()
        self.path = folder / f"{self.id}.jsonl"

    def write(self, kind: str, data: dict) -> None:
        line = json.dumps({"t": round(time.time(), 2), "kind": kind, **data}, ensure_ascii=False, default=str)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # a log that cannot be written must not cost the answer


def read_log(path: Path) -> list[dict]:
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def list_logs(work: Path, agent_id: str, limit: int = 50) -> list[dict]:
    """The agent's latest runs, newest first: id, when, goal, outcome."""
    folder = work / agent_id / "runs"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.jsonl"), reverse=True)[:limit]:
        steps = read_log(path)
        start = next((s for s in steps if s["kind"] == "start"), {})
        end = steps[-1] if steps else {}
        out.append({"id": path.stem, "path": str(path), "goal": start.get("goal", ""),
                    "model": start.get("model", ""), "outcome": end.get("kind", ""),
                    "steps": len(steps)})
    return out


# ── chats ───────────────────────────────────────────────────────────────────

@dataclass
class Chat:
    id: str
    agent: str
    title: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    # {"role": "user" | "assistant", "text": ..., and for answers: "sources",
    #  "basis", "calculations", "log", "tools"}
    messages: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {"id": self.id, "agent": self.agent, "title": self.title or "New chat",
                "updated": self.updated, "messages": len(self.messages)}


class ChatStore:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def path(self, chat_id: str) -> Path:
        if not re.fullmatch(r"[0-9A-Za-z-]+", chat_id or ""):
            raise KeyError(chat_id)
        return self.folder / f"{chat_id}.json"

    def new(self, agent: str, title: str = "") -> Chat:
        chat = Chat(id=new_id(), agent=agent, title=title)
        self.save(chat)
        return chat

    def get(self, chat_id: str) -> Chat:
        try:
            data = json.loads(self.path(chat_id).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise KeyError(chat_id) from e
        return Chat(**data)

    def save(self, chat: Chat) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        chat.updated = time.time()
        path = self.path(chat.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(chat.__dict__, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, chat_id: str) -> None:
        path = self.path(chat_id)
        if path.exists():
            path.unlink()

    def list(self, agent: str | None = None) -> list[dict]:
        """Every chat, newest first, or one agent's."""
        out = []
        if not self.folder.is_dir():
            return out
        for path in self.folder.glob("*.json"):
            try:
                chat = Chat(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if agent is None or chat.agent == agent:
                out.append(chat.summary())
        out.sort(key=lambda c: c["updated"], reverse=True)
        return out


def title_from(text: str) -> str:
    """A chat's title: its first message, cut at a word near 60 characters."""
    t = " ".join(text.split())
    if len(t) <= 60:
        return t
    cut = t[:60].rsplit(" ", 1)[0]
    return cut + "…"
