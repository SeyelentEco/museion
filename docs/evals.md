# The benchmark

A fixed set of questions people would put to this library, scored by machine, so that a
model or a setting can be judged against the last one rather than against a feeling.

Run it from the Evals tab, or:

```bash
museion eval --db /path/to/library/museion-index.db --label "Qwen3.6-35B Q4, reranker on"
museion eval --db ... --only calculate        # a capability
museion eval --db ... --only month            # a horizon
museion eval --db ... --only bleach-drum      # one case
```

**Comparing models.** The Evals tab has a *Chat model to test* picker: every `.gguf` in
your models folder, or any other file via *Choose…*. Picking one that isn't the configured
model swaps it in for the run — it is stopped, the candidate is fitted to the GPU with
`llama-fit-params`, loaded, benchmarked — and the configured model is put back when the
run ends, whatever happened. The run records the model's id, so two runs with two models
compare directly in the table. From the command line, point `llama-server` at the other
model yourself and run `museion eval --label "…"`.

Each run is saved under `<library>/evals/run-<timestamp>.json` with the model id, whether
the reranker was present, every case's answer, sources, calculations, timing, and the
verdict of every check. The Evals tab lists runs, shows pass rates by capability and by
horizon, compares any two side by side per case, and opens any answer.

## What it measures

Cases live in [`evals/benchmark.yaml`](../evals/benchmark.yaml) and are compiled into the
app. Each carries two tags.

**Capability** — what the case tests:

| tag | means |
|---|---|
| `retrieve_pdf` | find it in the indexed PDF collections (the Go Bag) |
| `retrieve_article` | find it in an archive with its own index (Wikipedia, Appropedia) |
| `calculate` | get a number right, through the calculator |
| `reason` | combine a fact from the library with the person's own numbers |
| `plan` | produce an ordered, multi-step, multi-source answer |
| `refuse` | say the library does not cover it, or label general knowledge honestly |

**Horizon** — when after a disaster the question comes up:

| tag | the period, and what it asks for |
|---|---|
| `month` | the first weeks: water, wounds, sanitation, fallout, hypothermia |
| `six_months` | living on stores: preserving food, infection without antibiotics, fuel |
| `year` | producing again: soap and lye, charcoal, delivering a baby, seed |
| `five_years` | rebuilding: steel, distillation, the chemistry underneath |
| `any` | arithmetic, conversions, and refusals — no particular time |

## How a case is scored

Checks are declared per case and every declared check must pass. They read what the
system already reports rather than judging prose:

- `expect_any` / `expect_all` — terms in the answer or its cited sources.
- `expect_basis` — the basis label: `library`, `library_and_calculation`, `computed`,
  or `general_knowledge`. Judged from which sources the answer actually cites and which
  tools actually ran, so a model cannot pass a refusal case by *claiming* a source, and
  a pure calculation is not labelled *from your library* because the search happened to
  turn something up.
- `expect_number` — a value with tolerance, looked for in the **calculator's log first**
  and the prose second. The bleach-drum case also declares `must_not_contain: ["343"]`,
  because a model once produced that figure by relabelling a correct result and
  multiplying again; it is the failure this whole design exists to catch.
- `min_sources`, `min_steps`, `source_from` — for planning and retrieval cases. Sources
  are the ones the answer cites, not everything retrieval returned.

A failed check records why: *"none of [bowline, loop] found"*, *"wanted
library_and_calculation, got computed"*, *"3 steps, wanted at least 4"*.

## Reading a run honestly

- A failure is a fact about the **system** — model, retrieval, prompt, index — not
  necessarily the model. `retrieve_pdf` failing on a machine without the Go Bag is a
  library problem.
- The `refuse` cases are the ones to watch when trying a new model. A model that scores
  higher on retrieval by answering from its own knowledge will fail them, and that is the
  worse trade.
- Cases with `expect_number` at zero tolerance are deterministic once the calculator is
  used; if they vary between runs, the model is doing arithmetic in prose.
- Times are wall-clock per case and include retrieval and reranking. Compare them only
  between runs on the same machine.

## Adding a case

Add an entry to `evals/benchmark.yaml` with a unique `id`, a capability, a horizon, and
at least one check. Prefer checks on the basis, a number, or a source over long term
lists; a case that can only be judged by reading the prose is a case that will be judged
differently next month. The suite's own test asserts every case has a known capability and
horizon and a unique id.
