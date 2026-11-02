"""The tools agents can use, one file each in this folder.

A tool file defines TOOL = Tool(...) and the function it runs. The registry
below finds every file here, so a new tool is a new file (start from
`python -m museion agent new-tool <name>`) plus a test in
tests/test_agent_tools.py. An agent only gets the tools its agent.toml lists.

Every tool declares how risky it is, and the harness -- not the model --
decides from that whether to ask the person first:

  read       reads the library or computes; runs without asking
  workspace  writes only inside the agent's own folder; runs without asking
             (unless the agent says confirm = "all"), and is logged
  ask        changes something outside the agent's folder; the person
             must press Allow every time

There are no tools for the shell, the network, or deleting, and there must
not be: the registry refuses a tool with any other risk.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field

READ = "read"
WORKSPACE = "workspace"
ASK = "ask"
RISKS = (READ, WORKSPACE, ASK)


class ToolError(Exception):
    """A problem the model can fix: said back to it as the tool's result."""


@dataclass
class Tool:
    name: str
    description: str  # what the model is told the tool does
    # JSON Schema properties, {"query": {"type": "string", "description": ...}}.
    parameters: dict
    risk: str
    # run(args, run) -> text for the model. `run` is the harness's Run: its
    # sources, calculations, workspace, library and the person (see harness.py).
    run: Callable[[dict, object], str]
    required: list[str] = field(default_factory=list)
    # One line for the Allow / Deny card: what this call will do, in words a
    # person reads. Defaults to the tool's name and arguments.
    describe: Callable[[dict], str] | None = None

    def schema(self) -> dict:
        """The tool as the chat model is offered it."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": self.parameters,
                               "required": list(self.required)},
            },
        }

    def summary(self, args: dict) -> str:
        if self.describe:
            try:
                return self.describe(args)
            except (KeyError, TypeError, ValueError):
                pass
        shown = ", ".join(f"{k}={str(v)[:80]!r}" for k, v in args.items())
        return f"{self.name}({shown})"


_registry: dict[str, Tool] | None = None


def all_tools() -> dict[str, Tool]:
    """Every tool in this folder, by name. A file that fails to import, or
    whose tool is malformed, raises: a broken tool must be found by
    `agent check` and the tests, not discovered half-way through a run."""
    global _registry
    if _registry is None:
        found: dict[str, Tool] = {}
        for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
            if info.name.startswith("_"):
                continue
            module = importlib.import_module(f"{__name__}.{info.name}")
            tool = getattr(module, "TOOL", None)
            if not isinstance(tool, Tool):
                raise RuntimeError(f"agent_tools/{info.name}.py defines no TOOL = Tool(...)")
            problem = check_tool(tool)
            if problem:
                raise RuntimeError(f"agent_tools/{info.name}.py: {problem}")
            if tool.name in found:
                raise RuntimeError(f"two tools are named {tool.name}")
            found[tool.name] = tool
        _registry = found
    return _registry


def check_tool(tool: Tool) -> str | None:
    if tool.risk not in RISKS:
        return f"risk must be one of {', '.join(RISKS)}, not {tool.risk!r}"
    if not tool.name.replace("_", "").isalnum():
        return f"tool name {tool.name!r} must be letters, digits and _"
    for name in tool.required:
        if name not in tool.parameters:
            return f"required parameter {name} is not among the parameters"
    for name, spec in tool.parameters.items():
        if not isinstance(spec, dict) or spec.get("type") not in ("string", "number", "integer", "boolean"):
            return f"parameter {name} needs a type of string, number, integer or boolean"
    return None


def check_args(tool: Tool, args: dict) -> str | None:
    """A sentence for the model if the arguments do not fit the tool, else
    None. Kept small on purpose: types, required, and nothing unknown."""
    if not isinstance(args, dict):
        return f"The arguments to {tool.name} must be a JSON object."
    missing = [n for n in tool.required if n not in args or args[n] in (None, "")]
    if missing:
        return f"{tool.name} needs {', '.join(missing)}."
    for name, value in args.items():
        spec = tool.parameters.get(name)
        if spec is None:
            return f"{tool.name} has no parameter {name}; it takes {', '.join(tool.parameters) or 'none'}."
        kind = spec["type"]
        ok = (isinstance(value, str) if kind == "string"
              else isinstance(value, bool) if kind == "boolean"
              else isinstance(value, int) and not isinstance(value, bool) if kind == "integer"
              else isinstance(value, (int, float)) and not isinstance(value, bool))
        if not ok:
            return f"{tool.name}: {name} must be a {kind}."
        if kind == "string" and len(value) > 20000:
            return f"{tool.name}: {name} is too long."
    return None
