"""Stand-ins for testing tools and the harness without a model."""

import copy
import tempfile
from pathlib import Path

from museion import llama
from museion.harness import Person


class FakeRun:
    """What a tool sees as `run`, with a scratch folder as the workspace."""

    def __init__(self, workspace: Path | None = None, sources=None, library=None, person=None):
        self.workspace = workspace if workspace is not None else Path(tempfile.mkdtemp(prefix="museion-tool-"))
        self.sources = list(sources or [])
        self.calculations = []
        self.files = []
        self.reranked = False
        self.library = library
        self.person = person or Person()
        self.spec = type("Spec", (), {"id": "test-agent"})()
        self.setting = type("Setting", (), {"library_dir": None})()


class ScriptedChat:
    """Plays back one llama.Turn per call to turn()."""

    def __init__(self, *turns, context=None):
        self.turns = list(turns)
        self.calls = []
        self.context = context

    def turn(self, messages, tools=None, max_tokens=1024, on_token=None):
        self.calls.append((copy.deepcopy(messages), tools))
        turn = self.turns.pop(0)
        if turn.text and on_token:
            on_token(turn.text)
        return turn

    def chat(self, messages, max_tokens=1024):
        return "subject"

    def context_size(self):
        return self.context

    def model_id(self):
        return "fake-model.gguf"


def says(text: str, limited: bool = False) -> llama.Turn:
    t = llama.Turn()
    t.text = text
    t.hit_token_limit = limited
    return t


def calls(*pairs) -> llama.Turn:
    """A turn calling tools: calls(("calculate", {"expression": "1+1"}), ...)."""
    import json
    t = llama.Turn()
    t.tool_calls = [{"id": f"c{i}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}
                    for i, (name, args) in enumerate(pairs)]
    return t
