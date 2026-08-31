# Hardware and model selection

Museion assumes nothing about your machine. It detects what you have at setup and picks a
model that fits, and you can always override the choice.

Check what it sees on your machine with:

```bash
museion hardware
```

On the machine this was developed against that reports an RTX 3060, 12,288 MB of VRAM,
65,276 MB of system RAM, and the Capable tier.

Detection and tiering are implemented. Automatically *downloading* the chosen model, and
running `llama-fit-params` to compute the offload split, are still to come — for now the
model paths are set by hand on the Library tab.

## The counterintuitive part

The instinct on a mid-range GPU is to pick the largest dense model that fits in VRAM. That
is usually the wrong call.

A **sparse mixture-of-experts** model with its expert tensors offloaded to system RAM beats
a dense model that merely fits. A 35B-parameter MoE with 256 experts fires only about 8 of
them per token — roughly 3B active parameters — so per-token compute resembles a small
model while quality resembles a large one. Attention and the KV cache stay on the GPU;
expert weights live in system RAM and are pulled as needed. The dense model has to move
every one of its parameters for every token.

Measured on one machine, same llama.cpp build, same KV settings, RTX 3060 12 GB:

| Model | Type | Speed |
|---|---|---|
| 35B-A3B | sparse MoE, experts offloaded | **43.6 tok/s** |
| 12B | dense, fully resident in VRAM | 30.6 tok/s |

The larger model is meaningfully faster *and* better. This is the single most important
technique for a 12 GB card, and it's why the tiers below reach for MoE architectures rather
than the biggest dense model that fits.

## Tiers

| Tier | Hardware | Model |
|---|---|---|
| 0 | CPU only, or under 6 GB VRAM | ~4B dense, Q4_K_M |
| 1 | 8–12 GB VRAM | ~30B-A3B-class MoE, Q4_K_M, experts offloaded |
| 2 | 16–24 GB VRAM | Same MoE, more layers resident, larger context |
| 3 | Apple Silicon, 32 GB+ unified | Larger MoE |

Detection: `nvidia-smi` on Windows, falling back to WMI/DXGI for AMD and Intel (Vulkan
build), else CPU. On macOS, architecture plus `sysctl hw.memsize` selects the Metal build
and the tier.

## Offload splits are computed, not guessed

Recent llama.cpp ships `llama-fit-params`, which works out the split for your actual card
rather than relying on a table someone wrote for different hardware:

```bash
llama-fit-params -m <model.gguf> -c 32768 -fa on -ctk q8_0 -ctv q8_0
```

It prints ready-to-use `-ngl` and `-ot` arguments. Museion runs this during setup and
stores the result as a launch profile. Two people with the same nominal GPU can have
different amounts free — a second monitor or a browser changes the answer — so computing it
beats assuming.

## Settings that are not negotiable

**KV cache at `q8_0`, not `q4_0`.** llama.cpp's own function-calling documentation warns
that aggressive KV quantization substantially degrades tool calling. Museion's retrieval
loop is built on tool calls, so `q4_0` breaks the app in a way that looks like the model
being stupid rather than a misconfiguration. `q4_0` is fine for plain chat; this is not
plain chat.

**Q4-tier weights, not IQ3.** Sub-4-bit quantisations emit malformed tool calls even with a
correct chat template.

**`--flash-attn on`** — required for quantised KV to perform.

**`--parallel 1`** — gives a single conversation the whole context window rather than
splitting it across slots. Museion is a single-user desktop app.

## The reranker

A 0.6B cross-encoder reranks retrieval candidates. It runs **on CPU by default**, so it
doesn't compete with the chat model for VRAM — 0.6B over 50 candidates costs a few seconds,
which is worth far more than the recall it buys back. On cards with headroom it can be
moved to the GPU.

## Shutting down cleanly

`llama-server` is typically launched via a wrapper script, which makes the server a
*grandchild* process. Stopping the wrapper leaves the server running and still holding
every gigabyte of VRAM it had. Museion tracks and kills the server process itself.

If you ever suspect a stray server, check with `nvidia-smi` on Windows or Activity Monitor
on macOS.
