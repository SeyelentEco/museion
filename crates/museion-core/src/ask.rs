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
seems obviously useful.
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
    /// Retrieve and rerank, returning the passages an answer would be built on.
    pub fn gather(&self, question: &str) -> Result<(Vec<Hit>, bool)> {
        let candidates = self.index.search(question, RETRIEVE)?;
        if candidates.is_empty() {
            return Ok((Vec::new(), false));
        }

        let Some(reranker) = self.reranker else {
            return Ok((candidates.into_iter().take(KEEP).collect(), false));
        };

        let docs: Vec<String> = candidates
            .iter()
            .map(|h| truncate_chars(&format!("{}. {}", h.doc_title, h.body), RERANK_CHARS))
            .collect();

        match reranker.rerank(question, &docs) {
            Ok(scored) => {
                let picked = scored
                    .into_iter()
                    .take(KEEP)
                    .filter_map(|(i, _)| candidates.get(i).cloned())
                    .collect();
                Ok((picked, true))
            }
            Err(e) => {
                // Reranking is an improvement, not a dependency. Losing it
                // degrades ordering; it must not lose the answer.
                eprintln!("  (reranker unavailable, using keyword ranking: {e})");
                Ok((candidates.into_iter().take(KEEP).collect(), false))
            }
        }
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
