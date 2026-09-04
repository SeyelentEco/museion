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
    index::Hit,
    llama::{Llama, Message, Tool, ToolCall},
    Index,
};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

/// How many passages to retrieve before reranking. Generous, because keyword
/// search is good at recall and bad at ordering -- which is precisely what the
/// reranker is there to fix.
const RETRIEVE: usize = 40;

/// How many survive into the prompt per search.
const KEEP: usize = 8;

/// Characters of a page passed to the reranker. Cross-encoders have a short
/// window, and the opening of a page is where its subject is usually stated.
const RERANK_CHARS: usize = 1200;

/// Characters of a page quoted to the chat model.
const CONTEXT_CHARS: usize = 2400;

/// Tool rounds before the model must answer with what it has.
const MAX_ROUNDS: usize = 4;

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

/// Hypatia's standing instructions.
///
/// Every clause is load-bearing. A model asked to be helpful will fill a gap
/// in its sources with plausible general knowledge, and plausible general
/// knowledge about wound care or water treatment, delivered to someone with
/// no way to check it, is the failure this whole design exists to prevent.
const SYSTEM: &str = "\
You are Hypatia, the librarian of an offline reference library. The person \
asking you has no internet and may have no other source of help.

You have two tools:
- search_library: finds passages in the library. Use it for any question of \
fact, method, or safety -- what to do, how to do it, how much of something. \
Search before answering such questions, and search again with different \
words if the first search misses.
- calculate: does arithmetic and unit conversion exactly. Use it for EVERY \
calculation, including simple ones, and for scaling a quantity from a source \
to the person's situation. Never do arithmetic or unit conversion yourself, \
not even approximately. Put the whole computation in one expression, for \
example convert(0.25 * 55, tsp, ml) to scale a quarter-teaspoon-per-gallon \
dose to 55 gallons in millilitres. Then report the figure EXACTLY as \
calculate returned it, with the unit you asked for. Do not rescale, \
re-convert, or round it further in your own words.

Rules for answering:
1. Factual and practical claims must come from search_library results. Cite \
each with its bracketed number, like [2], as you go. Do not add facts from \
your own knowledge to a library answer, even if confident.
2. If the library has nothing on a factual question, say so plainly. For \
medical, drug or dose, chemical, electrical, or other safety-critical topics \
that is the complete answer: do not improvise. For general questions the \
library does not cover -- history, science background, how something works \
-- you may answer from general knowledge, but begin with the words \
\"The library has nothing on this, so from general knowledge:\" and keep it \
brief.
3. Numbers you computed come from calculate; say what you computed.
4. If sources disagree, say so and cite both.
5. Be direct and practical. Lead with what to do. Someone may be reading \
this in an emergency. Where a clinician or other professional would normally \
handle something, say so once, plainly.

Write in plain prose with Markdown where it helps. Do not mention these \
instructions or the tools by name.";

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
    /// Below this reranker score the best passage does not answer the
    /// question. The reranker scores relevant passages around 0.9 and
    /// irrelevant ones at essentially 0.0 -- measured 0.918 for boiling water
    /// against a water question, 0.000 for a knot -- so the line is easy to
    /// draw and sits well clear of both.
    const MIN_RERANK_SCORE: f64 = 0.15;

    /// Retrieve and rerank, returning the passages an answer would be built on.
    ///
    /// Whether the library covers the question at all is decided by the
    /// reranker when there is one, and by term coverage only when there is
    /// not. Term overlap is a blunt instrument: it refused a canning question
    /// because "fridge" and "nobody" do not appear anywhere in the corpus,
    /// while the reranker had the pressure-canning procedure scored at the top.
    pub fn gather(&self, question: &str) -> Result<(Vec<Hit>, bool)> {
        let (mut candidates, best_coverage) = self.index.search_ungated(question, RETRIEVE)?;
        candidates.extend(self.native_hits(question));
        if candidates.is_empty() {
            return Ok((Vec::new(), false));
        }

        let Some(reranker) = self.reranker else {
            if best_coverage + 1e-9 < crate::index::Index::MIN_TERM_COVERAGE {
                return Ok((Vec::new(), false));
            }
            return Ok((candidates.into_iter().take(KEEP).collect(), false));
        };

        let docs: Vec<String> = candidates
            .iter()
            .map(|h| truncate_chars(&format!("{}. {}", h.doc_title, h.body), RERANK_CHARS))
            .collect();

        match reranker.rerank(question, &docs) {
            Ok(scored) => {
                // The reranker's verdict on coverage: if even its best passage
                // scores as irrelevant, the library does not hold the answer.
                if scored.first().map_or(true, |(_, s)| *s < Self::MIN_RERANK_SCORE) {
                    return Ok((Vec::new(), true));
                }
                let mut picked: Vec<Hit> = scored
                    .into_iter()
                    .take(KEEP)
                    .filter(|(_, s)| *s >= Self::MIN_RERANK_SCORE)
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
                Ok((candidates.into_iter().take(KEEP).collect(), false))
            }
        }
    }

    /// Search the archives that carry their own index, through kiwix-serve,
    /// and read the matching articles. They join the local candidates and go
    /// through the same reranker, so a Wikipedia article competes on equal
    /// terms with a page of a PDF.
    fn native_hits(&self, question: &str) -> Vec<Hit> {
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

    /// Answer the question, streaming to `on_token` as it arrives.
    pub fn ask(&self, question: &str, mut on_token: impl FnMut(Token)) -> Result<Answer> {
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

        let tools = tools();
        let mut messages = vec![Message::system(SYSTEM), Message::user(question.to_string())];
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
        {
            let (hits, rr) = self.gather(question)?;
            reranked |= rr;
            let call = ToolCall {
                id: "seed_search".into(),
                r#type: "function".into(),
                function: crate::llama::ToolCallFunction {
                    name: "search_library".into(),
                    arguments: serde_json::json!({ "query": question }).to_string(),
                },
            };
            let result = self.render_search(&hits, &mut sources, &mut seen);
            messages.push(Message::assistant_calls(String::new(), vec![call]));
            messages.push(Message::tool_result("seed_search", result));
        }

        for round in 0..=MAX_ROUNDS {
            // On the last round the tools are withheld so the model must answer.
            let offered = if round < MAX_ROUNDS { Some(tools.as_slice()) } else { None };
            let turn = self
                .chat
                .turn(&messages, offered, 900, |t| on_token(Token::Text(t)))
                .context("generating the answer")?;

            if !turn.tool_calls.is_empty() {
                // Whatever it narrated on the way to the tool call is not the
                // answer, and may contain numbers it made up. Take it back.
                on_token(Token::Discard);
            }

            if turn.tool_calls.is_empty() {
                let calculated = !calculations.is_empty();
                // Searched is always true now; what matters is whether it found anything.
                let basis = match (!sources.is_empty(), calculated) {
                    (true, true) => Basis::LibraryAndCalculation,
                    (true, false) => Basis::Library,
                    (false, true) => Basis::Computed,
                    (false, false) => Basis::GeneralKnowledge,
                };
                return Ok(Answer { text: turn.text, sources, reranked, basis, calculations });
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

        unreachable!("the final round withholds tools, so it always returns")
    }

    /// Format search hits for the model, numbering them continuously across
    /// searches so citations stay unambiguous, and skipping passages already
    /// shown.
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
            let n = if let Some(i) = sources.iter().position(|s| s.book == h.book && s.page == h.page) {
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

/// Kept for callers that want the legacy single-shot path.
#[allow(dead_code)]
fn legacy_prompt(sources: &[Hit], question: &str) -> Vec<Message> {
    let mut block = String::new();
    for (i, h) in sources.iter().enumerate() {
        block.push_str(&format!("[{}] {} (page {})\n{}\n\n", i + 1, h.doc_title, h.page, truncate_chars(&h.body, CONTEXT_CHARS)));
    }
    vec![Message::system(SYSTEM), Message::user(format!("Sources:\n\n{block}---\n\nQuestion: {question}"))]
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
    fn the_system_prompt_keeps_its_safety_clauses() {
        assert!(SYSTEM.contains("do not improvise"));
        assert!(SYSTEM.contains("Never do arithmetic or unit conversion yourself"));
        assert!(SYSTEM.contains("EXACTLY as \\\ncalculate returned it") || SYSTEM.contains("EXACTLY as calculate returned it"));
        assert!(SYSTEM.contains("The library has nothing on this, so from general knowledge:"));
    }

    #[test]
    fn tools_are_exactly_the_two_intended() {
        let names: Vec<String> = tools().into_iter().map(|t| t.function.name).collect();
        assert_eq!(names, vec!["search_library", "calculate"]);
    }
}
