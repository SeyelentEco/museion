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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<Vec<ToolCall>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
}

impl Message {
    pub fn system(c: impl Into<String>) -> Self {
        Self { role: "system".into(), content: c.into(), tool_calls: None, tool_call_id: None }
    }
    pub fn user(c: impl Into<String>) -> Self {
        Self { role: "user".into(), content: c.into(), tool_calls: None, tool_call_id: None }
    }
    /// The assistant turn that asked for tools, replayed so the model sees
    /// its own request before the results.
    pub fn assistant_calls(content: String, calls: Vec<ToolCall>) -> Self {
        Self { role: "assistant".into(), content, tool_calls: Some(calls), tool_call_id: None }
    }
    /// A tool's answer to one call.
    pub fn tool_result(call_id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            role: "tool".into(),
            content: content.into(),
            tool_calls: None,
            tool_call_id: Some(call_id.into()),
        }
    }
}

/// A tool the model may call, in the OpenAI function shape llama-server speaks.
#[derive(Debug, Clone, Serialize)]
pub struct Tool {
    pub r#type: String,
    pub function: ToolFunction,
}

#[derive(Debug, Clone, Serialize)]
pub struct ToolFunction {
    pub name: String,
    pub description: String,
    pub parameters: serde_json::Value,
}

impl Tool {
    pub fn function(name: &str, description: &str, parameters: serde_json::Value) -> Self {
        Self {
            r#type: "function".into(),
            function: ToolFunction {
                name: name.into(),
                description: description.into(),
                parameters,
            },
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ToolCall {
    pub id: String,
    pub r#type: String,
    pub function: ToolCallFunction,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ToolCallFunction {
    pub name: String,
    /// JSON text, as the model wrote it.
    pub arguments: String,
}

/// What one model turn produced: streamed text, and/or tool calls.
#[derive(Debug, Default)]
pub struct Turn {
    pub text: String,
    pub tool_calls: Vec<ToolCall>,
    pub hit_token_limit: bool,
}

#[derive(Serialize)]
struct ChatRequest<'a> {
    messages: &'a [Message],
    temperature: f32,
    max_tokens: u32,
    stream: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    tools: Option<&'a [Tool]>,
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
    #[serde(default)]
    tool_calls: Vec<ToolCallDelta>,
}

/// Streamed tool calls arrive in fragments keyed by index: the name in one
/// chunk, the arguments spread across many. They are stitched back together.
#[derive(Deserialize)]
struct ToolCallDelta {
    #[serde(default)]
    index: usize,
    id: Option<String>,
    function: Option<ToolCallFunctionDelta>,
}

#[derive(Deserialize)]
struct ToolCallFunctionDelta {
    name: Option<String>,
    arguments: Option<String>,
}

/// Fold a streamed fragment into the accumulating call list.
fn absorb(calls: &mut Vec<ToolCall>, d: ToolCallDelta) {
    while calls.len() <= d.index {
        calls.push(ToolCall { r#type: "function".into(), ..Default::default() });
    }
    let c = &mut calls[d.index];
    if let Some(id) = d.id {
        if !id.is_empty() {
            c.id = id;
        }
    }
    if let Some(f) = d.function {
        if let Some(n) = f.name {
            if !n.is_empty() {
                c.function.name = n;
            }
        }
        if let Some(a) = f.arguments {
            c.function.arguments.push_str(&a);
        }
    }
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

    /// One model turn, streamed. Text fragments go to `on_token` as they
    /// arrive; tool calls are stitched from their fragments and returned.
    /// A turn may contain either, or both.
    pub fn turn(
        &self,
        messages: &[Message],
        tools: Option<&[Tool]>,
        max_tokens: u32,
        mut on_token: impl FnMut(&str),
    ) -> Result<Turn> {
        // Greedy when tools are in play. Sampling is for prose; a turn that is
        // deciding whether to compute, and how to report the result, should
        // not roll dice -- the same question returned 68 ml once and 343.75 ml
        // the next time at 0.2.
        let req = ChatRequest {
            messages,
            temperature: if tools.is_some() { 0.0 } else { 0.2 },
            max_tokens,
            stream: true,
            tools,
            chat_template_kwargs: serde_json::json!({ "enable_thinking": false }),
        };

        let res = self
            .agent
            .post(&format!("{}/v1/chat/completions", self.base))
            .send_json(&req)
            .context("chat completion request failed")?;

        let mut out = Turn::default();
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
            for c in chunk.choices {
                // Streaming puts fragments in `delta`, but some servers deliver
                // the tail in `message` on the final chunk. Accept either.
                let msg = c.delta.or(c.message);
                if let Some(m) = msg {
                    if let Some(text) = m.content.as_deref() {
                        if !text.is_empty() {
                            out.text.push_str(text);
                            on_token(text);
                        }
                    }
                    for d in m.tool_calls {
                        absorb(&mut out.tool_calls, d);
                    }
                }
                if c.finish_reason.as_deref() == Some("length") {
                    out.hit_token_limit = true;
                }
            }
        }

        // Drop any call the stream never finished naming.
        out.tool_calls.retain(|c| !c.function.name.is_empty());
        for (i, c) in out.tool_calls.iter_mut().enumerate() {
            if c.id.is_empty() {
                c.id = format!("call_{i}");
            }
        }

        if out.text.trim().is_empty() && out.tool_calls.is_empty() {
            bail!(
                "the model returned nothing. If reasoning is enabled server-side it can \
                 consume the whole token budget and return an empty message; Museion \
                 disables it per request, but a server started with a forced thinking \
                 template will override that."
            );
        }
        Ok(out)
    }

    /// Stream a plain chat completion with no tools. Returns the full text.
    pub fn chat_stream(
        &self,
        messages: &[Message],
        max_tokens: u32,
        on_token: impl FnMut(&str),
    ) -> Result<String> {
        let t = self.turn(messages, None, max_tokens, on_token)?;
        if t.hit_token_limit {
            eprintln!("\n  (answer stopped at the token limit)");
        }
        Ok(t.text)
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
