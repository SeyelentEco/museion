"""The benchmark: run the question set, score each answer by machine, keep the
run so models and configurations can be compared later.

Scoring leans on what the system already reports rather than on judging
prose: the basis label, the cited sources, the calculator's log. A model that
writes a beautiful paragraph with the wrong bleach dose fails the same way a
terse one does.
"""

from __future__ import annotations

import dataclasses
import json
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from museion.answer import Calculation

# The benchmark shipped with Museion.
BENCHMARK_PATH = Path(__file__).resolve().parent.parent / "evals" / "benchmark.toml"


@dataclass
class NumberCheck:
    value: float
    tolerance: float = 0.0


@dataclass
class Case:
    id: str
    capability: str
    horizon: str
    prompt: str
    expect_any: list[str] = field(default_factory=list)
    expect_all: list[str] = field(default_factory=list)
    expect_basis: str | None = None  # alternatives separated by |
    expect_number: NumberCheck | None = None
    min_sources: int | None = None
    min_steps: int | None = None
    source_from: str | None = None
    must_not_contain: list[str] = field(default_factory=list)
    # For agents (agents/<id>/cases.toml):
    expect_tool: list[str] = field(default_factory=list)  # each of these tools ran
    max_rounds_used: int | None = None  # answered in at most this many model turns
    expect_file: str | None = None  # the agent wrote this file in its folder
    given_files: dict = field(default_factory=dict)  # {name: text} in its folder before it starts
    person_says: list[str] = field(default_factory=list)  # answers to its questions, in order
    approve: str = "deny"  # what the person presses on an Allow / Deny card: allow or deny


def load_cases(text: str, agent: bool = False) -> list[Case]:
    """The cases in a benchmark file's text. A field Museion does not know is
    an error rather than silently ignored, so a misspelt check cannot quietly
    pass every answer. An agent's cases need no capability or horizon."""
    known = {f.name for f in dataclasses.fields(Case)}
    cases = []
    for table in tomllib.loads(text).get("cases", []):
        if agent:
            table = {"capability": "agent", "horizon": "", **table}
        unknown = set(table) - known
        if unknown:
            raise ValueError(f"case {table.get('id', '?')}: unknown field(s) {', '.join(sorted(unknown))}")
        case = Case(**table)
        if isinstance(case.expect_number, dict):
            case.expect_number = NumberCheck(**case.expect_number)
        cases.append(case)
    return cases


def builtin_cases() -> list[Case]:
    return load_cases(BENCHMARK_PATH.read_text(encoding="utf-8"))


@dataclass
class CheckResult:
    """One check's verdict, kept so a person can see *why* a case failed."""

    check: str
    passed: bool
    detail: str


@dataclass
class CaseResult:
    id: str
    capability: str
    horizon: str
    prompt: str
    passed: bool
    checks: list[CheckResult]
    answer: str
    basis: str
    sources: list[str]
    calculations: list[str]
    seconds: float
    error: str | None = None
    # The tools it called, in order, and how many model turns it took: what
    # a failed case did is half of why it failed. Older runs have neither.
    tools: list[str] = field(default_factory=list)
    rounds: int = 0
    log: str = ""  # an agent case's run log: every tool call, its arguments and result


@dataclass
class Run:
    id: str
    started: str
    model: str
    reranker: bool
    label: str
    cases: list[CaseResult]
    passed: int
    total: int
    by_capability: list[tuple[str, int, int]]  # (name, passed, total)
    by_horizon: list[tuple[str, int, int]]
    seconds: float


def run(asker, cases: list[Case], label: str,
        report: Callable[[int, int, str, bool | None], None] = lambda *a: None) -> Run:
    """Run the benchmark. `label` is a free name for the configuration under
    test. `report(index, total, case_id, passed)` is called as each case
    starts, with passed None, and again when it is scored."""
    started = time.monotonic()
    try:
        model = asker.chat.model_id()
    except ConnectionError:
        model = "unknown"
    # Seconds since the epoch: stable, sortable, and enough.
    stamp = str(int(time.time()))
    results = []

    for i, case in enumerate(cases, start=1):
        report(i, len(cases), case.id, None)
        t0 = time.monotonic()
        try:
            answer = asker.ask_case(case) if hasattr(asker, "ask_case") else asker.ask(case.prompt)
        except Exception as e:  # noqa: BLE001 -- one failing case must not end the run
            result = CaseResult(
                id=case.id, capability=case.capability, horizon=case.horizon, prompt=case.prompt,
                passed=False, checks=[CheckResult("answered", False, str(e))], answer="", basis="",
                sources=[], calculations=[], seconds=time.monotonic() - t0, error=str(e),
            )
        else:
            sources = [
                f"{h.doc_title} (p.{h.page}) [{h.book}]" if h.page > 0 else f"{h.doc_title} [{h.book}]"
                for h in answer.sources
            ]
            checks = score(case, answer.text, answer.basis, sources, answer.calculations,
                           tools=answer.tools, rounds=answer.rounds, files=answer.files)
            result = CaseResult(
                id=case.id, capability=case.capability, horizon=case.horizon, prompt=case.prompt,
                passed=all(c.passed for c in checks), checks=checks, answer=answer.text,
                basis=answer.basis, sources=sources,
                calculations=[f"{c.expression} = {c.result}" for c in answer.calculations],
                seconds=time.monotonic() - t0, tools=list(answer.tools), rounds=answer.rounds,
                log=answer.log,
            )
        report(i, len(cases), case.id, result.passed)
        results.append(result)

    return Run(
        id=stamp, started=stamp, model=model, reranker=asker.reranker is not None, label=label,
        cases=results, passed=sum(r.passed for r in results), total=len(results),
        by_capability=tally(results, "capability"), by_horizon=tally(results, "horizon"),
        seconds=time.monotonic() - started,
    )


def tally(results: list[CaseResult], key: str) -> list[tuple[str, int, int]]:
    """(name, passed, total) per value of `key`, in order of first
    appearance."""
    counts: dict[str, list[int]] = {}
    for r in results:
        passed_total = counts.setdefault(getattr(r, key), [0, 0])
        passed_total[0] += r.passed
        passed_total[1] += 1
    return [(name, p, t) for name, (p, t) in counts.items()]


def score(case: Case, answer: str, basis: str, sources: list[str], calcs: list[Calculation],
          tools: list[str] | None = None, rounds: int = 0, files: list[str] | None = None) -> list[CheckResult]:
    """Apply every check a case declares."""
    hay = f"{answer}\n{chr(10).join(sources)}".lower()
    out = []

    if case.expect_any:
        found = [t for t in case.expect_any if t.lower() in hay]
        out.append(CheckResult("expect_any", bool(found),
                               f"found {found}" if found else f"none of {case.expect_any} found"))
    if case.expect_all:
        missing = [t for t in case.expect_all if t.lower() not in hay]
        out.append(CheckResult("expect_all", not missing, f"missing {missing}" if missing else "all present"))
    if case.expect_basis is not None:
        wanted = [alt.strip() for alt in case.expect_basis.split("|")]
        out.append(CheckResult("expect_basis", basis in wanted, f"wanted {case.expect_basis}, got {basis}"))
    if case.expect_number is not None:
        n = case.expect_number
        # Only the calculator's results count, as the benchmark says: a
        # number in the prose passed "26 miles is about 41.8 km" with no
        # calculation at all, which is the failure this check exists to find.
        # A question that is nothing but arithmetic logs its calculation too.
        found = []
        for c in calcs:
            try:
                found.append(float(c.result))
            except ValueError:
                pass
        ok = any(abs(v - n.value) <= n.tolerance + 1e-9 for v in found)
        out.append(CheckResult("expect_number", ok, f"wanted {n.value} ± {n.tolerance}, results {found[:6]}"))
    if case.min_sources is not None:
        out.append(CheckResult("min_sources", len(sources) >= case.min_sources,
                               f"{len(sources)} cited, wanted at least {case.min_sources}"))
    if case.min_steps is not None:
        steps = sum(1 for line in answer.splitlines() if is_step(line))
        out.append(CheckResult("min_steps", steps >= case.min_steps,
                               f"{steps} steps, wanted at least {case.min_steps}"))
    if case.source_from is not None:
        ok = any(case.source_from.lower() in s.lower() for s in sources)
        out.append(CheckResult("source_from", ok,
                               f"{'a' if ok else 'no'} source matches {json.dumps(case.source_from)}"))
    if case.must_not_contain:
        # The answer alone: a source cited at "(p.343)" is not the wrong
        # figure "343" in the answer.
        bad = [t for t in case.must_not_contain if t.lower() in answer.lower()]
        out.append(CheckResult("must_not_contain", not bad, f"found {bad}" if bad else "clean"))
    if case.expect_tool:
        missing = [t for t in case.expect_tool if t not in (tools or [])]
        out.append(CheckResult("expect_tool", not missing,
                               f"never ran {missing}" if missing else f"ran {sorted(set(tools or []))}"))
    if case.max_rounds_used is not None:
        out.append(CheckResult("max_rounds_used", rounds <= case.max_rounds_used,
                               f"{rounds} turns, wanted at most {case.max_rounds_used}"))
    if case.expect_file is not None:
        ok = case.expect_file in (files or [])
        out.append(CheckResult("expect_file", ok, f"{'wrote' if ok else 'did not write'} {case.expect_file}"))
    if not out:
        out.append(CheckResult("answered", bool(answer.strip()), "no checks declared"))
    return out


def is_step(line: str) -> bool:
    """A bulleted or numbered line: "- ", "* ", "• ", or "3. ..."."""
    t = line.lstrip()
    return t.startswith(("- ", "* ", "• ")) or (t[:1].isascii() and t[:1].isdigit() and "." in t)


def save_run(directory: Path, run: Run) -> Path:
    """Write the run as run-<id>.json."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run-{run.id}.json"
    path.write_text(json.dumps(dataclasses.asdict(run), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def list_runs(directory: Path) -> list[Run]:
    """Every saved run in the folder, newest first. Files that will not read
    as a run are skipped."""
    runs = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cases = []
            for c in data.pop("cases"):
                c["checks"] = [CheckResult(**ch) for ch in c["checks"]]
                cases.append(CaseResult(**c))
            data["by_capability"] = [tuple(row) for row in data["by_capability"]]
            data["by_horizon"] = [tuple(row) for row in data["by_horizon"]]
            runs.append(Run(cases=cases, **data))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    runs.sort(key=lambda r: r.id, reverse=True)
    return runs
