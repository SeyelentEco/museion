"""`python -m museion agent ...`: agents from the command line.

  agent list                       every agent, and whether it can run here
  agent check [<id>]               load every agent and tool; print the settings
                                   that apply on this computer; exit 1 on a problem
  agent run <id> <task...>         run an agent once, asking at the terminal
  agent test <id> [--only <case>]  run its cases, save the run, show the result
  agent new <id> --tools a,b       write a new agent's folder from the template
  agent new-tool <name>            write a new tool and its test from the template

Run `agent check` after changing anything under agents/ or agent_tools/.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from contextlib import closing
from pathlib import Path

from museion import agents, appstate, hardware
from museion.agent_run import Agents, count_cases, prepared, tests_dir
from museion.runs import WORK
from museion.agent_run import test as run_tests
from museion.harness import Approval, Person
from museion.llama import Llama

ROOT = Path(__file__).resolve().parent.parent


def add_parser(commands, where, servers) -> None:
    top = commands.add_parser("agent", help="list, check, run, test and make agents (see docs/agents.md)")
    sub = top.add_subparsers(dest="agent_command", metavar="<what>", required=True)
    here = argparse.ArgumentParser(add_help=False)
    here.add_argument("--tier", choices=hardware.TIERS, help="resolve for this tier instead of this computer's")
    here.add_argument("--model", help="resolve for this chat model id instead of the running one")

    c = sub.add_parser("list", parents=[here], help="every agent, and whether it can run here")
    c.set_defaults(run=cmd_list)
    c = sub.add_parser("check", parents=[here], help="load every agent and tool, print the settings here")
    c.add_argument("id", nargs="?")
    c.set_defaults(run=cmd_check)
    c = sub.add_parser("run", parents=[where, servers, here], help="run an agent once")
    c.add_argument("id")
    c.add_argument("task", nargs="+")
    c.set_defaults(run=cmd_run)
    c = sub.add_parser("test", parents=[where, servers, here], help="run an agent's cases")
    c.add_argument("id")
    c.add_argument("--only", help="one case id")
    c.add_argument("--label", default="", help="a name for this run")
    c.add_argument("--work", help="a folder that takes the library's place for what agents write (agent-work/, "
                                  "chats/, notes/, test results): to test against a library that must not be "
                                  "written to")
    c.set_defaults(run=cmd_test)
    c = sub.add_parser("new", help="a new agent's folder, from the template")
    c.add_argument("id", help="lower-case words joined by -, like seed-keeper")
    c.add_argument("--name", default="")
    c.add_argument("--goal", default="")
    c.add_argument("--tools", default="search_library,calculate", help="comma-separated tool names")
    c.add_argument("--mine", action="store_true", help="in your config folder instead of the app's agents/")
    c.set_defaults(run=cmd_new)
    c = sub.add_parser("new-tool", help="a new tool and its test, from the template")
    c.add_argument("name", help="lower-case words joined by _, like count_seeds")
    c.add_argument("--risk", choices=["read", "workspace", "ask"], default="read")
    c.set_defaults(run=cmd_new_tool)


def environment(args, library: Path | None = None) -> Agents:
    state = appstate.open_state()
    cfg = state.config()
    tier = getattr(args, "tier", None) or state.hardware().tier
    model = getattr(args, "model", None) or ""
    if not model and getattr(args, "llama", None):
        try:
            model = Llama(args.llama).model_id()
        except Exception:  # noqa: BLE001 -- no model running is fine for listing
            model = ""
    lib = library or (Path(args.library) if getattr(args, "library", None) else cfg.library_dir)
    work = Path(args.work) / WORK if getattr(args, "work", None) else None
    return Agents(lib, agents.override_dir(state.tuning_path), tier, model, work_dir=work)


def cmd_list(args) -> None:
    env = environment(args)
    print(f"\n  This computer: {env.tier}\n")
    for spec in env.all():
        why = spec.problems[0] if spec.problems else agents.tier_problem(spec, env.tier)
        mark = "  " if not why else "x "
        print(f"  {mark}{spec.id:20} {spec.name:18} {spec.min_tier:9} {why or spec.goal}")
    print()


def cmd_check(args) -> None:
    from museion.agent_tools import all_tools
    tools = all_tools()  # raises on a broken tool file
    env = environment(args)
    specs = [env.get(args.id)] if args.id else env.all()
    bad = 0
    print(f"\n  {len(tools)} tools: {', '.join(sorted(tools))}")
    print(f"  Settings for a {env.tier} computer{f' running {env.model_id}' if env.model_id else ''}:\n")
    for spec in specs:
        cases = count_cases(spec)
        where = f"{spec.folder}" + (f" + yours in {spec.override}" if spec.override else "")
        print(f"  {spec.id}  ({where})")
        settings = agents.as_settings(spec)
        for key in ("min_tier", "tools", "guards", "max_rounds", "answer_tokens", "keep_rounds",
                    "compact", "memory", "confirm"):
            print(f"      {key:14} {json.dumps(settings[key])}")
        print(f"      {'prompt':14} {len(spec.prompt)} characters" if spec.prompt_from == "file"
              else f"      {'prompt':14} from tuning.toml")
        print(f"      {'cases':14} {cases}")
        for p in spec.problems:
            print(f"      PROBLEM: {p}")
            bad += 1
        print()
    if bad:
        print(f"  {bad} problem(s).\n")
        sys.exit(1)
    print("  All good.\n")


class TerminalPerson(Person):
    """Approvals and questions at the terminal. Without one (input from a
    file), everything risky is refused."""

    def approve(self, request: Approval) -> tuple[str, str]:
        if not sys.stdin.isatty():
            return "deny", "No one is at the terminal."
        print(f"\n  >> {request.agent} wants to {request.summary}")
        if request.reason:
            print(f"     ({request.reason})")
        answer = input("     Allow? [y = once, a = for this run, n = no, or say why not] ").strip()
        if answer.lower() in ("y", "yes"):
            return "allow", ""
        if answer.lower() == "a":
            return "allow_run", ""
        return "deny", "" if answer.lower() in ("n", "no", "") else answer

    def ask(self, agent: str, question: str) -> str:
        if not sys.stdin.isatty():
            return Person().ask(agent, question)
        return input(f"\n  ?? {question}\n     ") or "(no answer)"


def cmd_run(args) -> None:
    from museion import cli, tuning
    from museion.index import Index
    env = environment(args)
    spec = env.get(args.id)
    with closing(Index(cli.db_path(args))) as index:
        asker = cli.make_asker(args, index)
        if not asker.chat.is_up():
            raise ConnectionError(f"no chat model at {args.llama}")

        def step(kind: str, data: dict) -> None:
            if kind == "tool":
                print(f"\n  [{data['agent']}] {data['tool']}: {str(data.get('args'))[:100]}", file=sys.stderr)
            elif kind in ("guard", "compact", "delegate"):
                print(f"\n  [{data['agent']}] {kind}", file=sys.stderr)

        def show(text):
            if text is not None:
                print(text, end="", flush=True)

        setting = env.setting(asker.chat, asker, TerminalPerson(), on_step=step, tuning=tuning.load(args.tuning))
        answer = env.run(spec, " ".join(args.task), setting, on_token=show, tuning=tuning.load(args.tuning))
    print(f"\n\n  {answer.basis}; tools {answer.tools}; files {answer.files}\n  log: {answer.log}\n")


def cmd_test(args) -> None:
    from museion import cli, tuning
    from museion.index import Index
    env = environment(args)
    spec = env.get(args.id)
    with closing(Index(cli.db_path(args))) as index:
        asker = cli.make_asker(args, index)
        if not asker.chat.is_up():
            raise ConnectionError(f"no chat model at {args.llama}")

        def report(i, total, case_id, passed):
            if passed is None:
                print(f"  [{i}/{total}] {case_id} ...", end="", flush=True)
            else:
                print(" pass" if passed else " FAIL")

        tune = tuning.load(args.tuning)
        run = run_tests(env, prepared(spec, tune), asker.chat, asker, tune, args.only, args.label, report)
    for case in run.cases:
        for check in case.checks:
            if not check.passed:
                print(f"    {case.id}: {check.check}: {check.detail}")
    print(f"\n  {run.passed}/{run.total} passed on {run.model}; saved in {tests_dir(env, spec)}\n")


AGENT_TOML = '''# {name}: {goal}
#
# The settings are explained in docs/agents.md. Check them with
#   python -m museion agent check {id}

name = "{name}"
goal = "{goal}"
min_tier = "Capable"
tools = {tools}
guards = ["calculate_first", "call_again", "cite_or_stand"]
max_rounds = 8
memory = "agent"
examples = []

# More room on bigger computers:
# [tier.Generous]
# max_rounds = 12
'''

PROMPT_MD = '''You are the {name_lower}. {goal}

How to work:
1. (What to find out first.)
2. (What to search for, and what to calculate.)
3. (What the answer should look like.)
'''

CASES_TOML = '''# {name}'s test cases. Each is a task and what the answer must show.
# The checks are those of evals/benchmark.toml plus expect_tool,
# max_rounds_used, expect_file, given_files, person_says and approve.

[[cases]]
id = "first"
prompt = "(A task a person would give this agent.)"
expect_tool = {first_tool}

[[cases]]
id = "second"
prompt = "(Another, different task.)"
expect_basis = "library|library_and_calculation"
'''


def cmd_new(args) -> None:
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", args.id):
        raise ValueError("an agent id is lower-case words joined by -, like seed-keeper")
    from museion.agent_tools import all_tools
    tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    unknown = [t for t in tools if t not in all_tools()]
    if unknown:
        raise ValueError(f"no tool called {', '.join(unknown)}; the tools are {', '.join(sorted(all_tools()))}")
    base = agents.override_dir(appstate.open_state().tuning_path) if args.mine else agents.SHIPPED_DIR
    folder = base / args.id
    if folder.exists():
        raise ValueError(f"{folder} already exists")
    if args.mine and (agents.SHIPPED_DIR / args.id).exists():
        raise ValueError(f"{args.id} is a shipped agent; change it from the Agents tab instead")
    name = args.name or args.id.replace("-", " ").capitalize()
    goal = (args.goal or "(One sentence: what this agent does for the person.)").replace('"', "'")
    folder.mkdir(parents=True)
    (folder / "agent.toml").write_text(AGENT_TOML.format(name=name, goal=goal, id=args.id,
                                       tools=json.dumps(tools)), encoding="utf-8", newline="\n")
    (folder / "prompt.md").write_text(PROMPT_MD.format(name_lower=name.lower(), goal=goal),
                                      encoding="utf-8", newline="\n")
    (folder / "cases.toml").write_text(CASES_TOML.format(name=name, first_tool=json.dumps(tools[:1])),
                                       encoding="utf-8", newline="\n")
    print(f"\n  Wrote {folder}: agent.toml, prompt.md, cases.toml.\n"
          f"  Fill in prompt.md and the cases, then: python -m museion agent check {args.id}\n")


TOOL_PY = '''"""{name}: (one line: what it does for an agent).

(Why an agent needs it, and anything surprising about it.)
"""

from museion.agent_tools import {risk_const}, Tool, ToolError


def run(args: dict, run) -> str:
    # args holds the parameters below. `run` is the harness's Run: run.sources,
    # run.calculations, run.workspace (the agent's folder), run.person.
    # Return the text the model reads. Raise ToolError("...") for a problem
    # the model can fix; it is told the message.
    value = args["value"]
    if not value:
        raise ToolError("value is empty.")
    return f"(the result for {{value}})"


TOOL = Tool(
    name="{name}",
    description="(What the model is told this tool does, in one or two sentences.)",
    parameters={{"value": {{"type": "string", "description": "(what to give it)"}}}},
    required=["value"],
    risk={risk_const},
    run=run,
    describe=lambda a: f"(what this call will do, for the Allow / Deny card) {{a['value']}}",
)
'''

TOOL_TEST = '''"""The {name} tool."""

import unittest

from museion.agent_tools import all_tools
from tests.agent_fakes import FakeRun


class {cls}Test(unittest.TestCase):
    def test_runs(self):
        tool = all_tools()["{name}"]
        run = FakeRun()
        self.assertIn("result", tool.run({{"value": "something"}}, run))


if __name__ == "__main__":
    unittest.main()
'''


def cmd_new_tool(args) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.name):
        raise ValueError("a tool name is lower-case words joined by _, like count_seeds")
    target = ROOT / "museion" / "agent_tools" / f"{args.name}.py"
    test = ROOT / "tests" / f"test_tool_{args.name}.py"
    if target.exists():
        raise ValueError(f"{target} already exists")
    target.write_text(TOOL_PY.format(name=args.name, risk_const=args.risk.upper()), encoding="utf-8", newline="\n")
    cls = "".join(w.capitalize() for w in args.name.split("_"))
    test.write_text(TOOL_TEST.format(name=args.name, cls=cls), encoding="utf-8", newline="\n")
    print(f"\n  Wrote {target.relative_to(ROOT)} and {test.relative_to(ROOT)}.\n"
          "  Fill in the (...) parts, add the tool to an agent's tools in agent.toml, then run\n"
          "  python -m unittest discover -s tests   and   python -m museion agent check\n")
