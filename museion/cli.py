"""museion -- the offline library, from the command line.

With no command, Museion opens in the browser (app.py). The commands are the
same code the window uses, exposed so retrieval can be measured and
regression-tested without a window in the way.

  python -m museion
  python -m museion fetch  go-bag --dest E:\\museion-library
  python -m museion index  --library E:\\museion-library
  python -m museion search --library E:\\museion-library how do I purify water
  python -m museion status --library E:\\museion-library
  python -m museion prepare --dest E:\\ --bundle go-bag
  python -m museion edit
  python -m museion agent list

Run `python -m museion <command> --help` for each command's options.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from museion import (agent_cli, app, appstate, ask, calc, catalog_cli, climate_cli, drive_cli, evals, fit, hardware,
                     indexer, map_cli, model_choice, retrieval_eval, tools, tuning)
from museion.appstate import UiError
from museion.index import Index
from museion.kiwix import Kiwix, book_id_from_filename
from museion.llama import Llama
from museion.model_text import cited_numbers

KIWIX_URL = "http://127.0.0.1:8090"
CHAT_URL = "http://127.0.0.1:8080"
RERANKER_URL = "http://127.0.0.1:8081"

# What a failed command can raise and still be the person's problem rather
# than Museion's: a missing file, a server that is not running, bad input.
# These are printed as one line. Anything else is a bug, and its traceback is
# shown so it can be found.
EXPECTED_ERRORS = (OSError, RuntimeError, ValueError, sqlite3.Error, UiError)

BASIS_LABELS = {
    ask.LIBRARY: "from the library",
    ask.LIBRARY_AND_CALCULATION: "from the library, with calculation",
    ask.COMPUTED: "computed",
    "own_files": "from the files it checked",
    "notes": "from your own notes, not checked against the library",
    ask.GENERAL_KNOWLEDGE: "GENERAL KNOWLEDGE -- not from the library, unverified",
}


def main(argv: list[str] | None = None) -> None:
    # Answers are full of characters like × and ±. A console shows them, but
    # output redirected to a file on Windows would be written in the old
    # code page and stop at the first one; write UTF-8 there, as Rust did.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    argv = sys.argv[1:] if argv is None else argv
    # No command, or only the window's options (--port 8075): open the
    # window, which is what a person double-clicking Museion wants.
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["app", *argv]
    args = parser().parse_args(argv)
    try:
        args.run(args)
    except EXPECTED_ERRORS as e:
        print(f"\nerror: {e}\n", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="museion", description="Offline library: the window, index and search.")
    commands = top.add_subparsers(dest="command", metavar="<command>")

    c = commands.add_parser("app", help="open Museion in the browser (what no command does)")
    app.add_arguments(c)
    c.set_defaults(run=lambda a: app.serve(a.port, not a.no_browser, a.stay, a.config_dir))

    # Where the index is, shared by every command that reads it.
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument("--db", help="index file (default: <library>/museion-index.db)")
    where.add_argument("--library", help="folder holding the .zim archives")

    # The servers an answer needs, shared by gather, ask, chat and eval.
    servers = argparse.ArgumentParser(add_help=False)
    servers.add_argument("--llama", default=CHAT_URL, help=f"chat model server (default {CHAT_URL})")
    servers.add_argument("--reranker", default=RERANKER_URL, help=f"reranker server (default {RERANKER_URL})")
    servers.add_argument("--no-rerank", action="store_true", help="skip reranking, use keyword ranking alone")
    servers.add_argument("--kiwix", default=KIWIX_URL, help=f"kiwix-serve (default {KIWIX_URL})")
    servers.add_argument("--tuning", type=Path, help="prompts and limits override (see museion/tuning.toml)")

    c = commands.add_parser("index", parents=[where], help="build the index over the library")
    c.add_argument("--extra", action="append", default=[], metavar="DIR",
                   help="a further folder of .zim archives or PDFs (may be repeated)")
    c.add_argument("--kiwix", default=KIWIX_URL, help=f"kiwix-serve serving the library (default {KIWIX_URL})")
    c.add_argument("--pdfium", default="bin", help="folder holding the pdfium library (default ./bin)")
    c.add_argument("--force", action="store_true", help="re-index archives that are already present")
    c.set_defaults(run=cmd_index)

    c = commands.add_parser("search", parents=[where], help="keyword search of the index")
    c.add_argument("query", nargs="+")
    c.add_argument("--limit", type=int, default=10, help="number of results (default 10)")
    c.set_defaults(run=cmd_search)

    c = commands.add_parser("gather", parents=[where, servers],
                            help="the passages an answer would be built on, without the chat model")
    c.add_argument("question", nargs="+")
    c.set_defaults(run=cmd_gather)

    c = commands.add_parser("ask", parents=[where, servers], help="ask one question")
    c.add_argument("question", nargs="+")
    c.set_defaults(run=cmd_ask)

    c = commands.add_parser("chat", parents=[where, servers], help="a conversation at the terminal")
    c.set_defaults(run=cmd_chat)

    c = commands.add_parser("eval", parents=[where, servers], help="run the benchmark and save the run")
    c.add_argument("--label", default="cli", help="a name for the configuration under test")
    c.add_argument("--only", help="run only this capability, horizon or case id")
    c.add_argument("--results", help="folder to save the run in (default: evals/ beside the index)")
    c.set_defaults(run=cmd_eval)

    c = commands.add_parser("retrieval-eval", parents=[where, servers],
                            help="check retrieval against evals/retrieval.toml, without the chat model")
    c.add_argument("--mode", choices=["search", "gather"],
                   help="search (keyword only) or gather (with the reranker); default gather if the reranker answers")
    c.add_argument("--limit", type=int, default=8, help="passages searched per question (default 8)")
    c.add_argument("--verbose", action="store_true", help="show the top passages for every case")
    c.set_defaults(run=cmd_retrieval_eval)

    agent_cli.add_parser(commands, where, servers)

    c = commands.add_parser("status", parents=[where], help="what the index holds")
    c.set_defaults(run=cmd_status)

    c = commands.add_parser("hardware", help="the accelerator, memory and tier Museion sees")
    c.set_defaults(run=cmd_hardware)

    c = commands.add_parser("fit", help="the GPU offload split for a model on this machine")
    c.add_argument("--model", required=True, type=Path, help="the model's .gguf file")
    c.add_argument("--fit-exe", type=Path, default=Path("bin", "llama-fit-params.exe" if sys.platform == "win32"
                                                         else "llama-fit-params"),
                   help="the llama-fit-params program (default bin/llama-fit-params)")
    c.add_argument("--ctx", type=int, default=32768, help="context length to fit (default 32768)")
    c.set_defaults(run=cmd_fit)

    c = commands.add_parser("tools", help="fetch and unpack the helper programs for this machine")
    c.add_argument("--dest", required=True, type=Path, help="folder to put them in")
    c.add_argument("--cpu", action="store_true", help="the CPU build, whatever the GPU")
    c.set_defaults(run=cmd_tools)

    catalog_cli.add_commands(commands)
    drive_cli.add_commands(commands)
    map_cli.add_commands(commands)
    climate_cli.add_commands(commands)
    return top


def db_path(args) -> Path:
    if args.db:
        return Path(args.db)
    if args.library:
        return Path(args.library) / "museion-index.db"
    raise ValueError("need --db (or --library, so the default location can be derived)")


def cmd_index(args) -> None:
    if not args.library:
        raise ValueError("--library is required")
    db = db_path(args)
    print(f"\nIndex: {db}\nKiwix: {args.kiwix}\n")

    def show(p) -> None:
        if isinstance(p, indexer.Skipped):
            print(f"  skip     {p.book}  (already indexed; --force to rebuild)")
        elif isinstance(p, indexer.Native):
            print(f"  native   {p.book}  (its own full-text index works; nothing to build)")
        elif isinstance(p, indexer.Archive):
            print(f"  building {p.book}  ({p.documents} documents)")
        elif isinstance(p, indexer.Document):
            if p.outcome == indexer.IMAGE_ONLY:
                print(f"      scan   {p.title}  (no text layer)")
            elif p.outcome == indexer.DUPLICATE:
                print(f"      dup    {p.title}")
            elif p.outcome == indexer.FAILED:
                print(f"      FAIL   {p.title}", file=sys.stderr)
        elif isinstance(p, indexer.ArchiveDone):
            print(f"           {p.indexed} indexed, {p.duplicate} duplicate, "
                  f"{p.image_only} image-only, {p.failed} failed")

    s = indexer.index_library(args.library, args.extra, db, args.kiwix, args.pdfium, args.force, show)
    print(f"\n{s.documents} documents, {s.pages} pages, {s.words:,} words indexed "
          f"({s.image_only} image-only skipped)\n")


def cmd_search(args) -> None:
    query = " ".join(args.query)
    if not query.strip():
        raise ValueError("nothing to search for")
    with closing(Index(db_path(args))) as index:
        hits = index.search(query, args.limit)
    if not hits:
        print("\nNothing in the library matches that.\n")
        return
    print()
    for i, h in enumerate(hits, start=1):
        who = f"  -- {h.author}" if h.author else ""
        print(f"{i:>2}. {h.doc_title}{who}")
        print(f"    page {h.page} of {h.book}")
        print(f"    {truncate_to(h.snippet, 150)}")
        print()


def cmd_gather(args) -> None:
    """Retrieval plus reranking, without generating an answer.

    This is the stage worth regression-testing: it is where a question either
    finds the right passages or does not, and it runs without loading a chat
    model, so the evaluation set is cheap enough to run on every change."""
    question = " ".join(args.question)
    if not question.strip():
        raise ValueError("gather what?")
    # A chat model is not needed to gather; the asker is given one that is
    # never called on this path.
    with closing(Index(db_path(args))) as index:
        hits, reranked = make_asker(args, index, quiet=True).gather(question)
    if not hits:
        print("\nNothing in the library covers that.\n")
        return
    print(f"\n{len(hits)} passage(s){', reranked' if reranked else ', keyword ranking only'}\n")
    for i, h in enumerate(hits, start=1):
        where = f"page {h.page}" if h.page > 0 else h.book
        print(f"{i:>2}. {h.doc_title} -- {where}")
        print(f"    {truncate_to(h.body, 220)}")
        print()


def cmd_ask(args) -> None:
    question = " ".join(args.question)
    if not question.strip():
        raise ValueError("ask what?")
    answer_one(args, question, [])


def cmd_chat(args) -> None:
    """A conversation at the terminal: each question is asked with the ones
    before it, so follow-ups can be tried without the window. An empty line,
    "quit" or end of input ends it."""
    history: list[ask.Exchange] = []
    print("Hypatia. Ask, and ask again; an empty line ends it.\n")
    while True:
        try:
            question = input("> ").strip()
        except EOFError:
            break
        if not question or question.lower() in ("quit", "exit"):
            break
        try:
            answer = answer_one(args, question, history)
        except EXPECTED_ERRORS as e:
            print(f"\nerror: {e}\n", file=sys.stderr)
            continue
        history.append(ask.Exchange(question, answer.text))


def answer_one(args, question: str, history: list[ask.Exchange]) -> ask.Answer:
    with closing(Index(db_path(args))) as index:
        return answer_from(args, index, question, history)


def answer_from(args, index: Index, question: str, history: list[ask.Exchange]) -> ask.Answer:
    if index.stats().documents == 0:
        raise RuntimeError("the index is empty, so there is nothing to answer from.\n\n"
                           "  Build it first:  python -m museion index --library <dir>")
    asker = make_asker(args, index)
    if not calc.looks_like_arithmetic(question) and not asker.chat.is_up():
        raise ConnectionError(f"no chat model at {args.llama}\n\n"
                              "  Start llama-server with a chat model, then try again.")
    print()
    streamed = False
    shown = ""

    def show(text: str | None) -> None:
        nonlocal streamed, shown
        if text is ask.DISCARD:
            # A terminal cannot unprint; mark where the model started over.
            if streamed:
                print("\n  -- consulted a tool; the answer restarts below --\n")
                streamed = False
            shown = ""
        else:
            streamed = True
            shown += text
            print(text, end="", flush=True)

    from museion.harness import Setting
    notes_dir = Path(args.library) / "notes" if getattr(args, "library", None) else None
    answer = asker.ask(question, history, show, Setting(chat=asker.chat, notes_dir=notes_dir))
    # Nothing streams when retrieval found nothing, and that answer -- the
    # library saying it does not cover this -- is the one that must not be
    # swallowed.
    if not streamed:
        print(answer.text, end="")
    print("\n")
    print(f"Basis: {BASIS_LABELS[answer.basis]}")
    if answer.calculations:
        print("Calculations performed (ground truth):")
        for c in answer.calculations:
            print(f"  {c.expression} = {c.result}")
    if answer.sources:
        print(f"Sources{' (reranked)' if answer.reranked else ''}:")
        # The answer as streamed carries the model's own numbers ([5]), not
        # the renumbered ones in answer.text; the list must use the numbers
        # the reader saw, or [5] above names nothing below.
        cited = cited_numbers(shown) if streamed else []
        numbers = cited if len(cited) == len(answer.sources) else range(1, len(answer.sources) + 1)
        for i, h in zip(numbers, answer.sources):
            who = f", {h.author}" if h.author else ""
            where = f"page {h.page}" if h.page > 0 else h.book
            print(f"  [{i}] {h.doc_title}{who} -- {where}")
        print()
    return answer


def cmd_eval(args) -> None:
    """Run the benchmark against the running servers and save the run.

      python -m museion eval --db <file> --label "qwen3.6-35b q4, reranker"
      python -m museion eval --db <file> --only calculate"""
    db = db_path(args)
    with closing(Index(db)) as index:
        run = run_benchmark(args, index)
    path = evals.save_run(Path(args.results) if args.results else db.parent / "evals", run)
    print(f"\nSaved {path}\n")


def cmd_retrieval_eval(args) -> None:
    """Score what search (or gather) prints for each case in
    evals/retrieval.toml. Exits 1 if any case fails."""
    cases = retrieval_eval.cases()
    mode = args.mode or ("gather" if not args.no_rerank and Llama(args.reranker).is_up() else "search")
    db = db_path(args)
    print(f"\nRetrieval check: {len(cases)} cases against {db}")
    print("Mode: gather (retrieval and reranking)\n" if mode == "gather"
          else "Mode: search (keyword retrieval only; no reranker)\n")

    def output(question: str, limit: int) -> str:
        buffer = io.StringIO()
        words = question.split()
        run_args = argparse.Namespace(**{**vars(args), "query": words, "question": words, "limit": limit})
        with contextlib.redirect_stdout(buffer):
            try:
                (cmd_gather if mode == "gather" else cmd_search)(run_args)
            except EXPECTED_ERRORS as e:
                print(f"ERROR: {e}")
        return buffer.getvalue()

    failed = []
    for case in cases:
        if case.needs_rerank and mode != "gather":
            print(f"  SKIP  {case.question}\n        needs a reranker; start one to run this case\n")
            continue
        ok, detail = retrieval_eval.score(case, output(case.question, args.limit))
        print(f"  {'PASS' if ok else 'FAIL'}  {case.question}\n        {detail}")
        if not ok or args.verbose:
            print(f"        ({' '.join(case.why.split())})")
        if args.verbose:
            print("".join(f"        {line}\n" for line in output(case.question, 3).strip().splitlines()))
        print()
        if not ok:
            failed.append(case.question)
    print(f"{len(cases) - len(failed)}/{len(cases)} passed (skipped cases count as passed)\n")
    if failed:
        print("Failed:\n" + "".join(f"  {q}\n" for q in failed))
        sys.exit(1)


def run_benchmark(args, index: Index) -> evals.Run:
    asker = make_asker(args, index)
    if not asker.chat.is_up():
        raise ConnectionError(f"no chat model at {args.llama}")
    cases = evals.builtin_cases()
    if args.only:
        cases = [c for c in cases if args.only in (c.capability, c.horizon, c.id)]
        if not cases:
            raise ValueError(f"--only {args.only!r} matches no case, capability or horizon")

    print(f"\nRunning {len(cases)} case(s) against {asker.chat.model_id()}"
          f"{' with reranker' if asker.reranker else ' (no reranker)'}\n")

    def show(index: int, total: int, case_id: str, passed: bool | None) -> None:
        if passed is None:
            print(f"  [{index:>2}/{total}] {case_id:<22} ", end="", flush=True)
        else:
            print("pass" if passed else "FAIL")

    run = evals.run(asker, cases, args.label, show)
    print(f"\n{run.passed}/{run.total} passed in {run.seconds:.0f}s\n")
    print("By capability:")
    for name, passed, total in run.by_capability:
        print(f"  {name:<18} {passed}/{total}")
    print("By horizon:")
    for name, passed, total in run.by_horizon:
        print(f"  {name:<18} {passed}/{total}")
    failed = [c for c in run.cases if not c.passed]
    if failed:
        print("\nFailures:")
        for c in failed:
            for check in c.checks:
                if not check.passed:
                    print(f"  {c.id:<22} {check.check:<16} {truncate_to(check.detail, 90)}")
    return run


def cmd_status(args) -> None:
    with closing(Index(db_path(args))) as index:
        s = index.stats()
        books = index.books()
    print(f"\n{s.documents} documents, {s.pages} pages, {s.words:,} words")
    if s.image_only:
        print(f"{s.image_only} documents are image-only and hold no searchable text")
    print()
    for book, n in books:
        print(f"  {book:<40} {n} documents")
    print()


def cmd_hardware(args) -> None:
    state = appstate.open_state()
    hw = state.hardware()
    a = hw.accelerator
    api = {hardware.CUDA: "CUDA", hardware.METAL: "Metal", hardware.VULKAN: "Vulkan"}.get(a.kind)
    label = f"{a.name} ({api})" if api else "no usable GPU"
    print(f"\n  Accelerator : {label}")
    memory = hardware.accelerator_memory_mb(a)
    if memory:
        print(f"  Memory      : {memory:,} MB")
    print(f"  System RAM  : {hw.system_ram_mb:,} MB")
    print(f"  Tier        : {hw.tier} -- {hardware.describe_tier(hw.tier)}")
    # What the set-up wizard's model step would say here.
    print("\n  Models:")
    for line in model_choice.summary(state):
        print(f"    {line}")
    print()


def cmd_fit(args) -> None:
    print(f"\nMeasuring {args.model} against this machine at {args.ctx} context...")
    result = fit.fit_model(args.fit_exe, args.model, args.ctx)
    print(f"\n  GPU layers : {'all' if result.gpu_layers is None else result.gpu_layers}")
    print(f"  Arguments  : {' '.join(result.args)}\n")


def cmd_tools(args) -> None:
    accel = hardware.Accelerator(hardware.CPU) if args.cpu else hardware.detect().accelerator
    print(f"\nInto: {args.dest}")
    for name, variant, size in tools.plan(accel):
        print(f"  {name:<14} {variant:<16} {size // 1_048_576:,} MB")
    print()

    def show(step: tools.Step) -> None:
        if step.kind == tools.DOWNLOADING:
            percent = step.received * 100 // step.total if step.total else 0
            print(f"\r  fetching {step.file:<44} {percent:>3}%", end="", flush=True)
        elif step.kind == tools.UNPACKING:
            print(f"\r  unpacking {step.tool:<50}")
        elif step.kind == tools.DONE:
            print(f"  ok        {step.tool}")
        elif step.kind == tools.SKIPPED:
            print(f"  --        {step.tool}: {step.reason}")

    installed = tools.install(accel, args.dest, show)
    print(f"\n  kiwix-serve  : {installed.kiwix_serve or 'missing'}")
    print(f"  llama-server : {installed.llama_server or 'missing'}")
    print(f"  pdfium       : {installed.pdfium_dir}\n")


def make_asker(args, index: Index, quiet: bool = False) -> ask.Asker:
    """An asker over the index and the servers named on the command line. The
    reranker is used if it answers; the archives kiwix-serve searches itself
    join in if kiwix-serve answers."""
    reranker = Llama(args.reranker)
    use_rerank = not args.no_rerank and reranker.is_up()
    if not use_rerank and not args.no_rerank and not quiet:
        print(f"  (no reranker at {args.reranker}; falling back to keyword ranking)", file=sys.stderr)
    kiwix, native = native_books(index, args)
    return ask.Asker(index, Llama(args.llama), reranker if use_rerank else None,
                     kiwix, native, tuning.load(args.tuning))


def native_books(index: Index, args) -> tuple[Kiwix | None, list[str]]:
    """Which archives in the library are served by kiwix-serve's own index
    rather than the local one, so retrieval can search them too."""
    kiwix = Kiwix(args.kiwix)
    if not kiwix.is_up():
        return None, []
    processed = set(index.processed_books())
    folder = Path(args.library) if args.library else db_path(args).parent
    try:
        books = sorted(book_id_from_filename(p.name) for p in folder.iterdir() if p.suffix == ".zim")
    except OSError:
        books = []
    return kiwix, [b for b in books if b not in processed]


def truncate_to(s: str, n: int) -> str:
    """The text on one line, cut to n characters with "..." if it was longer."""
    flat = " ".join(s.split())
    return flat if len(flat) <= n else flat[:n] + "..."
