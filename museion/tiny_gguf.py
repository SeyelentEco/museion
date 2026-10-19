"""A tiny GGUF model, written from scratch, for testing the plumbing.

It answers in printable characters and nothing else, so llama-server can be
started, asked something and stopped in a second or two without downloading
a real model. It knows nothing: every weight is zero except the two that make
it print letters. Used by the tests and by `museion doctor --tiny`.

The GGUF format is a header, typed key/value metadata, a table of tensors and
then the tensor data, each piece aligned to 32 bytes:
https://github.com/ggml-org/ggml/blob/master/docs/gguf.md
"""

from __future__ import annotations

import struct
from pathlib import Path

# Metadata value types, from the GGUF spec.
_UINT32, _INT32, _FLOAT32, _STRING, _ARRAY = 4, 5, 6, 8, 9
_ALIGN = 32

# Hyperparameters: as small as llama.cpp will load.
N_EMBD = 64
N_HEAD = 4
N_FF = 128
N_CTX = 512

CHAT_TEMPLATE = (
    "{% for m in messages %}<|im_start|>{{ m['role'] }}\n{{ m['content'] }}<|im_end|>\n"
    "{% endfor %}{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
)


def vocabulary() -> tuple[list[str], list[int]]:
    """A sentencepiece-style vocabulary: three control tokens, the printable
    ASCII characters, and the 256 byte-fallback tokens that let any text be
    tokenized. Returns the tokens and their types (1 normal, 2 unknown,
    3 control, 6 byte)."""
    tokens = ["<unk>", "<s>", "</s>"]
    types = [2, 3, 3]
    for code in range(0x20, 0x7F):
        tokens.append("▁" if code == 0x20 else chr(code))
        types.append(1)
    for byte in range(256):
        tokens.append(f"<0x{byte:02X}>")
        types.append(6)
    return tokens, types


def write(path: Path) -> Path:
    tokens, types = vocabulary()
    n_vocab = len(tokens)

    # The trick that makes it print letters: dimension 0 of every token's
    # embedding is 1 and nothing else in the network is non-zero, so the
    # final hidden state is always (1, 0, 0, ...). The output weights then
    # give every printable token a high score on that dimension and every
    # other token a very low one, so control and byte tokens -- including
    # end-of-text -- are never chosen and it writes until max_tokens.
    embd = [0.0] * (N_EMBD * n_vocab)
    for t in range(n_vocab):
        embd[t * N_EMBD] = 1.0
    output = [0.0] * (N_EMBD * n_vocab)
    for t, kind in enumerate(types):
        output[t * N_EMBD] = 1.0 if kind == 1 else -10.0
    ones = [1.0] * N_EMBD

    # (name, dims with the fastest-varying first, as ggml counts them, values)
    tensors: list[tuple[str, list[int], list[float] | None]] = [
        ("token_embd.weight", [N_EMBD, n_vocab], embd),
        ("output_norm.weight", [N_EMBD], ones),
        ("output.weight", [N_EMBD, n_vocab], output),
        ("blk.0.attn_norm.weight", [N_EMBD], ones),
        ("blk.0.attn_q.weight", [N_EMBD, N_EMBD], None),
        ("blk.0.attn_k.weight", [N_EMBD, N_EMBD], None),
        ("blk.0.attn_v.weight", [N_EMBD, N_EMBD], None),
        ("blk.0.attn_output.weight", [N_EMBD, N_EMBD], None),
        ("blk.0.ffn_norm.weight", [N_EMBD], ones),
        ("blk.0.ffn_gate.weight", [N_EMBD, N_FF], None),
        ("blk.0.ffn_up.weight", [N_EMBD, N_FF], None),
        ("blk.0.ffn_down.weight", [N_FF, N_EMBD], None),
    ]

    metadata = [
        ("general.architecture", _STRING, "llama"),
        ("general.name", _STRING, "museion-tiny-test"),
        ("llama.context_length", _UINT32, N_CTX),
        ("llama.embedding_length", _UINT32, N_EMBD),
        ("llama.block_count", _UINT32, 1),
        ("llama.feed_forward_length", _UINT32, N_FF),
        ("llama.attention.head_count", _UINT32, N_HEAD),
        ("llama.attention.head_count_kv", _UINT32, N_HEAD),
        ("llama.rope.dimension_count", _UINT32, N_EMBD // N_HEAD),
        ("llama.attention.layer_norm_rms_epsilon", _FLOAT32, 1e-5),
        ("tokenizer.ggml.model", _STRING, "llama"),
        ("tokenizer.ggml.tokens", (_ARRAY, _STRING), tokens),
        ("tokenizer.ggml.scores", (_ARRAY, _FLOAT32), [0.0] * n_vocab),
        ("tokenizer.ggml.token_type", (_ARRAY, _INT32), types),
        ("tokenizer.ggml.unknown_token_id", _UINT32, 0),
        ("tokenizer.ggml.bos_token_id", _UINT32, 1),
        ("tokenizer.ggml.eos_token_id", _UINT32, 2),
        ("tokenizer.chat_template", _STRING, CHAT_TEMPLATE),
    ]

    out = bytearray(b"GGUF")
    out += struct.pack("<IQQ", 3, len(tensors), len(metadata))
    for key, kind, value in metadata:
        out += _string(key)
        out += _value(kind, value)

    offset = 0
    sizes = []
    for name, dims, _ in tensors:
        size = 4 * _product(dims)  # float32
        out += _string(name)
        out += struct.pack("<I", len(dims))
        out += struct.pack(f"<{len(dims)}Q", *dims)
        out += struct.pack("<IQ", 0, offset)  # type 0 is F32
        sizes.append(size)
        offset += _padded(size)

    out += bytes(_padded(len(out)) - len(out))
    for (_, dims, values), size in zip(tensors, sizes):
        if values is None:
            out += bytes(size)
        else:
            out += struct.pack(f"<{len(values)}f", *values)
        out += bytes(_padded(size) - size)

    path = Path(path)
    path.write_bytes(bytes(out))
    return path


def _string(s: str) -> bytes:
    data = s.encode("utf-8")
    return struct.pack("<Q", len(data)) + data


def _value(kind: int | tuple[int, int], value) -> bytes:
    if isinstance(kind, tuple):
        _, item = kind
        body = b"".join(_scalar(item, v) for v in value)
        return struct.pack("<IIQ", _ARRAY, item, len(value)) + body
    return struct.pack("<I", kind) + _scalar(kind, value)


def _scalar(kind: int, value) -> bytes:
    if kind == _STRING:
        return _string(value)
    if kind == _UINT32:
        return struct.pack("<I", value)
    if kind == _INT32:
        return struct.pack("<i", value)
    if kind == _FLOAT32:
        return struct.pack("<f", value)
    raise ValueError(f"unsupported GGUF value type {kind}")


def _product(dims: list[int]) -> int:
    n = 1
    for d in dims:
        n *= d
    return n


def _padded(n: int) -> int:
    return (n + _ALIGN - 1) // _ALIGN * _ALIGN
