"""Asking Hypatia a question, and the library she searches.

The Asker is the library's search -- keyword search, the archives' own
indexes, the reranker -- which the search_library tool uses for every agent.
Asking a question runs the librarian agent (agents/librarian/) through the
harness (harness.py). The model decides, per question, whether to search
again, calculate, both, or neither; what it may *say* is still governed by
where the material came from, and that is judged from which tools actually
ran, not from what the model claims.

Pure arithmetic never reaches the model at all.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections.abc import Callable, Sequence

from museion import llama, notes
from museion.agents import AgentSpec
from museion.agents import load as load_agent
from museion.answer import (COMPUTED, DISCARD, GENERAL_KNOWLEDGE, LIBRARY,  # noqa: F401 -- named here as before
                            LIBRARY_AND_CALCULATION, Answer, Calculation, Exchange)
from museion.guards import (CALCULATE_FIRST, CALL_AGAIN, CITE_OR_STAND, CONTINUE,  # noqa: F401
                            CUT_SHORT, NO_ANSWER, NO_MORE_TOOLS)
from museion.hardware import CAPABLE
from museion.index import Hit, Index, is_navigation_page
from museion.model_text import parse_queries
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

# For keeping a conversation inside the model's context: characters per token,
# erring low (English prose runs nearer four), and tokens kept free for the
# tool definitions and the chat template around the messages.
CHARS_PER_TOKEN = 3.5
RESERVED_TOKENS = 1500

# Candidates pulled per rewritten seed search. They join the question's own
# forty in one pool that is reranked once; the prompt still holds the usual
# eight passages. Every other arrangement tried put twelve to thirty passages
# in the prompt, and the model changed with the prompt: reading took thirty
# seconds before it wrote a word, and on the bleach question it did the dose
# in prose and would not touch the calculator.
SEED_RETRIEVE = 20

# Articles fetched per native archive per search.
NATIVE_PER_BOOK = 4

# Parts of the person's own notes added to the candidates per search. They
# go through the reranker with everything else.
NOTES_RETRIEVE = 10

NOTHING_FOUND = (
    "No passages in the library match that. Try different words, or if the question "
    "is factual and nothing matches after another try, say the library does not "
    "cover it."
)



def _tools() -> list[dict]:
    from museion.agent_tools import all_tools
    return [all_tools()[name].schema() for name in ("search_library", "calculate")]


# The librarian's two tools, as the model is offered them (agent_tools/).
TOOLS = _tools()


def librarian(tuning: Tuning) -> AgentSpec:
    """The librarian agent as shipped, with its prompt and limits taken from
    tuning.toml (and the person's override of it, which Settings edits), as
    they always were."""
    spec = load_agent("librarian", None, CAPABLE)
    spec.prompt = tuning.system_prompt
    spec.max_rounds = tuning.max_rounds
    spec.answer_tokens = tuning.answer_tokens
    spec.max_continuations = tuning.max_continuations
    spec.max_history = tuning.max_history
    spec.history_chars = tuning.history_chars
    return spec


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

    def gather(self, question: str, notes_dir=None) -> tuple[list[Hit], bool]:
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
        mine, mine_coverage = self.notes_hits(question, notes_dir)
        candidates.extend(mine)
        return self.choose(question, candidates, max(best_coverage, mine_coverage))

    def gather_seeded(self, question: str, rewrites: list[str], fetched: set[str],
                      notes_dir=None) -> tuple[list[Hit], bool]:
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
        mine, mine_coverage = self.notes_hits(question, notes_dir)
        add(mine)
        best_coverage = max(best_coverage, mine_coverage)
        for q in rewrites:
            found, _ = self.index.search_ungated(q, SEED_RETRIEVE)
            add(found)
            add(self.native_hits(q, fetched))
            add(self.notes_hits(q, notes_dir)[0])
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

    def notes_hits(self, question: str, notes_dir) -> tuple[list[Hit], float]:
        """The person's own notes that match (notes.py). A notes index that
        cannot be built -- a folder that cannot be written -- is skipped, not
        the end of the question."""
        if notes_dir is None:
            return [], 0.0
        try:
            return notes.search(notes_dir, question, NOTES_RETRIEVE, self.index)
        except (OSError, RuntimeError, sqlite3.Error) as e:
            print(f"  (notes not searched: {e})", file=sys.stderr)
            return [], 0.0

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
            on_token: Callable[[str | None], None] = lambda t: None, setting=None) -> Answer:
        """Answer the question, streaming to `on_token` as it arrives (and
        DISCARD when what was streamed must be taken back). `history` is the
        conversation so far, oldest first, so that a follow-up like "what
        about the placenta?" or "I'm not near a hospital, how do I deliver
        this baby?" is read against the exchange it follows.

        This is the librarian agent (agents/librarian/) run by the harness
        (harness.py), with this Asker as its library. Its ways, set in its
        agent.toml:

        - Arithmetic never reaches the model. "10*10" is not a library
          question and a language model is the wrong instrument for it.
        - The library is searched before the model says a word. Left to
          decide for itself, it skipped the search on three factual questions
          in one benchmark run -- a bleeding wound among them -- and answered
          from general knowledge. Whether to consult the library is not the
          model's call; only how to use what it finds is.
        - What is searched for is the subject, not the sentence. "My friend
          just went into labor, what are the steps I should take to help with
          her delivery?" retrieved pages on comforting a labouring woman, and
          the answer said the library had nothing on delivering a baby, while
          Where There Is No Doctor's "The stages of labor" sat unread. So the
          model first names what a reference book would have a chapter on
          (seed_queries), and those are searched with the question."""
        from museion.harness import Harness, Setting
        spec = librarian(self.tuning)
        setting = setting or Setting(chat=self.chat)
        setting.library = self
        return Harness(spec, setting).run(question, history, on_token)

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
        # from a native archive all have page 0. (Compared by book and page
        # only, a second article from the same archive would be shown under
        # the first one's number and could never be cited.)
        number = next((i + 1 for i, s in enumerate(sources)
                       if (s.book, s.page, s.file) == (h.book, h.page, h.file)), 0)
        who = f", {h.author}" if h.author else ""
        where = f" (page {h.page})" if h.page > 0 else f" ({h.book})"
        if h.book == notes.NOTES_BOOK:
            # Said in the passage itself, so the model can tell the person's
            # own words from a book's without a rule in its prompt.
            who, where = "", f" -- the person's own note, not a book (part {h.page})"
        if number:
            # Shown in full once is enough; repeating it filled the context.
            out += f"[{number}] {h.doc_title}{who}{where}: shown above\n\n"
            continue
        sources.append(h)
        number = len(sources)
        out += f"[{number}] {h.doc_title}{who}{where}\n{truncate_chars(h.body, CONTEXT_CHARS)}\n\n"
    return out


from museion.compaction import prompt_chars  # noqa: E402,F401 -- where it always was


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
