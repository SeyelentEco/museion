# Hardware and model selection

Museion assumes nothing about your machine. It detects what you have at setup and picks a
model that fits, and you can always override the choice.

Check what it sees on your machine with:

```bash
python -m museion hardware
```

On the machine this was developed against that reports an RTX 3060, 12,288 MB of VRAM,
65,276 MB of system RAM, and the Capable tier.

Detection, tiering, and the offload fit are implemented — "Fit model to this machine"
under Settings, or `python -m museion fit --model <gguf>` from the command line. Each tier
has a model manifest under `models/` with an upstream checksum. The set-up screen picks
the model for the detected tier and fetches it; by hand, `python -m museion hardware`
names the tier and this fetches its model like any other content:

```bash
python -m museion fetch qwen3.6-35b-a3b-q4 --dest /path/to/models
```

## Choosing a model

The set-up wizard's **Model** step, and **Settings › Models › Help me choose a model**,
show this for the computer you are sitting at. In short:

| Model | Download | Needs | Can | Cannot |
|---|---|---|---|---|
| Qwen3 4B | 2.3 GB | Any computer, no graphics card needed | Answer from the library with citations; the librarian and ration scaler agents | Run the other agents; change Museion's code with Edit Museion; reason as well as the big ones |
| Qwen3.6 35B-A3B Q4 (recommended from 8 GB cards up) | 21 GB | Graphics card with 8 GB + 24 GB system memory, or a Mac with 32 GB | Everything: every agent, 28 of 29 on the benchmark, edits Museion's code offline | — |
| Gemma 4 26B-A4B Q4 | 16 GB | The same | Every agent; tied Qwen on the benchmark | Match Qwen's speed on a 12 GB card; not yet tried with Edit Museion |
| Qwen3.6 35B-A3B Q8 | 34 GB | Graphics card with 32 GB, or a Mac with 64 GB | Everything, slightly better | Run well on less |

Two pieces of advice the chooser gives:

- **Take the recommended one.** It is the largest model that runs well on the computer
  as measured, not the largest that loads.
- **Keep the small model as a spare.** If the graphics card fails, or the library moves to
  an older computer, the 4B still answers on the processor alone. It is 2.3 GB, and it
  cannot be fetched once the internet is gone.

The words about each model are in its file under `models/` (`summary`, `good_for`,
`limits`, `speed`), so a new measurement is a one-line edit. Whether a model fits, which
agents it runs and whether there is room for it are worked out in
`museion/model_choice.py`. Agents are offered by the model chosen, not only by the
computer: the 4B on a 12 GB card gets the 4B's agents.

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
| 3 | Apple Silicon: 32 GB+ unified for the 35B at Q4, 64 GB+ for Q8; less gets tier 0 | Larger MoE |

Detection, with nothing to install on any of the three systems:

| System | Graphics card | System memory |
|---|---|---|
| Windows | `nvidia-smi` for NVIDIA (CUDA); otherwise the display adapters in the registry, largest first, for AMD and Intel (Vulkan) | `GlobalMemoryStatusEx` |
| macOS | Apple Silicon is Metal on its unified memory (`sysctl hw.memsize`); an Intel Mac runs on the processor | `sysctl hw.memsize` |
| Linux | `nvidia-smi` for NVIDIA, otherwise the AMD driver's own figure in `/sys/class/drm/card*/device/mem_info_vram_total`. Either runs the Vulkan build, since llama.cpp publishes no CUDA build for Linux, and counts only when the system can load Vulkan (`libvulkan.so.1`). Without it, and for Intel graphics, Linux gets the CPU build | `/proc/meminfo` |

`python -m museion hardware` prints what was found, and which models fit. Windows on an
RTX 3060 and Linux with no graphics card have been run for real; the macOS and Linux
graphics-card paths are covered by tests that stand in for the system's answers, not yet
by a real machine.

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

**Check the split against `llama-bench`, though.** The fit tool measures whether a split
*loads*; on Windows, CUDA will quietly page VRAM it does not have into system memory over
PCIe, and a split that loads that way runs at a fraction of the speed. Gemma 4 26B-A4B on
an RTX 3060 showed it plainly (September 2026, `-p 512 -n 64`, prompt / generation tok/s):

| experts on the CPU from block | prompt | generation |
|---|---|---|
| 10 | 491 | 37 |
| 15 (what the fit chose) | 160 | 32 |
| 20 | 68 | 24 |
| all experts on CPU | 395 | 32 |

More on the GPU was slower every step, and in the app the fit's split read prompts at 77
tok/s. Qwen3.6 35B-A3B with its fitted split does 368 / 49 on the same card. When a model
seems slow, run `llama-bench` over two or three splits before blaming the model.

## Gemma 4 26B-A4B, measured against Qwen

Same library, same 29 benchmark cases, same card. Both scored 29/29: both refused the
drug dose, both labelled general knowledge, both put every sum through the calculator.
Gemma cited slightly more and wrote slightly less. Qwen finished in 1,081 s, Gemma in
2,763 s with the fit's split; with the block-10 split above Gemma would be closer to
Qwen's prompt speed and about three-quarters of its generation speed (3.8B active
parameters against 3B). Qwen stays the tier's default; Gemma is in the catalogue as the
alternative, and the Evals tab compares them on any library.

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
