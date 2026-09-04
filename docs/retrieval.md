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

## Why: the zimgit archives are PDF collections, not web pages

Enumerating the archives explains it. A `zimgit-*` file contains roughly 700 entries of
bundled JavaScript viewer scaffolding — SVG icons, source maps, TypeScript sources — around
a set of **PDF documents**. `zimgit-water` has 803 entries, of which 7 are the actual
content.

| Archive | Entries | PDFs |
|---|---|---|
| zimgit-water | 803 | 7 |
| zimgit-medicine | 808 | 12 |
| zimgit-knots | 807 | 11 |
| zimgit-food-preparation | 813 | 17 |
| zimgit-post-disaster | 904 | 108 |
| **Go Bag total** | | **155** |

There is no HTML article text to index, which is why the index holds only the landing pages.

The archives do ship a `database.js` carrying real per-document metadata — title,
description, author — which the JavaScript viewer uses. The PDF filenames themselves are
useless (`Emergency Preparedness (1).pdf` … `(108).pdf`), so this file is the only source of
real titles and is essential to indexing them well.

## Two tooling traps found on the way

**`zimdump dump` corrupts binaries on Windows.** It writes extracted files in text mode, so
every `0x0A` becomes `0x0D 0x0A`. A PDF the ZIM records as 117,197 bytes is written as
118,056 — 859 newlines expanded — and its header reads `%PDF-1.2 \r\r\n`. Every extractor
then fails with `zlib error: incorrect header check`, which looks exactly like a corrupt or
scanned source document and is not.

Fetch content over `kiwix-serve` HTTP instead: `/content/<zim>/<path>` returns byte-exact
data (117,197 bytes, correct header). Museion already runs kiwix-serve as a sidecar, so this
is also the simpler design. `zimdump list` is fine and is still the right way to enumerate.

**The bundled `pdftotext` is Xpdf 4.00 from 2017** and cannot read modern PDFs, failing with
the same zlib errors. Two independent extractors failing identically was the clue that the
bytes, not the documents, were wrong.

Once fetched correctly, the content extracts cleanly — the first water document yields 1,038
words of exactly the expected material on distillation and contamination.

## What the Go Bag actually contains

With extraction working, the corpus is far better than the empty search results suggested:

| Archive | Documents | Pages | Words |
|---|---|---|---|
| zimgit-water | 7 | 314 | 102,168 |
| zimgit-medicine | 12 | 3,174 | 960,707 |
| zimgit-knots | 11 | 644 | 72,021 |
| zimgit-food-preparation | 17 | 1,135 | 328,192 |
| zimgit-post-disaster | 108 | 12,568 | 3,255,440 |
| **Total** | **155** | **17,835** | **4,718,528** |

Only 9 documents (6%) are image scans with no text layer; none failed to parse. The titles
are the canonical works of the genre — *Where There Is No Doctor* (503 pages), *A Book for
Midwives* (544), *Nuclear War Survival Skills* (510), *Foxfire Three*, the HHS Pandemic
Influenza Plan, FEMA manuals.

4.7 million words in 822 MB is a serious reference library, and none of it was reachable
through the archives' own search.

**Duplicates are real and must be handled.** *Where There Is No Doctor* appears in both the
medicine and post-disaster archives at an identical 182,240 words, as does *A Book for
Midwives* at 181,962. Indexing both copies would put the same passage twice into every set
of retrieval candidates, crowding out other sources. The indexer hashes extracted text and
skips exact duplicates.

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

## Knowing when to say nothing

The hardest case in the evaluation set is the one where the right answer is silence.

Query terms are OR-ed, because a question's wording rarely matches a document's. The cost
is that an OR query always matches *something*. Asked "what caused the Bronze Age
collapse" — a topic the Go Bag contains nothing about — the index happily returned pages
about blast overpressure collapsing lungs, and one that reads "Ice ages; Economic
collapse". Two query terms out of three, so a flat coverage threshold let it through.

The fix is to weight coverage by **inverse document frequency**. Missing "age" costs
almost nothing, because nearly every document contains it. Missing "bronze" costs almost
everything, because that is the word carrying the question. A term the corpus has never
seen gets the maximum weight: its absence is the strongest available evidence that the
library does not cover what is being asked.

With that gate the evaluation set goes 8/8 — every real question still answered, and the
out-of-corpus one correctly refused. `refuses_questions_the_library_does_not_cover` pins
the behaviour, asserting both halves: nothing for the uncovered question, and a real answer
from the same corpus for one it does cover.

This matters more here than in an online tool. Someone consulting Museion generally cannot
check the answer anywhere else, so a confident irrelevant passage is worse than an honest
"the library doesn't have this".

## The gate belongs at the topic, not the passage

The IDF gate above was first applied to every passage individually. That looked reasonable
and quietly broke retrieval.

Asked *"how do I safely can green beans to avoid botulism"*, the answer became: the sources
don't contain canning procedures. It was wrong. `Canning Food` pages 16–18 and 29 hold the
packing steps, processing times and USDA recommendations. They were discarded because they
describe the method without ever using the words *green*, *beans* or *botulism* — while the
book's **index page**, which contains all three and teaches nothing, sailed through.

The gate answers one question: *does the library cover this topic at all?* That is a
property of the best passage in the corpus, not of each passage. Deciding which passage
answers best is the reranker's job. Applying the topic gate per passage let it overrule the
component that was actually good at the decision.

Restructured — threshold on the best passage, everything else passed to the reranker — the
same question now returns the pressure-canner requirement, 240°F, 10 lb weighted / 11 lb
dial gauge, the altitude adjustment, and both hot- and raw-pack procedures, all cited. The
Bronze Age question is still refused, because no passage anywhere clears the bar.

Getting canning times wrong causes botulism, which makes this the sharpest available
illustration of the point: **a retrieval filter that looks conservative can be the thing
that makes an answer dangerous.** Being unable to answer is safe. Being wrong about the
reason you cannot answer is not.

## …and the reranker is the better gatekeeper

Driving the built app with the questions a person actually types found the next crack.
*"We have a lot of green beans and no fridge, how do I can them so nobody gets
botulism"* was refused — the same canning procedure that the tidier eval phrasing finds.

Two causes. "can" was a stopword, so the topic verb vanished from the query. And *fridge*
and *nobody* occur nowhere in the corpus (the documents say *refrigerator*), so under IDF
weighting each carried the maximum weight — the weight meant for a *topic* word like
*bronze* — and together they sank the coverage score below the bar. Term overlap cannot
tell an absent topic word from an absent incidental one.

The reranker can. It scores relevant passages near 0.9 and irrelevant ones at 0.0 with a
wide gap between, so when one is available it now decides coverage: if even its top-scored
passage reads as irrelevant, the library does not hold the answer. The IDF gate remains
only as the fallback for machines running without a reranker. Both regression phrasings
are in the eval set, and *Bronze Age* still refuses under both judges.

## …and even then, the right pages have to be in the pool

Fixing the gate exposed the next layer. With the natural phrasing, the reranker never
*saw* the pressure-canning procedure: keyword retrieval ranked *Canning Food* pages 17 and
18 at **252nd and 283rd**, because they never use the word *botulism*, while the book's
contents page — which does — sat 3rd. No candidate cutoff rescues that.

The plan always called for query expansion, and this is where it earned its place. Before
retrieval, the chat model rewrites the question into the two or three searches a reference
librarian would type — for this one it produced *"low acid vegetable canning procedures"*,
*"botulism prevention pressure canning"*, *"green beans safe preservation methods"* — and
the candidates from all of them are pooled (133, deduplicated by page) before the reranker
scores anything. The procedure pages arrive through the rewrites, the reranker puts them in
the top eight, and the answer comes out right: pressure canner, 240 °F, 10/11 lb by gauge,
½-inch headspace, altitude-adjusted times.

The rewrite is an aid, not a dependency: if the chat model is unreachable, retrieval runs
on the question alone, exactly as before.

A note on the harness, because it cost an hour: the evaluation script was pointed at a
stale, pre-workspace build directory, so every "gather-mode" result it reported for a day
came from a binary that predated the changes under test. It now uses the workspace target,
and the stale directory is gone.

## Contents pages are not answers

Index and contents pages match almost any question about a book, because they list every
topic in it, and contain none of the answer. The reranker likes them for the same reason a
keyword search does.

They are demoted rather than dropped, and demoted *after* reranking — doing it before is
pointless, since the reranker simply re-sorts them back to the top. Dropping them outright
would be wrong too: number-dense pages include dosage charts and altitude tables, which are
often exactly what someone needs. Measured on this corpus, prose runs 0.03–0.06 numeric
token density, real tables reach 0.14 at the 95th percentile, and the contents page that
prompted this measured 0.42.

## Tools, and knowing where an answer came from

Asked "10×10", the original loop searched the library, found nothing, and refused. The
grounding rule doing exactly what it was built for, on a question it was never meant for.

Hypatia now has two tools and decides per question:

- **`search_library`** — retrieve and rerank, returning numbered passages. The model may
  call it more than once with different words; numbering continues across calls so
  citations stay unambiguous.
- **`calculate`** — a small expression evaluator written in Rust: arithmetic, a few
  functions, and unit conversion for the units this library deals in (gallons and
  teaspoons, pounds and kilograms, °F and °C). Nothing executes. A model that reads
  "¼ teaspoon per gallon" from a source and multiplies by 55 in its head gets it wrong
  often enough — it produced 339 ml on the first attempt, against a correct 68 — that
  the multiplication must not be its job.

Pure arithmetic never reaches the model at all: "what is 12 x 12" is evaluated directly.

**The basis label is decided by which tools ran, not by what the model says.** Every
answer carries one of: *from your library*, *from your library, with calculation*,
*computed*, or *general knowledge — not from your library, unverified*. The last is the
one that must never pass as library material, and the model cannot promote itself into
a better category by claiming a source it did not retrieve.

On general knowledge: when the library has nothing, Hypatia may answer from training for
history, science background and the like — prefixed, in its own words, "The library has
nothing on this, so from general knowledge:" — but not for medical, dosing, chemical,
electrical or other safety-critical questions, where "the library doesn't cover this"
remains the complete answer. That is one clause in the system prompt, and it is a policy
choice rather than a technical one.

**Narration before a tool call is discarded.** The model thinks aloud on its way to
calling a tool, and that narration streamed straight to the screen — wrong number and
all — before the calculator returned. Text is still streamed optimistically, but a turn
that ends in tool calls sends a reset, and only the final turn's text stands.

Why not delegate to a coding harness (opencode and the like)? It would mean shipping
another agent runtime, giving generated code a way to run on the machine, and inheriting
a coding agent's failure modes for questions whose answer is "100". The right version of
that instinct is the other way round: expose the library as an MCP server so any harness
can use *it*. That is on the list.

## An open question for M1

Common single words return oddly few hits even on the good archives — "iron" returns 1
while "distillation" returns 593. Distinctive terms behave well and phrases behave well, so
this does not block the design, but the query-expansion step should be tuned against it
rather than assuming raw keyword hits are well-ordered. Worth measuring properly with the
fixed question set before the reranker is tuned.
