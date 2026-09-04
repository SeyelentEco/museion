//! Client for the llama-server sidecar.
//!
//! Two roles, usually two processes: a chat model holding most of the VRAM,
//! and a small reranker on CPU so it does not compete for it.

use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader};
use std::time::Duration;

pub struct Llama {
    base: String,
    agent: ureq::Agent,
}

#[derive(Debug, Clone, Serialize)]
pub struct Message {
    pub role: String,
    pub content: String,
}

impl Message {
    pub fn system(c: impl Into<String>) -> Self {
        Self { role: "system".into(), content: c.into() }
    }
    pub fn user(c: impl Into<String>) -> Self {
        Self { role: "user".into(), content: c.into() }
    }
}

#[derive(Serialize)]
struct ChatRequest<'a> {
    messages: &'a [Message],
    temperature: f32,
    max_tokens: u32,
    stream: bool,
    /// Reasoning is suppressed by default. It costs several times the
    /// wall-clock and an order of magnitude more output tokens, and has a nasty
    /// failure mode: when it consumes the whole token budget the server returns
    /// HTTP 200 with null content, which looks like success and delivers
    /// nothing.
    chat_template_kwargs: serde_json::Value,
}

#[derive(Deserialize)]
struct ChatResponse {
    choices: Vec<Choice>,
}

#[derive(Deserialize)]
struct Choice {
    message: Option<ResponseMessage>,
    delta: Option<ResponseMessage>,
    finish_reason: Option<String>,
}

#[derive(Deserialize)]
struct ResponseMessage {
    content: Option<String>,
}

#[derive(Serialize)]
struct RerankRequest<'a> {
    query: &'a str,
    documents: &'a [String],
}

#[derive(Deserialize)]
struct RerankResponse {
    results: Vec<RerankResult>,
}

#[derive(Deserialize)]
struct RerankResult {
    index: usize,
    relevance_score: f64,
}

impl Llama {
    pub fn new(base: &str) -> Self {
        Self {
            base: base.trim_end_matches('/').to_string(),
            agent: ureq::AgentBuilder::new()
                .timeout_connect(Duration::from_secs(5))
                .timeout(Duration::from_secs(600))
                .build(),
        }
    }

    /// Model id the server reports, or an error if it is not answering.
    ///
    /// Uses its own short timeout. The main agent waits up to ten minutes,
    /// which is right for generating an answer and wrong for asking "are you
    /// there?" -- a server that has bound its port but is still loading a
    /// 20 GB model accepts the connection and then says nothing, and the UI
    /// sat on a spinner for the whole ten minutes.
    pub fn model_id(&self) -> Result<String> {
        let probe = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(2))
            .timeout(Duration::from_secs(4))
            .build();
        let body: serde_json::Value = probe
            .get(&format!("{}/v1/models", self.base))
            .call()
            .with_context(|| format!("no llama-server answering at {}", self.base))?
            .into_json()?;
        Ok(body["data"][0]["id"]
            .as_str()
            .unwrap_or("unknown")
            .to_string())
    }

    pub fn is_up(&self) -> bool {
        self.model_id().is_ok()
    }

    /// Stream a chat completion, handing each token fragment to `on_token`.
    /// Returns the full text.
    pub fn chat_stream(
        &self,
        messages: &[Message],
        max_tokens: u32,
        mut on_token: impl FnMut(&str),
    ) -> Result<String> {
        let req = ChatRequest {
            messages,
            temperature: 0.2,
            max_tokens,
            stream: true,
            chat_template_kwargs: serde_json::json!({ "enable_thinking": false }),
        };

        let res = self
            .agent
            .post(&format!("{}/v1/chat/completions", self.base))
            .send_json(&req)
            .context("chat completion request failed")?;

        let mut full = String::new();
        let reader = BufReader::new(res.into_reader());
        for line in reader.lines() {
            let line = line?;
            let Some(data) = line.strip_prefix("data: ") else {
                continue;
            };
            if data.trim() == "[DONE]" {
                break;
            }
            let Ok(chunk) = serde_json::from_str::<ChatResponse>(data) else {
                continue;
            };
            for c in &chunk.choices {
                // Streaming puts fragments in `delta`, but some servers deliver
                // the tail in `message` on the final chunk. Accept either.
                let text = c
                    .delta
                    .as_ref()
                    .and_then(|d| d.content.as_deref())
                    .or_else(|| c.message.as_ref().and_then(|m| m.content.as_deref()));
                if let Some(text) = text {
                    full.push_str(text);
                    on_token(text);
                }
                if c.finish_reason.as_deref() == Some("length") {
                    eprintln!("\n  (answer stopped at the token limit)");
                }
            }
        }

        if full.trim().is_empty() {
            bail!(
                "the model returned nothing. If reasoning is enabled server-side it can \
                 consume the whole token budget and return an empty message; Museion \
                 disables it per request, but a server started with a forced thinking \
                 template will override that."
            );
        }
        Ok(full)
    }

    pub fn chat(&self, messages: &[Message], max_tokens: u32) -> Result<String> {
        self.chat_stream(messages, max_tokens, |_| {})
    }

    /// Score each document against the query. Returns (index, score) sorted
    /// best-first.
    ///
    /// A reranker whose scores are all identical is worse than none, because it
    /// destroys the BM25 ordering it was supposed to improve. That case is
    /// detected and reported rather than silently accepted.
    pub fn rerank(&self, query: &str, documents: &[String]) -> Result<Vec<(usize, f64)>> {
        if documents.is_empty() {
            return Ok(Vec::new());
        }
        let res = self
            .agent
            .post(&format!("{}/v1/rerank", self.base))
            .send_json(&RerankRequest { query, documents })
            .map_err(|e| {
                anyhow!(
                    "rerank request failed: {e}. The reranker server must be started with \
                     --reranking (equivalently --embedding --pooling rank)."
                )
            })?;

        let body: RerankResponse = res.into_json().context("parsing rerank response")?;
        let mut scored: Vec<(usize, f64)> = body
            .results
            .into_iter()
            .map(|r| (r.index, r.relevance_score))
            .collect();

        if scored.len() > 1 {
            let first = scored[0].1;
            if scored.iter().all(|(_, s)| (s - first).abs() < 1e-12) {
                bail!(
                    "the reranker returned an identical score ({first:e}) for every passage, \
                     so it is not discriminating between them. This is the known symptom of a \
                     GGUF conversion that lost the classification head. Use ggml-org's \
                     conversion of Qwen3-Reranker rather than a community one."
                );
            }
        }

        scored.sort_by(|a, b| b.1.total_cmp(&a.1));
        Ok(scored)
    }
}
