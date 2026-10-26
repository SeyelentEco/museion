//! Asking Hypatia a question.
//!
//! The model decides, per question, whether to search the library, calculate,
//! both, or neither -- through two tools and nothing else. What it may *say*
//! is still governed by where the material came from, and that is judged from
//! which tools actually ran, not from what the model claims.
//!
//! Pure arithmetic never reaches the model at all.

use crate::{
    calc,
    tuning::Tuning,
    index::Hit,
    llama::{Llama, Message, Tool, ToolCall},
    Index,
};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::time::Instant;

/// How many passages to retrieve before reranking. Generous, because keyword
/// search is good at recall and bad at ordering -- which is precisely what the
/// reranker is there to fix.
const RETRIEVE: usize = 40;

/// Characters of a page passed to the reranker. Cross-encoders have a short
/// window, and the opening of a page is where its subject is usually stated.
const RERANK_CHARS: usize = 1200;

/// Characters of a page quoted to the chat model.
const CONTEXT_CHARS: usize = 2400;

/// Candidates pulled per rewritten seed search. They join the question's
/// own forty in one pool that is reranked once; the prompt still holds the
/// usual eight passages. Every other arrangement tried put twelve to thirty
/// passages in the prompt, and the model changed with the prompt: reading
/// took thirty seconds before it wrote a word, and on the bleach question
/// it did the dose in prose and would not touch the calculator.
const SEED_RETRIEVE: usize = 20;

/// Said once to an answer that worked a sum out in prose. The calculator is
/// the only arithmetic allowed; "20 ÷ 8 = 2.5" written by the model is the
/// 343.75 ml failure waiting to happen again.
const CALCULATE_FIRST: &str = "That answer did arithmetic in prose. Do every \
calculation with the calculate tool, one expression each, then give the answer \
again reporting each figure exactly as calculate returned it, with the same \
citations.";

const CONTINUE: &str = "Your answer was cut off mid-sentence by a length limit. \
Continue exactly from where it stopped -- do not repeat anything already said, do \
not start over, do not add a preamble -- and finish the answer.";

/// One earlier question and its answer, as the person saw them.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Exchange {
    pub question: String,
    pub answer: String,
}

const CUT_SHORT: &str = "\n\n*(This answer reached its length limit and was cut short. \
Ask about the remaining steps separately.)*";

/// Said to the model when the searches run out, and again if it hands back
/// a tool call as text anyway. Without it a planning question that wanted a
/// seventh search produced a bare `<tool_call>` block as its answer.
const NO_MORE_TOOLS: &str = "No more searches or calculations are available. \
Answer now from the passages already found, citing each by its number.";

/// Said once to an answer that cites nothing while passages were on the
/// table. A hypothermia answer once came word for word from Where There Is
/// No Doctor -- kangarooing and all -- with no [n] anywhere, and was labelled
/// general knowledge. The model is asked to point at its sources, and told
/// plainly that an answer that owes them nothing may stand as it is.
const CITE_OR_STAND: &str = "Your answer cites no passages. Every claim taken \
from the passages above must carry that passage's number, like [2]. Rewrite \
the answer with those citations, and if you cite anything do not say the library \
has nothing on this. If no part of it comes from the passages, repeat it unchanged.";

pub struct Asker<'a> {
    pub index: &'a Index,
    pub chat: &'a Llama,
    pub reranker: Option<&'a Llama>,
    /// The content server, for archives whose own full-text index works.
    /// Without it only the locally indexed PDF collections are searched --
    /// Wikipedia and the like would be browsable but not askable.
    pub kiwix: Option<&'a crate::Kiwix>,
    /// Book ids served by kiwix-serve that the local index does not hold.
    pub native_books: Vec<String>,
    /// The prompts and limits, loaded by the caller -- from the override
    /// file on every question in the app, so a prompt edit needs no build.
    pub tuning: Tuning,
}

/// Articles fetched per native archive per search.
const NATIVE_PER_BOOK: usize = 4;

/// Where an answer's substance came from -- decided by the tools that ran.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Basis {
    /// Built on passages from the library.
    Library,
    /// Library passages plus arithmetic from the calculator.
    LibraryAndCalculation,
    /// The calculator alone.
    Computed,
    /// The library had nothing and no calculation was involved: whatever the
    /// model said came from its own training. Shown as unverified.
    GeneralKnowledge,
}

pub struct Answer {
    pub text: String,
    pub sources: Vec<Hit>,
    pub reranked: bool,
    pub basis: Basis,
    /// Every calculation actually performed, verbatim. Shown to the reader as
    /// ground truth: the model once reported 343.75 ml for a dose the
    /// calculator had correctly returned as 67.8, by relabelling the result
    /// and multiplying again in prose. The log makes that contradiction
    /// visible instead of trusting the retelling.
    pub calculations: Vec<Calculation>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Calculation {
    pub expression: String,
    pub result: String,
}

/// What the streaming callback receives.
///
/// The model narrates before it calls a tool -- "55 × 0.25 teaspoons is 339
/// millilitres… let me check" -- and that narration went straight to the
/// screen, wrong number and all, before the calculator returned 68. Text is
/// streamed optimistically, and if the turn turns out to end in tool calls the
/// caller is told to discard it. Only the final turn's text stands.
#[derive(Debug, Clone, Copy)]
pub enum Token<'a> {
    Text(&'a str),
    /// Throw away everything streamed so far for this answer; the model is
    /// consulting a tool and will write again.
    Discard,
}

fn tools() -> Vec<Tool> {
    vec![
        Tool::function(
            "search_library",
            "Search the offline reference library. Returns numbered passages with the \
             document title and page. Cite passages by their number.",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "query": { "type": "string", "description": "What to search for, as keywords or a short phrase" }
                },
                "required": ["query"]
            }),
        ),
        Tool::function(
            "calculate",
            "Evaluate an arithmetic expression exactly. Supports + - * / ^ %, parentheses, \
             sqrt, abs, round, floor, ceil, min, max, pow, pi, and \
             convert(value, from_unit, to_unit) for volume (gallon, liter, quart, pint, \
             cup, floz, tbsp, tsp, drop, ml), mass (kg, g, lb, oz), length (km, m, cm, mm, \
             mile, yard, foot, inch) and temperature (c, f, k). Example: \
             convert(0.25 * 55, tsp, ml)",
            serde_json::json!({
                "type": "object",
                "properties": {
                    "expression": { "type": "string" }
                },
                "required": ["expression"]
            }),
        ),
    ]
}

#[derive(Deserialize)]
struct SearchArgs {
    query: String,
}

#[derive(Deserialize)]
struct CalcArgs {
    expression: String,
}

impl<'a> Asker<'a> {
    /// Retrieve and rerank, returning the passages an answer would be built on.
    ///
    /// Whether the library covers the question at all is decided by the
    /// reranker when there is one, and by term coverage only when there is
    /// not. Term overlap is a blunt instrument: it refused a canning question
    /// because "fridge" and "nobody" do not appear anywhere in the corpus,
    /// while the reranker had the pressure-canning procedure scored at the top.
    pub fn gather(&self, question: &str) -> Result<(Vec<Hit>, bool)> {
        self.gather_with(question, RETRIEVE, self.tuning.keep, &mut HashSet::new())
    }

    /// `gather` with the knobs exposed: how many candidates to pull, how many
    /// passages to keep, and which articles have already been fetched by an
    /// earlier search in the same seeding pass, so they are not fetched again.
    fn gather_with(
        &self,
        question: &str,
        retrieve: usize,
        keep: usize,
        fetched: &mut HashSet<String>,
    ) -> Result<(Vec<Hit>, bool)> {
        let (mut candidates, best_coverage) = self.index.search_ungated(question, retrieve)?;
        candidates.extend(self.native_hits(question, fetched));
        if candidates.is_empty() {
            return Ok((Vec::new(), false));
        }

        let Some(reranker) = self.reranker else {
            if best_coverage + 1e-9 < crate::index::Index::MIN_TERM_COVERAGE {
                return Ok((Vec::new(), false));
            }
            return Ok((candidates.into_iter().take(keep).collect(), false));
        };

        let docs: Vec<String> = candidates
            .iter()
            .map(|h| truncate_chars(&format!("{}. {}", h.doc_title, h.body), RERANK_CHARS))
            .collect();

        match reranker.rerank(question, &docs) {
            Ok(scored) => {
                // The reranker's verdict on coverage: if even its best passage
                // scores as irrelevant, the library does not hold the answer.
                if scored.first().map_or(true, |(_, s)| *s < self.tuning.min_rerank_score) {
                    return Ok((Vec::new(), true));
                }
                let mut picked: Vec<Hit> = scored
                    .into_iter()
                    .take(keep)
                    .filter(|(_, s)| *s >= self.tuning.min_rerank_score)
                    .filter_map(|(i, _)| candidates.get(i).cloned())
                    .collect();
                // Demote contents and index pages *after* reranking. Doing it
                // before is pointless: the reranker re-sorts, and it likes them,
                // because an index page really does mention every term in the
                // question. It just cannot answer one.
                picked.sort_by_key(|h| crate::index::is_navigation_page(&h.body));
                Ok((picked, true))
            }
            Err(e) => {
                // Reranking is an improvement, not a dependency. Losing it
                // degrades ordering; it must not lose the answer. The term
                // gate stands in for it.
                eprintln!("  (reranker unavailable, using keyword ranking: {e})");
                if best_coverage + 1e-9 < crate::index::Index::MIN_TERM_COVERAGE {
                    return Ok((Vec::new(), false));
                }
                Ok((candidates.into_iter().take(keep).collect(), false))
            }
        }
    }

    /// The seeded search: the question's candidates and the rewritten
    /// subjects' candidates in one pool, duplicates dropped, reranked once
    /// against the question *and* its subjects, keeping `keep` in all. The
    /// cross-encoder sees "how can I help deliver this baby, not near a
    /// medical center" together with "delivering a baby, stages of labor",
    /// so the chapter pages compete with the when-to-seek-help pages on the
    /// subject rather than on the person's wording. Coverage is judged as in
    /// `gather`: a best passage below the threshold means the library does
    /// not hold the answer.
    fn gather_seeded(
        &self,
        question: &str,
        rewrites: &[String],
        keep: usize,
        fetched: &mut HashSet<String>,
    ) -> Result<(Vec<Hit>, bool)> {
        let mut pool: Vec<Hit> = Vec::new();
        let mut seen: HashSet<(String, usize, String)> = HashSet::new();
        let mut push = |hits: Vec<Hit>, pool: &mut Vec<Hit>| {
            for h in hits {
                if seen.insert((h.book.clone(), h.page, h.file.clone())) {
                    pool.push(h);
                }
            }
        };
        let (found, best_coverage) = self.index.search_ungated(question, RETRIEVE)?;
        push(found, &mut pool);
        push(self.native_hits(question, fetched), &mut pool);
        for q in rewrites {
            let (found, _) = self.index.search_ungated(q, SEED_RETRIEVE)?;
            push(found, &mut pool);
            push(self.native_hits(q, fetched), &mut pool);
        }
        if pool.is_empty() {
            return Ok((Vec::new(), false));
        }

        let Some(reranker) = self.reranker else {
            if best_coverage + 1e-9 < crate::index::Index::MIN_TERM_COVERAGE {
                return Ok((Vec::new(), false));
            }
            return Ok((pool.into_iter().take(keep).collect(), false));
        };
        let rank_query = if rewrites.is_empty() {
            question.to_string()
        } else {
            format!("{question} ({})", rewrites.join("; "))
        };
        let docs: Vec<String> = pool
            .iter()
            .map(|h| truncate_chars(&format!("{}. {}", h.doc_title, h.body), RERANK_CHARS))
            .collect();
        match reranker.rerank(&rank_query, &docs) {
            Ok(scored) => {
                if scored.first().map_or(true, |(_, s)| *s < self.tuning.min_rerank_score) {
                    return Ok((Vec::new(), true));
                }
                let mut picked: Vec<Hit> = scored
                    .into_iter()
                    .filter(|(_, s)| *s >= self.tuning.min_rerank_score)
                    .take(keep)
                    .filter_map(|(i, _)| pool.get(i).cloned())
                    .collect();
                picked.sort_by_key(|h| crate::index::is_navigation_page(&h.body));
                Ok((picked, true))
            }
            Err(e) => {
                eprintln!("  (reranker unavailable, using keyword ranking: {e})");
                if best_coverage + 1e-9 < crate::index::Index::MIN_TERM_COVERAGE {
                    return Ok((Vec::new(), false));
                }
                Ok((pool.into_iter().take(keep).collect(), false))
            }
        }
    }

    /// Search the archives that carry their own index, through kiwix-serve,
    /// and read the matching articles. They join the local candidates and go
    /// through the same reranker, so a Wikipedia article competes on equal
    /// terms with a page of a PDF.
    fn native_hits(&self, question: &str, fetched: &mut HashSet<String>) -> Vec<Hit> {
        let Some(kiwix) = self.kiwix else {
            return Vec::new();
        };
        let mut out = Vec::new();
        for book in &self.native_books {
            let hits = match kiwix.search(book, question, NATIVE_PER_BOOK) {
                Ok(h) => h,
                Err(_) => continue,
            };
            for h in hits {
                let Some(path) = h.path() else { continue };
                // Reading an article is the expensive part of a native
                // search; the same one found by two searches is read once.
                if !fetched.insert(format!("{book}/{path}")) {
                    continue;
                }
                let Ok(text) = kiwix.article_text(book, &path) else { continue };
                if text.split_whitespace().count() < 30 {
                    continue;
                }
                out.push(Hit {
                    doc_title: h.title.clone(),
                    author: String::new(),
                    book: book.clone(),
                    file: path,
                    // Articles have no page; 0 means "the article itself".
                    page: 0,
                    snippet: h.snippet.clone(),
                    rank: 0.0,
                    coverage: 1.0,
                    body: focus(&text, question, CONTEXT_CHARS * 2),
                });
            }
        }
        out
    }

    /// Answer the question, streaming to `on_token` as it arrives. `history`
    /// is the conversation so far, oldest first, so that a follow-up like
    /// "what about the placenta?" or "I'm not near a hospital, how do I
    /// deliver this baby?" is read against the exchange it follows.
    pub fn ask(&self, question: &str, history: &[Exchange], mut on_token: impl FnMut(Token)) -> Result<Answer> {
        // Arithmetic never reaches the model. "10*10" is not a library question
        // and a language model is the wrong instrument for it anyway.
        if calc::looks_like_arithmetic(question) {
            let expr = calc::normalise(question);
            let text = match calc::evaluate(&expr) {
                Ok(v) => format!("{expr} = **{}**", calc::format(v)),
                Err(e) => format!("Could not evaluate `{expr}`: {e}"),
            };
            on_token(Token::Text(&text));
            let calculations = calc::evaluate(&expr)
                .ok()
                .map(|v| vec![Calculation { expression: expr.clone(), result: calc::format(v) }])
                .unwrap_or_default();
            return Ok(Answer { text, sources: Vec::new(), reranked: false, basis: Basis::Computed, calculations });
        }

        // MUSEION_TRACE=1 prints where the seconds go: rewrite, each search,
        // each model turn. The dev loop's stopwatch.
        let t0 = Instant::now();
        let trace = std::env::var_os("MUSEION_TRACE").is_some();
        let mark = |what: &str| {
            if trace {
                eprintln!("  [{:6.1}s] {what}", t0.elapsed().as_secs_f64());
            }
        };

        let tools = tools();
        let mut messages = vec![Message::system(self.tuning.system_prompt.clone())];
        // Earlier exchanges give a follow-up its subject. Their citation
        // markers are stripped: those numbers pointed at passages from an
        // earlier search, and would otherwise be repeated as if they named
        // the passages found for this question.
        for x in history.iter().rev().take(self.tuning.max_history).collect::<Vec<_>>().into_iter().rev() {
            messages.push(Message::user(x.question.clone()));
            messages.push(Message::assistant(strip_citations(&truncate_chars(&x.answer, self.tuning.history_chars))));
        }
        messages.push(Message::user(question.to_string()));
        let mut sources: Vec<Hit> = Vec::new();
        let mut seen: HashSet<(String, usize)> = HashSet::new();
        let mut reranked = false;
        let mut calculations: Vec<Calculation> = Vec::new();

        // The library is searched before the model says a word. Left to
        // decide for itself, it skipped the search on three factual questions
        // in one benchmark run -- a bleeding wound among them -- and answered
        // from general knowledge, when the same question had cited Where
        // There Is No Doctor every time under the old retrieval-first loop.
        // Whether to consult the library is not the model's call; only how to
        // use what it finds is. The seeded search is replayed as a tool
        // exchange so the model sees it exactly as it would its own.
        //
        // What is searched for is the subject, not the sentence. "My friend
        // just went into labor, what are the steps I should take to help with
        // her delivery? I've never done this before" retrieved pages on
        // comforting a labouring woman and on when to go to a medical centre,
        // and the answer said the library had nothing on delivering a baby --
        // while Where There Is No Doctor's "The stages of labor" sat unread.
        // So the model first names what a reference book would have a chapter
        // on, and those queries are searched ahead of the question itself.
        {
            let mut queries = self.seed_queries(question, history);
            mark(&format!("rewrite -> {queries:?}"));
            let mut fetched: HashSet<String> = HashSet::new();
            let (hits, rr) = self.gather_seeded(question, &queries, self.tuning.keep, &mut fetched)?;
            mark(&format!("seeded search ({} queries): {} passages", queries.len() + 1, hits.len()));
            reranked |= rr;
            let result = self.render_search(&hits, &mut sources, &mut seen);
            queries.push(question.to_string());
            let call = ToolCall {
                id: "seed_search".into(),
                r#type: "function".into(),
                function: crate::llama::ToolCallFunction {
                    name: "search_library".into(),
                    arguments: serde_json::json!({ "query": queries.join(" | ") }).to_string(),
                },
            };
            messages.push(Message::assistant_calls(String::new(), vec![call]));
            messages.push(Message::tool_result("seed_search", result));
        }

        let mut repaired = false;
        let mut math_repaired = false;
        let mut round = 0;
        loop {
            // On the last round the tools are withheld so the model must answer.
            let last = round >= self.tuning.max_rounds;
            if round == self.tuning.max_rounds {
                messages.push(Message::user(NO_MORE_TOOLS));
            }
            let offered = if last { None } else { Some(tools.as_slice()) };
            round += 1;
            let turn = self
                .chat
                .turn(&messages, offered, self.tuning.answer_tokens, |t| on_token(Token::Text(t)))
                .context("generating the answer")?;
            mark(&format!("turn {round}: {} tool call(s), {} chars", turn.tool_calls.len(), turn.text.len()));

            if !turn.tool_calls.is_empty() {
                // Whatever it narrated on the way to the tool call is not the
                // answer, and may contain numbers it made up. Take it back.
                on_token(Token::Discard);
            }

            if turn.tool_calls.is_empty() {
                // An answer that ran into the token limit is not finished; it
                // is asked to carry on from the cut, and the reader sees the
                // continuation stream in behind the first part. If it runs
                // out again after the allowed continuations, say so rather
                // than end mid-sentence as if that were the whole answer.
                let mut text = turn.text;
                let mut limited = turn.hit_token_limit;
                let mut continuations = 0;
                while limited && continuations < self.tuning.max_continuations {
                    messages.push(Message::assistant(text.clone()));
                    messages.push(Message::user(CONTINUE));
                    let more = self
                        .chat
                        .turn(&messages, None, self.tuning.answer_tokens, |t| on_token(Token::Text(t)))
                        .context("continuing the answer")?;
                    if !text.ends_with(char::is_whitespace) && !more.text.starts_with(char::is_whitespace) {
                        text.push(' ');
                        on_token(Token::Text(" "));
                    }
                    text.push_str(&more.text);
                    limited = more.hit_token_limit;
                    continuations += 1;
                }
                if limited {
                    text.push_str(CUT_SHORT);
                    on_token(Token::Text(CUT_SHORT));
                }
                let turn = crate::llama::Turn { text, tool_calls: Vec::new(), hit_token_limit: limited };

                // A sum worked out in prose is sent back once, with the
                // tools still on offer, to be done by the calculator.
                if !math_repaired
                    && calculations.is_empty()
                    && round < self.tuning.max_rounds
                    && has_prose_arithmetic(&turn.text)
                {
                    math_repaired = true;
                    on_token(Token::Discard);
                    messages.push(Message::assistant(turn.text.clone()));
                    messages.push(Message::user(CALCULATE_FIRST));
                    continue;
                }

                let calculated = !calculations.is_empty();
                // The library is always searched now, so "did retrieval find
                // something" no longer says anything about the answer. What
                // does is whether the answer *cites* it: a passage the model
                // was handed and ignored did not ground anything. Only cited
                // sources are kept, in citation order, and the label follows.
                let cited = cited_numbers(&turn.text);
                let leaked = looks_like_leaked_tool_call(&turn.text);
                if !repaired && (leaked || (cited.is_empty() && !sources.is_empty())) {
                    // One more turn, tools withheld: cite the passages, or
                    // answer at all if what came back was a tool call as
                    // text. What was streamed so far is taken back.
                    repaired = true;
                    on_token(Token::Discard);
                    messages.push(Message::assistant(turn.text.clone()));
                    messages.push(Message::user(if leaked { NO_MORE_TOOLS } else { CITE_OR_STAND }));
                    round = self.tuning.max_rounds + 1;
                    continue;
                }
                let used: Vec<Hit> = cited
                    .iter()
                    .filter_map(|n| sources.get(n - 1).cloned())
                    .collect();
                let text = renumber_citations(&turn.text, &cited);
                let basis = match (!used.is_empty(), calculated) {
                    (true, true) => Basis::LibraryAndCalculation,
                    (true, false) => Basis::Library,
                    (false, true) => Basis::Computed,
                    (false, false) => Basis::GeneralKnowledge,
                };
                return Ok(Answer { text, sources: used, reranked, basis, calculations });
            }

            // Replay the model's request, then answer each call.
            messages.push(Message::assistant_calls(turn.text.clone(), turn.tool_calls.clone()));
            for call in &turn.tool_calls {
                let result = match call.function.name.as_str() {
                    "search_library" => {
                        let q = serde_json::from_str::<SearchArgs>(&call.function.arguments)
                            .map(|a| a.query)
                            .unwrap_or_else(|_| call.function.arguments.clone());
                        let (hits, rr) = self.gather(&q)?;
                        reranked |= rr;
                        self.render_search(&hits, &mut sources, &mut seen)
                    }
                    "calculate" => {
                        let expr = serde_json::from_str::<CalcArgs>(&call.function.arguments)
                            .map(|a| a.expression)
                            .unwrap_or_else(|_| call.function.arguments.clone());
                        match calc::evaluate(&expr) {
                            Ok(v) => {
                                let result = calc::format(v);
                                calculations.push(Calculation { expression: expr.clone(), result: result.clone() });
                                format!("{expr} = {result}. Report this figure exactly; do not convert or rescale it.")
                            }
                            Err(e) => format!("Could not evaluate `{expr}`: {e}. Rewrite it using only numbers, operators, and the listed functions."),
                        }
                    }
                    other => format!("Unknown tool {other}."),
                };
                messages.push(Message::tool_result(call.id.clone(), result));
            }
        }
    }

    /// The plain subjects behind a question, as up to three short library
    /// searches, written by the model with the earlier exchanges in view so
    /// that "this baby" resolves. On any failure the list is empty and the
    /// question itself is searched, as before.
    fn seed_queries(&self, question: &str, history: &[Exchange]) -> Vec<String> {
        let mut context = String::new();
        for x in history.iter().rev().take(self.tuning.max_history).collect::<Vec<_>>().into_iter().rev() {
            context.push_str("Earlier question: ");
            context.push_str(x.question.trim());
            context.push('\n');
        }
        context.push_str("Question: ");
        context.push_str(question.trim());
        context.push_str("\nSearches:");
        let messages = vec![Message::system(self.tuning.rewrite_prompt.clone()), Message::user(context)];
        let text = match self.chat.chat(&messages, 80) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("  (search rewrite unavailable: {e})");
                return Vec::new();
            }
        };
        parse_queries(&text, question)
    }

    /// Format search hits for the model, numbering them continuously across
    /// searches so citations stay unambiguous. A passage already shown keeps
    /// its number. The file is part of what makes it the same passage: every
    /// PDF in an archive shares the archive's book id, and every article from
    /// a native archive has page 0, so book and page alone gave page 2 of
    /// Home Canning the number of page 2 of the water guide shown before it,
    /// and a citation of it named the wrong book.
    fn render_search(
        &self,
        hits: &[Hit],
        sources: &mut Vec<Hit>,
        seen: &mut HashSet<(String, usize)>,
    ) -> String {
        if hits.is_empty() {
            return "No passages in the library match that. Try different words, or if the \
                    question is factual and nothing matches after another try, say the \
                    library does not cover it."
                .to_string();
        }
        let mut out = String::new();
        for h in hits {
            let key = (h.book.clone(), h.page);
            let n = if let Some(i) = sources.iter().position(|s| s.book == h.book && s.page == h.page && s.file == h.file) {
                i + 1
            } else {
                seen.insert(key);
                sources.push(h.clone());
                sources.len()
            };
            let who = if h.author.is_empty() { String::new() } else { format!(", {}", h.author) };
            let loc = if h.page > 0 { format!(" (page {})", h.page) } else { format!(" ({})", h.book) };
            out.push_str(&format!(
                "[{n}] {}{who}{loc}\n{}\n\n",
                h.doc_title,
                truncate_chars(&h.body, CONTEXT_CHARS)
            ));
        }
        out
    }
}

/// The source numbers an answer cites, in order of first appearance.
/// Handles [2], [4][5] and [4, 5].
pub fn cited_numbers(text: &str) -> Vec<usize> {
    let mut out: Vec<usize> = Vec::new();
    let bytes = text.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'[' {
            if let Some(end) = text[i..].find(']') {
                let inner = &text[i + 1..i + end];
                if !inner.is_empty() && inner.chars().all(|c| c.is_ascii_digit() || c == ',' || c == ' ') {
                    for tok in inner.split(',') {
                        if let Ok(n) = tok.trim().parse::<usize>() {
                            if n > 0 && !out.contains(&n) {
                                out.push(n);
                            }
                        }
                    }
                }
                i += end + 1;
                continue;
            }
        }
        i += 1;
    }
    out
}

/// Turn the rewrite's reply into searches: one per line, bullets, numbering
/// and quotes stripped, nothing empty, nothing long enough to be a sentence,
/// nothing that merely repeats the question, at most three.
fn parse_queries(text: &str, question: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for line in text.lines() {
        let mut q = line.trim();
        q = q.trim_start_matches(|c: char| c == '-' || c == '*' || c == '•' || c.is_ascii_digit() || c == '.' || c == ')' || c == ' ');
        let q = q.trim_matches(|c: char| c == '"' || c == '\'' || c == '`').trim();
        if q.is_empty() || q.ends_with(':') {
            continue;
        }
        let words = q.split_whitespace().count();
        if words > 10 || q.eq_ignore_ascii_case(question.trim()) {
            continue;
        }
        if out.iter().any(|o| o.eq_ignore_ascii_case(q)) {
            continue;
        }
        out.push(q.to_string());
        if out.len() == 3 {
            break;
        }
    }
    out
}

/// Remove [n] citation markers from an earlier answer before it is replayed
/// as history, so their numbers are not carried into a new answer whose
/// passages are numbered afresh.
fn strip_citations(text: &str) -> String {
    renumber_or_strip(text, None)
}

/// Shared walker: with `cited` it renumbers markers, without it removes them.
fn renumber_or_strip(text: &str, cited: Option<&[usize]>) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find('[') {
        out.push_str(&rest[..start]);
        let after = &rest[start..];
        match after.find(']') {
            Some(end) => {
                let inner = &after[1..end];
                let is_marker = !inner.is_empty()
                    && inner.chars().all(|c| c.is_ascii_digit() || c == ',' || c == ' ')
                    && inner.chars().any(|c| c.is_ascii_digit());
                if !is_marker {
                    out.push_str(&after[..=end]);
                } else if let Some(cited) = cited {
                    let mapped: Vec<String> = inner
                        .split(',')
                        .filter_map(|t| t.trim().parse::<usize>().ok())
                        .filter_map(|n| cited.iter().position(|c| *c == n).map(|p| (p + 1).to_string()))
                        .collect();
                    if mapped.is_empty() {
                        out.push_str(&after[..=end]);
                    } else {
                        out.push('[');
                        out.push_str(&mapped.join(", "));
                        out.push(']');
                    }
                } else {
                    // Stripped. Also eat one space left dangling before a full stop.
                    if out.ends_with(' ') && after[end + 1..].starts_with(['.', ',', ';', ':']) {
                        out.pop();
                    }
                }
                rest = &after[end + 1..];
            }
            None => {
                out.push_str(after);
                rest = "";
            }
        }
    }
    out.push_str(rest);
    out
}

/// Does the text work a sum out itself -- "4 × 2 = 8", "20 ÷ 8 = 2.5"?
/// A number, an operator, a number and an equals sign followed by a number,
/// in that order. Ranges ("5-10 minutes"), dates and "pH = 7" do not match.
fn has_prose_arithmetic(text: &str) -> bool {
    // Space out the symbols so "4×2=8" and "4 × 2 = 8" tokenise alike. An
    // 'x' counts as multiplication only between digits.
    let chars: Vec<char> = text.chars().collect();
    let mut spaced = String::with_capacity(text.len() + 32);
    for (i, &c) in chars.iter().enumerate() {
        let between_digits = i > 0
            && i + 1 < chars.len()
            && chars[i - 1].is_ascii_digit()
            && chars[i + 1].is_ascii_digit();
        if matches!(c, '×' | '÷' | '=' | '*' | '/' | '+') || ((c == 'x' || c == 'X') && between_digits) {
            spaced.push(' ');
            spaced.push(c);
            spaced.push(' ');
        } else {
            spaced.push(c);
        }
    }
    let is_num = |t: &str| {
        let t = t.trim_matches(|c: char| !c.is_ascii_digit() && c != '.').replace(',', "");
        !t.is_empty() && t.parse::<f64>().is_ok()
    };
    let is_op = |t: &str| matches!(t, "×" | "÷" | "*" | "/" | "+" | "x" | "X");
    let toks: Vec<&str> = spaced.split_whitespace().collect();
    for e in 0..toks.len() {
        if toks[e] != "=" || e + 1 >= toks.len() || !is_num(toks[e + 1]) {
            continue;
        }
        // In the eight tokens before the '=': a number, then an operator,
        // then a number, with unit words allowed between them.
        let window = &toks[e.saturating_sub(8)..e];
        let (mut num_before, mut op, mut num_after) = (false, false, false);
        for t in window {
            if is_op(t) {
                if num_before {
                    op = true;
                }
            } else if is_num(t) {
                if op {
                    num_after = true;
                } else {
                    num_before = true;
                }
            }
        }
        if num_before && op && num_after {
            return true;
        }
    }
    false
}

/// A tool call the chat template failed to parse comes back as prose:
/// `<tool_call>…`, or Qwen's `<function=search_library>` form. It is not an
/// answer and must not be shown as one.
fn looks_like_leaked_tool_call(text: &str) -> bool {
    let t = text.trim_start();
    t.starts_with("<tool_call>") || t.starts_with("<function=")
}

/// Rewrite [n] markers so the kept sources number 1..k in citation order.
/// The reader sees a source list that matches the markers, with no gaps
/// for passages the model was handed and never used.
fn renumber_citations(text: &str, cited: &[usize]) -> String {
    if cited.is_empty() {
        return text.to_string();
    }
    renumber_or_strip(text, Some(cited))
}

/// Kept for callers that want the legacy single-shot path.
#[allow(dead_code)]
fn legacy_prompt(sources: &[Hit], question: &str) -> Vec<Message> {
    let mut block = String::new();
    for (i, h) in sources.iter().enumerate() {
        block.push_str(&format!("[{}] {} (page {})\n{}\n\n", i + 1, h.doc_title, h.page, truncate_chars(&h.body, CONTEXT_CHARS)));
    }
    vec![Message::system(Tuning::default().system_prompt), Message::user(format!("Sources:\n\n{block}---\n\nQuestion: {question}"))]
}

/// The stretch of an article most likely to answer the question: a window
/// around the first occurrence of its most distinctive term, or the opening
/// if nothing matches. Long articles would otherwise contribute only their
/// lead section.
fn focus(text: &str, question: &str, chars: usize) -> String {
    let lower = text.to_lowercase();
    let terms: Vec<String> = question
        .split(|c: char| !c.is_alphanumeric())
        .filter(|w| w.len() > 3)
        .map(|w| w.to_lowercase())
        .collect();
    // Longest term first: the more specific word is the better anchor.
    let mut sorted = terms.clone();
    sorted.sort_by_key(|t| std::cmp::Reverse(t.len()));
    let anchor = sorted.iter().find_map(|t| lower.find(t.as_str())).unwrap_or(0);
    let start_char = lower[..anchor].chars().count().saturating_sub(chars / 4);
    text.chars().skip(start_char).take(chars).collect()
}

fn truncate_chars(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        s.chars().take(n).collect()
    }
}

#[allow(dead_code)]
fn describe_calls(calls: &[ToolCall]) -> String {
    calls.iter().map(|c| c.function.name.clone()).collect::<Vec<_>>().join(", ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncation_is_char_safe_on_multibyte_text() {
        let s = "évacuation immédiate";
        assert_eq!(truncate_chars(s, 5).chars().count(), 5);
        assert_eq!(truncate_chars(s, 500), s);
    }

    #[test]
    fn citations_are_read_and_renumbered_in_order_of_use() {
        let text = "Boil it [3]. Or add bleach [1][5], see also [3, 1].";
        assert_eq!(cited_numbers(text), vec![3, 1, 5]);
        assert_eq!(
            renumber_citations(text, &[3, 1, 5]),
            "Boil it [1]. Or add bleach [2][3], see also [1, 2]."
        );
        // Prose brackets that are not citations are left alone.
        assert_eq!(cited_numbers("see [note] and [2024]"), vec![2024]);
    }

    #[test]
    fn rewrite_replies_become_short_searches() {
        let q = "I'm not near a medical center how can I help deliver this baby?";
        let reply = "Searches:\n1. \"delivering a baby\"\n- stages of labor\n* stages of labor\n\nThis is a long sentence that explains itself at great length and is not a search at all\n3) cutting the umbilical cord\n4) placenta delivery\n";
        assert_eq!(
            parse_queries(reply, q),
            vec!["delivering a baby", "stages of labor", "cutting the umbilical cord"]
        );
        assert!(parse_queries(q, q).is_empty());
    }

    #[test]
    fn history_answers_lose_their_citation_numbers() {
        assert_eq!(strip_citations("Boil it [3]. Add bleach [1][5], see [3, 1]."), "Boil it. Add bleach, see.");
        assert_eq!(strip_citations("see [note] and [2024]"), "see [note] and ");
    }

    #[test]
    fn arithmetic_worked_out_in_prose_is_noticed() {
        assert!(has_prose_arithmetic("Daily use: 4 people × 2 litres per person = 8 litres per day."));
        assert!(has_prose_arithmetic("20 litres ÷ 8 litres per day = 2.5 days"));
        assert!(has_prose_arithmetic("so 55*0.25=13.75 teaspoons"));
        assert!(!has_prose_arithmetic("Wait 5-10 minutes, then check the pH = 7 reading [1]."));
        assert!(!has_prose_arithmetic("You need 2 litres per person per day [2]."));
        assert!(!has_prose_arithmetic("Between 1 and 2 = a range, not a sum"));
    }

    #[test]
    fn a_tool_call_returned_as_text_is_recognised() {
        assert!(looks_like_leaked_tool_call(
            "<tool_call>\n<function=search_library>\n<parameter=query>\nfood safety\n</parameter>\n</function>\n</tool_call>"
        ));
        assert!(looks_like_leaked_tool_call("  <function=calculate>"));
        assert!(!looks_like_leaked_tool_call("Boil the water for one minute [1]."));
    }

    #[test]
    fn the_system_prompt_keeps_its_safety_clauses() {
        let s = Tuning::default().system_prompt;
        let flat = s.replace('\n', " ");
        assert!(flat.contains("do not improvise"));
        assert!(flat.contains("Never do arithmetic or unit conversion yourself"));
        assert!(flat.contains("EXACTLY as calculate returned it"));
        assert!(flat.contains("The library has nothing on this, so from general knowledge:"));
        assert!(flat.contains("Cite each with its bracketed number"));
    }

    #[test]
    fn two_documents_in_one_archive_never_share_a_citation_number() {
        let dir = std::env::temp_dir().join(format!("museion-ask-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let index = Index::open(&dir.join("index.db")).unwrap();
        let chat = Llama::new("http://127.0.0.1:1");
        let asker = Asker { index: &index, chat: &chat, reranker: None, kiwix: None, native_books: Vec::new(), tuning: Tuning::default() };
        let hit = |title: &str, file: &str| Hit {
            doc_title: title.into(), author: String::new(), book: "zimgit-water".into(), file: file.into(),
            page: 2, snippet: String::new(), rank: 0.0, coverage: 1.0, body: "text".into(),
        };
        let mut sources = Vec::new();
        let mut seen = HashSet::new();
        let first = asker.render_search(&[hit("Water Guide", "water.pdf"), hit("Home Canning", "canning.pdf")], &mut sources, &mut seen);
        assert!(first.contains("[1] Water Guide (page 2)") && first.contains("[2] Home Canning (page 2)"), "{first}");
        // Found again by a later search, a passage keeps its number.
        let again = asker.render_search(&[hit("Home Canning", "canning.pdf")], &mut sources, &mut seen);
        assert!(again.starts_with("[2] Home Canning"), "{again}");
        assert_eq!(sources.len(), 2);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tools_are_exactly_the_two_intended() {
        let names: Vec<String> = tools().into_iter().map(|t| t.function.name).collect();
        assert_eq!(names, vec!["search_library", "calculate"]);
    }
}
