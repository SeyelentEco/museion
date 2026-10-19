"""Asking Hypatia a question.

The model decides, per question, whether to search the library, calculate,
both, or neither -- through two tools and nothing else. What it may *say* is
still governed by where the material came from, and that is judged from which
tools actually ran, not from what the model claims.

Pure arithmetic never reaches the model at all.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from museion import calc, llama
from museion.model_text import (cited_numbers, has_prose_arithmetic, looks_like_leaked_tool_call,
                                parse_queries, renumber_citations, strip_citations)
from museion.index import Hit, Index, is_navigation_page
from museion.tuning import Tuning
from museion.tuning import defaults as default_tuning

# How many passages to retrieve before reranking. Generous, because keyword
# search is good at recall and bad at ordering -- which is precisely what the
# reranker is there to fix.
RETRIEVE = 40

# Characters of a page passed to the reranker. Cross-encoders have a short
# window, and the opening of a page is where its subject is usually stated.
RERANK_CHARS = 1200

# Characters of a page quoted to the chat model.
CONTEXT_CHARS = 2400

# Candidates pulled per rewritten seed search. They join the question's own
# forty in one pool that is reranked once; the prompt still holds the usual
# eight passages. Every other arrangement tried put twelve to thirty passages
# in the prompt, and the model changed with the prompt: reading took thirty
# seconds before it wrote a word, and on the bleach question it did the dose
# in prose and would not touch the calculator.
SEED_RETRIEVE = 20

# Articles fetched per native archive per search.
NATIVE_PER_BOOK = 4

# Said once to an answer that worked a sum out in prose. The calculator is the
# only arithmetic allowed; "20 ÷ 8 = 2.5" written by the model is the 343.75 ml
# failure waiting to happen again.
CALCULATE_FIRST = (
    "That answer did arithmetic in prose. Do every calculation with the calculate "
    "tool, one expression each, then give the answer again reporting each figure "
    "exactly as calculate returned it, with the same citations."
)

CONTINUE = (
    "Your answer was cut off mid-sentence by a length limit. Continue exactly from "
    "where it stopped -- do not repeat anything already said, do not start over, do "
    "not add a preamble -- and finish the answer."
)

CUT_SHORT = (
    "\n\n*(This answer reached its length limit and was cut short. "
    "Ask about the remaining steps separately.)*"
)

# Said to the model when the searches run out, and again if it hands back a
# tool call as text anyway. Without it a planning question that wanted a
# seventh search produced a bare <tool_call> block as its answer.
NO_MORE_TOOLS = (
    "No more searches or calculations are available. Answer now from the passages "
    "already found, citing each by its number."
)

# Said once to an answer that cites nothing while passages were on the table.
# A hypothermia answer once came word for word from Where There Is No Doctor --
# kangarooing and all -- with no [n] anywhere, and was labelled general
# knowledge. The model is asked to point at its sources, and told plainly that
# an answer that owes them nothing may stand as it is.
CITE_OR_STAND = (
    "Your answer cites no passages. Every claim taken from the passages above must "
    "carry that passage's number, like [2]. Rewrite the answer with those citations, "
    "and if you cite anything do not say the library has nothing on this. If no part "
    "of it comes from the passages, repeat it unchanged."
)

NOTHING_FOUND = (
    "No passages in the library match that. Try different words, or if the question "
    "is factual and nothing matches after another try, say the library does not "
    "cover it."
)

# Where an answer's substance came from -- decided by the tools that ran, and
# written into saved eval runs under these names.
LIBRARY = "library"  # built on passages from the library
LIBRARY_AND_CALCULATION = "library_and_calculation"  # passages plus the calculator
COMPUTED = "computed"  # the calculator alone
# The library had nothing and no calculation was involved: whatever the model
# said came from its own training. Shown as unverified.
GENERAL_KNOWLEDGE = "general_knowledge"

# What on_token receives instead of text when everything streamed so far must
# be taken back.
#
# The model narrates before it calls a tool -- "55 × 0.25 teaspoons is 339
# millilitres... let me check" -- and that narration went straight to the
# screen, wrong number and all, before the calculator returned 68. Text is
# streamed optimistically, and if the turn turns out to end in tool calls the
# caller is told to discard it. Only the final turn's text stands.
DISCARD = None

TOOLS = [
    llama.tool(
        "search_library",
        "Search the offline reference library. Returns numbered passages with the "
        "document title and page. Cite passages by their number.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for, as keywords or a short phrase"}
            },
            "required": ["query"],
        },
    ),
    llama.tool(
        "calculate",
        "Evaluate an arithmetic expression exactly. Supports + - * / ^ %, parentheses, "
        "sqrt, abs, round, floor, ceil, min, max, pow, pi, and "
        "convert(value, from_unit, to_unit) for volume (gallon, liter, quart, pint, "
        "cup, floz, tbsp, tsp, drop, ml), mass (kg, g, lb, oz), length (km, m, cm, mm, "
        "mile, yard, foot, inch) and temperature (c, f, k). Example: "
        "convert(0.25 * 55, tsp, ml)",
        {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    ),
]


@dataclass
class Exchange:
    """One earlier question and its answer, as the person saw them."""

    question: str
    answer: str


@dataclass
class Calculation:
    expression: str
    result: str


@dataclass
class Answer:
    text: str
    sources: list[Hit]
    reranked: bool
    basis: str  # LIBRARY, LIBRARY_AND_CALCULATION, COMPUTED or GENERAL_KNOWLEDGE
    # Every calculation actually performed, verbatim. Shown to the reader as
    # ground truth: the model once reported 343.75 ml for a dose the
    # calculator had correctly returned as 67.8, by relabelling the result and
    # multiplying again in prose. The log makes that contradiction visible
    # instead of trusting the retelling.
    calculations: list[Calculation] = field(default_factory=list)


class Asker:
    def __init__(self, index: Index, chat: llama.Llama, reranker: llama.Llama | None = None,
                 kiwix=None, native_books: list[str] | None = None, tuning: Tuning | None = None) -> None:
        self.index = index
        self.chat = chat
        self.reranker = reranker
        # The content server, for archives whose own full-text index works.
        # Without it only the locally indexed PDF collections are searched --
        # Wikipedia and the like would be browsable but not askable.
        self.kiwix = kiwix
        # Book ids served by kiwix-serve that the local index does not hold.
        self.native_books = native_books or []
        # The prompts and limits, loaded by the caller -- from the override
        # file on every question in the app, so a prompt edit needs no restart.
        self.tuning = tuning or default_tuning()

    def gather(self, question: str) -> tuple[list[Hit], bool]:
        """Retrieve and rerank, returning the passages an answer would be
        built on, and whether the reranker ordered them.

        Whether the library covers the question at all is decided by the
        reranker when there is one, and by term coverage only when there is
        not. Term overlap is a blunt instrument: it refused a canning question
        because "fridge" and "nobody" do not appear anywhere in the corpus,
        while the reranker had the pressure-canning procedure scored at the
        top."""
        candidates, best_coverage = self.index.search_ungated(question, RETRIEVE)
        candidates.extend(self.native_hits(question, set()))
        return self.choose(question, candidates, best_coverage)

    def gather_seeded(self, question: str, rewrites: list[str], fetched: set[str]) -> tuple[list[Hit], bool]:
        """The seeded search: the question's candidates and the rewritten
        subjects' candidates in one pool, duplicates dropped, reranked once
        against the question *and* its subjects, keeping `keep` in all. The
        cross-encoder sees "how can I help deliver this baby, not near a
        medical center" together with "delivering a baby, stages of labor", so
        the chapter pages compete with the when-to-seek-help pages on the
        subject rather than on the person's wording. `fetched` holds the
        articles already read in this pass, so none is read twice."""
        pool: list[Hit] = []
        seen: set[tuple[str, int, str]] = set()

        def add(hits: list[Hit]) -> None:
            for h in hits:
                if (h.book, h.page, h.file) not in seen:
                    seen.add((h.book, h.page, h.file))
                    pool.append(h)

        found, best_coverage = self.index.search_ungated(question, RETRIEVE)
        add(found)
        add(self.native_hits(question, fetched))
        for q in rewrites:
            found, _ = self.index.search_ungated(q, SEED_RETRIEVE)
            add(found)
            add(self.native_hits(q, fetched))
        rank_query = f"{question} ({'; '.join(rewrites)})" if rewrites else question
        return self.choose(rank_query, pool, best_coverage)

    def choose(self, query: str, pool: list[Hit], best_coverage: float) -> tuple[list[Hit], bool]:
        """Pick the passages to keep from a pool of candidates: reranked
        against `query` when there is a reranker, judged by the keyword
        search's term coverage when there is not."""
        if not pool:
            return [], False
        keep = self.tuning.keep
        if self.reranker is None:
            return self._by_keywords(pool, best_coverage), False

        docs = [truncate_chars(f"{h.doc_title}. {h.body}", RERANK_CHARS) for h in pool]
        try:
            scored = self.reranker.rerank(query, docs)
        except (OSError, RuntimeError, ValueError, KeyError) as e:
            # Reranking is an improvement, not a dependency. Losing it
            # degrades ordering; it must not lose the answer. The term gate
            # stands in for it.
            print(f"  (reranker unavailable, using keyword ranking: {e})", file=sys.stderr)
            return self._by_keywords(pool, best_coverage), False

        # The reranker's verdict on coverage: if even its best passage scores
        # as irrelevant, the library does not hold the answer.
        least = self.tuning.min_rerank_score
        if not scored or scored[0][1] < least:
            return [], True
        picked = [pool[i] for i, score in scored if score >= least and i < len(pool)][:keep]
        # Demote contents and index pages *after* reranking. Doing it before is
        # pointless: the reranker re-sorts, and it likes them, because an index
        # page really does mention every term in the question. It just cannot
        # answer one.
        picked.sort(key=lambda h: is_navigation_page(h.body))
        return picked, True

    def _by_keywords(self, pool: list[Hit], best_coverage: float) -> list[Hit]:
        if best_coverage + 1e-9 < Index.MIN_TERM_COVERAGE:
            return []
        return pool[: self.tuning.keep]

    def native_hits(self, question: str, fetched: set[str]) -> list[Hit]:
        """Search the archives that carry their own index, through kiwix-serve,
        and read the matching articles. They join the local candidates and go
        through the same reranker, so a Wikipedia article competes on equal
        terms with a page of a PDF."""
        if self.kiwix is None:
            return []
        out = []
        for book in self.native_books:
            try:
                found = self.kiwix.search(book, question, NATIVE_PER_BOOK)
            except (OSError, RuntimeError, ValueError):
                continue
            for h in found:
                path = h.path()
                if not path:
                    continue
                # Reading an article is the expensive part of a native search;
                # the same one found by two searches is read once.
                if f"{book}/{path}" in fetched:
                    continue
                fetched.add(f"{book}/{path}")
                try:
                    text = self.kiwix.article_text(book, path)
                except (OSError, RuntimeError, ValueError):
                    continue
                if len(text.split()) < 30:
                    continue
                out.append(Hit(
                    doc_title=h.title, author="", book=book, file=path,
                    # Articles have no page; 0 means "the article itself".
                    page=0, snippet=h.snippet, rank=0.0, coverage=1.0,
                    body=focus(text, question, CONTEXT_CHARS * 2),
                ))
        return out

    def ask(self, question: str, history: Sequence[Exchange] = (),
            on_token: Callable[[str | None], None] = lambda t: None) -> Answer:
        """Answer the question, streaming to `on_token` as it arrives (and
        DISCARD when what was streamed must be taken back). `history` is the
        conversation so far, oldest first, so that a follow-up like "what
        about the placenta?" or "I'm not near a hospital, how do I deliver
        this baby?" is read against the exchange it follows."""
        # Arithmetic never reaches the model. "10*10" is not a library question
        # and a language model is the wrong instrument for it anyway.
        if calc.looks_like_arithmetic(question):
            return self._compute(question, on_token)

        # MUSEION_TRACE=1 prints where the seconds go: rewrite, each search,
        # each model turn. The dev loop's stopwatch.
        started = time.monotonic()
        trace = "MUSEION_TRACE" in os.environ

        def mark(what: str) -> None:
            if trace:
                print(f"  [{time.monotonic() - started:6.1f}s] {what}", file=sys.stderr)

        tuning = self.tuning
        messages = [llama.system(tuning.system_prompt)]
        # Earlier exchanges give a follow-up its subject. Their citation
        # markers are stripped: those numbers pointed at passages from an
        # earlier search, and would otherwise be repeated as if they named the
        # passages found for this question.
        for x in recent(history, tuning.max_history):
            messages.append(llama.user(x.question))
            messages.append(llama.assistant(strip_citations(truncate_chars(x.answer, tuning.history_chars))))
        messages.append(llama.user(question))
        sources: list[Hit] = []
        calculations: list[Calculation] = []

        # The library is searched before the model says a word. Left to decide
        # for itself, it skipped the search on three factual questions in one
        # benchmark run -- a bleeding wound among them -- and answered from
        # general knowledge, when the same question had cited Where There Is
        # No Doctor every time under the old retrieval-first loop. Whether to
        # consult the library is not the model's call; only how to use what it
        # finds is. The seeded search is replayed as a tool exchange so the
        # model sees it exactly as it would its own.
        #
        # What is searched for is the subject, not the sentence. "My friend
        # just went into labor, what are the steps I should take to help with
        # her delivery? I've never done this before" retrieved pages on
        # comforting a labouring woman and on when to go to a medical centre,
        # and the answer said the library had nothing on delivering a baby --
        # while Where There Is No Doctor's "The stages of labor" sat unread. So
        # the model first names what a reference book would have a chapter on,
        # and those queries are searched ahead of the question itself.
        queries = self.seed_queries(question, history)
        mark(f"rewrite -> {queries}")
        hits, reranked = self.gather_seeded(question, queries, set())
        mark(f"seeded search ({len(queries) + 1} queries): {len(hits)} passages")
        result = render_search(hits, sources)
        seed_call = {
            "id": "seed_search",
            "type": "function",
            "function": {
                "name": "search_library",
                "arguments": json.dumps({"query": " | ".join(queries + [question])},
                                        separators=(",", ":"), ensure_ascii=False),
            },
        }
        messages.append(llama.assistant_calls("", [seed_call]))
        messages.append(llama.tool_result("seed_search", result))

        repaired = False
        math_repaired = False
        round = 0
        while True:
            # On the last round the tools are withheld so the model must answer.
            last = round >= tuning.max_rounds
            if round == tuning.max_rounds:
                messages.append(llama.user(NO_MORE_TOOLS))
            round += 1
            turn = self.chat.turn(messages, None if last else TOOLS, tuning.answer_tokens, on_token)
            mark(f"turn {round}: {len(turn.tool_calls)} tool call(s), {len(turn.text)} chars")

            if turn.tool_calls:
                # Whatever it narrated on the way to the tool call is not the
                # answer, and may contain numbers it made up. Take it back.
                on_token(DISCARD)
                # Replay the model's request, then answer each call.
                messages.append(llama.assistant_calls(turn.text, turn.tool_calls))
                for call in turn.tool_calls:
                    output, rr = self._run_tool(call, sources, calculations)
                    reranked |= rr
                    messages.append(llama.tool_result(call["id"], output))
                continue

            text = self._finish(turn, messages, on_token)

            # A sum worked out in prose is sent back once, with the tools still
            # on offer, to be done by the calculator.
            if not math_repaired and not calculations and round < tuning.max_rounds and has_prose_arithmetic(text):
                math_repaired = True
                on_token(DISCARD)
                messages.append(llama.assistant(text))
                messages.append(llama.user(CALCULATE_FIRST))
                continue

            # The library is always searched now, so "did retrieval find
            # something" no longer says anything about the answer. What does is
            # whether the answer *cites* it: a passage the model was handed and
            # ignored did not ground anything. Only cited sources are kept, in
            # citation order, and the label follows.
            cited = cited_numbers(text)
            leaked = looks_like_leaked_tool_call(text)
            if not repaired and (leaked or (not cited and sources)):
                # One more turn, tools withheld: cite the passages, or answer
                # at all if what came back was a tool call as text. What was
                # streamed so far is taken back.
                repaired = True
                on_token(DISCARD)
                messages.append(llama.assistant(text))
                messages.append(llama.user(NO_MORE_TOOLS if leaked else CITE_OR_STAND))
                round = tuning.max_rounds + 1
                continue

            used = [sources[n - 1] for n in cited if n <= len(sources)]
            if used:
                basis = LIBRARY_AND_CALCULATION if calculations else LIBRARY
            else:
                basis = COMPUTED if calculations else GENERAL_KNOWLEDGE
            return Answer(renumber_citations(text, cited), used, reranked, basis, calculations)

    def _compute(self, question: str, on_token) -> Answer:
        """The answer to a question that is nothing but arithmetic."""
        expr = calc.normalise(question)
        calculations = []
        try:
            result = calc.format_number(calc.evaluate(expr))
            text = f"{expr} = **{result}**"
            calculations.append(Calculation(expr, result))
        except ValueError as e:
            text = f"Could not evaluate `{expr}`: {e}"
        on_token(text)
        return Answer(text, [], False, COMPUTED, calculations)

    def _finish(self, turn: llama.Turn, messages: list[dict], on_token) -> str:
        """The text of a turn that answered, carried on past the token limit.

        An answer that ran into the limit is not finished; it is asked to
        carry on from the cut, and the reader sees the continuation stream in
        behind the first part. If it runs out again after the allowed
        continuations, say so rather than end mid-sentence as if that were the
        whole answer."""
        text, limited = turn.text, turn.hit_token_limit
        continuations = 0
        while limited and continuations < self.tuning.max_continuations:
            messages.append(llama.assistant(text))
            messages.append(llama.user(CONTINUE))
            more = self.chat.turn(messages, None, self.tuning.answer_tokens, on_token)
            if not text[-1:].isspace() and not more.text[:1].isspace():
                text += " "
                on_token(" ")
            text += more.text
            limited = more.hit_token_limit
            continuations += 1
        if limited:
            text += CUT_SHORT
            on_token(CUT_SHORT)
        return text

    def _run_tool(self, call: dict, sources: list[Hit], calculations: list[Calculation]) -> tuple[str, bool]:
        """Carry out one tool call. Returns what to tell the model, and
        whether a search was reranked."""
        name = call["function"]["name"]
        arguments = call["function"]["arguments"]
        if name == "search_library":
            hits, reranked = self.gather(argument(arguments, "query"))
            return render_search(hits, sources), reranked
        if name == "calculate":
            expr = argument(arguments, "expression")
            try:
                result = calc.format_number(calc.evaluate(expr))
            except ValueError as e:
                return (f"Could not evaluate `{expr}`: {e}. Rewrite it using only numbers, "
                        "operators, and the listed functions."), False
            calculations.append(Calculation(expr, result))
            return f"{expr} = {result}. Report this figure exactly; do not convert or rescale it.", False
        return f"Unknown tool {name}.", False

    def seed_queries(self, question: str, history: Sequence[Exchange]) -> list[str]:
        """The plain subjects behind a question, as up to three short library
        searches, written by the model with the earlier exchanges in view so
        that "this baby" resolves. On any failure the list is empty and the
        question itself is searched, as before."""
        context = ""
        for x in recent(history, self.tuning.max_history):
            context += f"Earlier question: {x.question.strip()}\n"
        context += f"Question: {question.strip()}\nSearches:"
        messages = [llama.system(self.tuning.rewrite_prompt), llama.user(context)]
        try:
            text = self.chat.chat(messages, 80)
        except (OSError, RuntimeError) as e:
            print(f"  (search rewrite unavailable: {e})", file=sys.stderr)
            return []
        return parse_queries(text, question)


def render_search(hits: list[Hit], sources: list[Hit]) -> str:
    """Format search hits for the model, numbering them continuously across
    searches so citations stay unambiguous. A passage already shown keeps its
    number. `sources` is every passage shown so far, in number order; new ones
    are added to it."""
    if not hits:
        return NOTHING_FOUND
    out = ""
    for h in hits:
        # The file is part of what makes a passage the same one: articles
        # from a native archive all have page 0. (The Rust version compared
        # book and page only, so a second article from the same archive was
        # shown under the first one's number and could never be cited.)
        number = next((i + 1 for i, s in enumerate(sources)
                       if (s.book, s.page, s.file) == (h.book, h.page, h.file)), 0)
        if not number:
            sources.append(h)
            number = len(sources)
        who = f", {h.author}" if h.author else ""
        where = f" (page {h.page})" if h.page > 0 else f" ({h.book})"
        out += f"[{number}] {h.doc_title}{who}{where}\n{truncate_chars(h.body, CONTEXT_CHARS)}\n\n"
    return out


def recent(history: Sequence[Exchange], n: int) -> list[Exchange]:
    """The last n exchanges, oldest first."""
    return list(history)[max(len(history) - n, 0):]


def argument(arguments: str, name: str) -> str:
    """One string argument from a tool call's JSON. A model that wrote the
    arguments as bare text instead gets that text used whole."""
    try:
        value = json.loads(arguments)[name]
    except (ValueError, KeyError, TypeError, IndexError):
        return arguments
    return value if isinstance(value, str) else arguments


def focus(text: str, question: str, chars: int) -> str:
    """The stretch of an article most likely to answer the question: a window
    around the first occurrence of its most distinctive term, or the opening if
    nothing matches. Long articles would otherwise contribute only their lead
    section."""
    lower = text.lower()
    terms = [w.lower() for w in re.findall(r"[^\W_]+", question) if len(w) > 3]
    # Longest term first: the more specific word is the better anchor.
    anchor = next((lower.find(t) for t in sorted(terms, key=len, reverse=True) if t in lower), 0)
    start = max(anchor - chars // 4, 0)
    return text[start:start + chars]


def truncate_chars(s: str, n: int) -> str:
    return s[:n]
