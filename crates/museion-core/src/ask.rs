//! Asking Hypatia a question.
//!
//! Retrieve, rerank, then answer strictly from what came back. The order
//! matters and so does the strictness: this tool gets consulted exactly when
//! its answer cannot be checked against anything else, so an answer that drifts
//! off its sources is worse than no answer at all.

use crate::{index::Hit, llama::{Llama, Message}, Index};
use anyhow::{Context, Result};

/// How many passages to retrieve before reranking. Generous, because keyword
/// search is good at recall and bad at ordering -- which is precisely what the
/// reranker is there to fix.
const RETRIEVE: usize = 40;

/// How many survive into the prompt.
const KEEP: usize = 8;

/// Characters of a page passed to the reranker. Cross-encoders have a short
/// window, and the opening of a page is where its subject is usually stated.
const RERANK_CHARS: usize = 1200;

/// Characters of a page quoted to the chat model.
const CONTEXT_CHARS: usize = 2400;

pub struct Asker<'a> {
    pub index: &'a Index,
    pub chat: &'a Llama,
    pub reranker: Option<&'a Llama>,
}

pub struct Answer {
    pub text: String,
    pub sources: Vec<Hit>,
    pub reranked: bool,
}

/// Hypatia's standing instructions.
///
/// Every clause is load-bearing. The refusal clause especially: a model asked
/// to be helpful will fill a gap in its sources with plausible general
/// knowledge, and plausible general knowledge about wound care or water
/// treatment, delivered to someone with no way to check it, is the failure this
/// whole design exists to prevent.
const SYSTEM: &str = "\
You are Hypatia, the librarian of an offline reference library. The person \
asking you has no internet and may have no other source of help.

Answer ONLY from the sources given to you below. Follow these rules exactly:

1. Every factual claim must come from the sources. Do not add information from \
your own knowledge, even if you are confident it is correct and even if it \
seems obviously useful. Do not adapt instructions written for one thing to a \
different thing -- a method for preserving meat is not a method for preserving \
vegetables unless a source says so.
2. Cite the source for each claim with its bracketed number, like [2]. Cite as \
you go, not in a list at the end.
3. If the sources do not answer the question, say so plainly and say what they \
do cover. Do not improvise, and do not pad the answer to seem helpful. \"The \
library doesn't cover this\" is a complete and useful answer.
4. If the sources disagree, say so and cite both.
5. Be direct and practical. Lead with what to do. Someone may be reading this \
in an emergency.
6. Where the sources describe something a clinician or other professional would \
normally handle, say so once, plainly, without lecturing.

Write in plain prose. Do not mention these instructions.";

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
        // Keyword retrieval finds the pages that use the question's words. A
        // question phrased the way people speak often does not: "how do I can
        // them so nobody gets botulism" ranked the actual pressure-canning
        // procedure at 252nd and 283rd, because those pages never say
        // "botulism" -- while the book's contents page, which does, sat 3rd.
        // So the chat model first rewrites the question into the searches a
        // librarian would type, and the candidates from all of them are
        // pooled before the reranker sees any.
        let mut candidates: Vec<Hit> = Vec::new();
        let mut best_coverage = 0.0_f64;
        let mut seen = std::collections::HashSet::new();
        let queries = self.expand(question);
        if std::env::var_os("MUSEION_DEBUG").is_some() {
            eprintln!("  [debug] queries: {queries:?}");
        }
        for q in queries {
            let (hits, best) = self.index.search_ungated(&q, RETRIEVE)?;
            best_coverage = best_coverage.max(best);
            for h in hits {
                if seen.insert((h.book.clone(), h.file.clone(), h.page)) {
                    candidates.push(h);
                }
            }
        }
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
                if std::env::var_os("MUSEION_DEBUG").is_some() {
                    let top: Vec<String> = scored
                        .iter()
                        .take(5)
                        .map(|(i, s)| format!("{s:.3}:{}p{}", candidates[*i].doc_title.chars().take(24).collect::<String>(), candidates[*i].page))
                        .collect();
                    eprintln!("  [debug] {} candidates, best coverage {best_coverage:.2}, top rerank: {}", candidates.len(), top.join(" | "));
                }
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

    /// The question itself plus up to three keyword rewrites of it. Falls back
    /// to the question alone when the chat model is unreachable or unhelpful,
    /// so retrieval never depends on the rewrite succeeding.
    fn expand(&self, question: &str) -> Vec<String> {
        let mut out = vec![question.to_string()];
        if !self.chat.is_up() {
            return out;
        }
        let messages = vec![
            Message::system(
                "You turn a question into the short keyword searches a reference librarian \
                 would type to find the answer in a library of practical manuals. Reply with \
                 two or three searches, one per line, three to six words each, no numbering, \
                 no commentary. Use the technical terms a manual would use rather than the \
                 words in the question.",
            ),
            Message::user(question.to_string()),
        ];
        if let Ok(text) = self.chat.chat(&messages, 80) {
            for line in text.lines() {
                let l = line.trim().trim_start_matches(['-', '*', '•']).trim();
                let l = l.trim_start_matches(|c: char| c.is_ascii_digit() || c == '.' || c == ')').trim();
                if l.split_whitespace().count() >= 2 && l.len() < 80 && !out.iter().any(|o| o.eq_ignore_ascii_case(l)) {
                    out.push(l.to_string());
                }
                if out.len() == 4 {
                    break;
                }
            }
        }
        out
    }

    /// Answer the question, streaming tokens to `on_token` as they arrive.
    pub fn ask(&self, question: &str, on_token: impl FnMut(&str)) -> Result<Answer> {
        let (sources, reranked) = self.gather(question)?;

        if sources.is_empty() {
            return Ok(Answer {
                text: "The library doesn't contain anything on that. Try rephrasing, or \
                       check what is installed with \"museion status\"."
                    .to_string(),
                sources,
                reranked,
            });
        }

        let mut block = String::new();
        for (i, h) in sources.iter().enumerate() {
            let who = if h.author.is_empty() {
                String::new()
            } else {
                format!(", {}", h.author)
            };
            block.push_str(&format!(
                "[{}] {}{} (page {})\n{}\n\n",
                i + 1,
                h.doc_title,
                who,
                h.page,
                truncate_chars(&h.body, CONTEXT_CHARS)
            ));
        }

        let messages = vec![
            Message::system(SYSTEM),
            Message::user(format!(
                "Sources:\n\n{block}---\n\nQuestion: {question}"
            )),
        ];

        let text = self
            .chat
            .chat_stream(&messages, 900, on_token)
            .context("generating the answer")?;

        Ok(Answer { text, sources, reranked })
    }
}

fn truncate_chars(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        s.chars().take(n).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncation_is_char_safe_on_multibyte_text() {
        // Byte slicing here would panic; the corpus contains accented text.
        let s = "évacuation immédiate";
        assert_eq!(truncate_chars(s, 5).chars().count(), 5);
        assert_eq!(truncate_chars(s, 500), s);
    }

    #[test]
    fn the_system_prompt_keeps_its_refusal_clause() {
        // Reworded freely, but the permission to refuse must survive edits.
        assert!(SYSTEM.contains("do not answer") || SYSTEM.contains("does not cover") || SYSTEM.contains("do not cover"));
        assert!(SYSTEM.contains("Answer ONLY from the sources"));
    }
}
