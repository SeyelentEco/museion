# Retrieval, and a measured finding that changed the design

## The original plan

Every ZIM was assumed to ship with a usable Xapian full-text index, so Museion could search
the index that already exists rather than spending hours embedding article chunks into a
vector database before the library becomes usable at all.

That assumption is **half true**, and the half that is false matters.

## What was measured

Tested against `kiwix-serve` 3.8.1 serving the real Go Bag library, plus
`wikipedia_en_chemistry_nopic_2026-07` as a control.

**mwoffliner-produced ZIMs (Wikipedia and friends) — full-text search works well:**

| Query | Hits |
|---|---|
| distillation | 593 |
| electrolysis | 532 |
| water purification | 400 |
| soap | 223 |
| iron smelting | 130 |

Result titles are sensible: *Distillation*, *Smelting*, *List of purification methods in chemistry*.

**zimit-produced ZIMs (the entire `zimgit-*` series) — full-text search is empty:**

| Query | Hits across all five Go Bag archives |
|---|---|
| chlorine | 0 |
| boiling | 0 |
| bleach | 0 |
| wound | 0 |
| bandage | 0 |
| bowline | 0 |
| water | 1 (the landing page) |

The archives *do* contain a `fulltext/xapian` index entry — it is present in all five files —
but it holds only about four documents, the landing pages. The crawled content behind them
is not indexed. Searching the whole library for "of" returns 4 results.

The cause is how these archives are built. The `zimgit-*` collections come from
Browsertrix/zimit web crawls, where page content sits behind a JavaScript replay layer, so
the indexer never extracts indexable text. This is a property of the build pipeline, not of
these particular topics, and it applies to zimit archives generally.

## Why this matters more than it first appears

The zimgit archives are the *most curated, most directly useful* content in the Go Bag —
hand-assembled post-disaster, water, medicine, food and knots references. They are the
reason the bundle exists. Under the original design they would have been browsable but
invisible to the assistant, which is close to the worst outcome: a library that looks
complete and silently cannot answer from its best material.

Finding this before building the retrieval layer cost one afternoon. Finding it after would
have meant rebuilding that layer.

## The design as revised

**Two retrieval paths, chosen per archive.**

1. **Archives with a working index** (Wikipedia and the rest of the mwoffliner catalogue):
   query `kiwix-serve` directly, as originally planned. No build step, queryable the moment
   the download finishes. This still covers the overwhelming majority of content by volume.

2. **Archives with no usable index** (zimit/zimgit): Museion builds its own. Entries are
   enumerated from the archive, HTML is reduced to text, and the result goes into a local
   SQLite FTS5 index. At Go Bag scale this is 862 MB of content — minutes, not the hours
   that embedding a full vector database would take, and a fraction of the disk.

   This is not the "embed everything" approach the original design rejected. It is keyword
   indexing over a small curated corpus, which is cheap. The distinction that mattered was
   never "index vs no index" — it was "hours of GPU time before the tool works" vs "not
   that."

Detection is automatic: probe the archive's index at import, and if it returns nothing for
terms drawn from its own content, build a local index instead. That way the app stays
correct for archives that don't exist yet.

**Reranking is unchanged.** Both paths feed candidates to the same 0.6B cross-encoder,
which is what fixes keyword search's weakness at paraphrase.

## An open question for M1

Common single words return oddly few hits even on the good archives — "iron" returns 1
while "distillation" returns 593. Distinctive terms behave well and phrases behave well, so
this does not block the design, but the query-expansion step should be tuned against it
rather than assuming raw keyword hits are well-ordered. Worth measuring properly with the
fixed question set before the reranker is tuned.
