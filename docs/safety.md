# Safety posture

This document exists because a tool that answers survival and medical questions offline,
with no way to check its work against anything, deserves to have its limits written down
rather than left implicit.

## Grounding is the safety mechanism

Hypatia answers **from retrieved library pages, with citations**. She is not a general
question-answering system that happens to have a library attached; she is a librarian who
must show you the page.

This matters more offline than online. A model answering from memory alone will produce
confident, fluent, plausible instructions for treating a wound or purifying water, and you
will have no way to check them — no second opinion, no search engine, possibly no doctor.
Grounding every answer in a citable source is what makes the tool trustworthy enough to
use in exactly the situation it was built for.

Concretely:

- Every answer cites the articles it came from, and every citation opens the real page.
- When the library does not cover something, the answer says so. "The library doesn't have
  anything on this" is a correct and useful answer.
- With no content installed, the app refuses to answer rather than falling back on the
  model's own recollection. An ungrounded answer is the failure mode that makes a tool
  like this dangerous.

The retrieval-quality tests in the repo assert this behaviour directly, including the case
where the correct response is an admission of ignorance.

## Medicine

Medicine is the highest-value content in the library and the highest-risk.

Answers drawn from medical sources carry the source prominently, and where a clinician
would ordinarily be involved, they say so. The framing is deliberate: this material exists
for the case where professional care is genuinely unavailable, not as a replacement for it
when it is. *Where There Is No Doctor* is written from exactly that premise, and Museion
inherits it.

Museion is not a diagnostic tool and does not present itself as one.

## Where the line is

A corpus assembled to help rebuild civilization contains chemistry, metallurgy, and
industrial process. That is not incidental — it is the point of the genre, and the reason
*The Knowledge* and *The Book* are the reference works this project is modelled on.

Retrieval will surface encyclopedia articles on such topics, as an encyclopedia should.
What Hypatia will not do is act as a synthesis assistant: taking reference material and
working it up into operational instructions for explosives, weapons, or poisons. The
library is a library. Reading it is not the same as being handed a procedure.

This boundary is stated here rather than left to inference, so that anyone forking or
modifying the project knows it was a decision and not an oversight.

## Attribution

Nearly all bundled content is CC BY-SA. Attribution is not a formality here — the citation
UI that satisfies the license is the same UI that lets you check the answer. Each manifest
in `catalog/sources/` records its license and attribution, and they travel with every
citation shown.

## What this project does not claim

It does not claim to contain everything needed to survive. It does not claim its medical
content substitutes for a clinician, or its engineering content for an engineer. It claims
to put the best freely-licensed reference corpus available onto your disk, make it
searchable without a network, and answer from it honestly — including when the honest
answer is that it does not know.
