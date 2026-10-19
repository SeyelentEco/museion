"""Client for the llama-server helper.

Two roles, usually two processes: a chat model holding most of the VRAM, and
a small reranker on CPU so it does not compete for it.

Messages and tool calls are plain dicts in the OpenAI shape llama-server
speaks, e.g. {"role": "user", "content": "..."}, so what is sent is exactly
what is written here, with no translation layer in between.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field


def system(content: str) -> dict:
    return {"role": "system", "content": content}


def user(content: str) -> dict:
    return {"role": "user", "content": content}


def assistant(content: str) -> dict:
    """A plain assistant turn, replayed before a follow-up instruction."""
    return {"role": "assistant", "content": content}


def assistant_calls(content: str, calls: list[dict]) -> dict:
    """The assistant turn that asked for tools, replayed so the model sees its
    own request before the results."""
    return {"role": "assistant", "content": content, "tool_calls": calls}


def tool_result(call_id: str, content: str) -> dict:
    """A tool's answer to one call."""
    return {"role": "tool", "content": content, "tool_call_id": call_id}


def tool(name: str, description: str, parameters: dict) -> dict:
    """A tool the model may call, in the OpenAI function shape."""
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


@dataclass
class Turn:
    """What one model turn produced: streamed text, and/or tool calls. Each
    call is {"id", "type": "function", "function": {"name", "arguments"}},
    with arguments as the JSON text the model wrote."""

    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    hit_token_limit: bool = False


class Llama:
    def __init__(self, base: str, timeout: float = 600.0) -> None:
        self.base = base.rstrip("/")
        # Generous, because generating a long answer on a slow machine takes
        # minutes. Probes that only ask "are you there?" use their own short
        # timeout (model_id).
        self.timeout = timeout

    def model_id(self) -> str:
        """Model id the server reports; raises if it is not answering.

        Uses its own short timeout. A server that has bound its port but is
        still loading a 20 GB model accepts the connection and then says
        nothing, and with the ten-minute timeout the UI once sat on a spinner
        for the whole ten minutes."""
        try:
            with urllib.request.urlopen(f"{self.base}/v1/models", timeout=4) as res:
                body = json.load(res)
        except (OSError, ValueError) as e:
            raise ConnectionError(f"no llama-server answering at {self.base}: {e}") from e
        data = body.get("data") or [{}]
        return data[0].get("id", "unknown")

    def is_up(self) -> bool:
        try:
            self.model_id()
            return True
        except ConnectionError:
            return False

    def turn(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        on_token: Callable[[str], None] | None = None,
    ) -> Turn:
        """One model turn, streamed. Text fragments go to on_token as they
        arrive; tool calls are stitched from their fragments and returned. A
        turn may contain either, or both."""
        request = {
            "messages": messages,
            # Greedy when tools are in play. Sampling is for prose; a turn that
            # is deciding whether to compute, and how to report the result,
            # should not roll dice -- the same question returned 68 ml once and
            # 343.75 ml the next time at 0.2.
            "temperature": 0.0 if tools else 0.2,
            "max_tokens": max_tokens,
            "stream": True,
            # Reasoning is suppressed. It costs several times the wall-clock
            # and an order of magnitude more output tokens, and has a nasty
            # failure mode: when it consumes the whole token budget the server
            # returns HTTP 200 with null content, which looks like success and
            # delivers nothing.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools:
            request["tools"] = tools

        out = Turn()
        with self._post("/v1/chat/completions", request, "chat completion request failed") as res:
            for raw in res:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                for choice in chunk.get("choices", []):
                    # Streaming puts fragments in "delta", but some servers
                    # deliver the tail in "message" on the final chunk.
                    msg = choice.get("delta") or choice.get("message") or {}
                    text = msg.get("content") or ""
                    if text:
                        out.text += text
                        if on_token:
                            on_token(text)
                    for fragment in msg.get("tool_calls") or []:
                        absorb(out.tool_calls, fragment)
                    if choice.get("finish_reason") == "length":
                        out.hit_token_limit = True

        # Drop any call the stream never finished naming.
        out.tool_calls = [c for c in out.tool_calls if c["function"]["name"]]
        for i, call in enumerate(out.tool_calls):
            if not call["id"]:
                call["id"] = f"call_{i}"

        if not out.text.strip() and not out.tool_calls:
            raise RuntimeError(
                "the model returned nothing. If reasoning is enabled server-side it can "
                "consume the whole token budget and return an empty message; Museion "
                "disables it per request, but a server started with a forced thinking "
                "template will override that."
            )
        return out

    def chat(self, messages: list[dict], max_tokens: int = 1024) -> str:
        """A plain chat completion with no tools. Returns the full text."""
        return self.turn(messages, None, max_tokens).text

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        """Score each document against the query. Returns (index, score)
        pairs, best first.

        A reranker whose scores are all identical is worse than none, because
        it destroys the keyword ordering it was supposed to improve. That case
        is detected and reported rather than silently accepted."""
        if not documents:
            return []
        hint = (
            "rerank request failed. The reranker server must be started with "
            "--reranking (equivalently --embedding --pooling rank)"
        )
        with self._post("/v1/rerank", {"query": query, "documents": documents}, hint) as res:
            body = json.load(res)
        scored = [(r["index"], float(r["relevance_score"])) for r in body.get("results", [])]
        if len(scored) > 1 and all(abs(s - scored[0][1]) < 1e-12 for _, s in scored):
            raise RuntimeError(
                f"the reranker returned an identical score ({scored[0][1]:e}) for every "
                "passage, so it is not discriminating between them. This is the known "
                "symptom of a GGUF conversion that lost the classification head. Use "
                "ggml-org's conversion of Qwen3-Reranker rather than a community one."
            )
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    def _post(self, path: str, body: dict, failure: str):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"{failure}: HTTP {e.code}: {detail}") from e
        except OSError as e:
            raise ConnectionError(f"{failure}: {e}") from e


def absorb(calls: list[dict], fragment: dict) -> None:
    """Fold one streamed tool-call fragment into the accumulating list.
    Fragments are keyed by index: the name arrives in one chunk, the
    arguments spread across many."""
    index = fragment.get("index", 0)
    while len(calls) <= index:
        calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    call = calls[index]
    if fragment.get("id"):
        call["id"] = fragment["id"]
    function = fragment.get("function") or {}
    if function.get("name"):
        call["function"]["name"] = function["name"]
    if function.get("arguments"):
        call["function"]["arguments"] += function["arguments"]
