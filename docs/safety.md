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

- An answer from the library cites the pages it came from, and every citation opens the
  real page.
- Every answer is labelled by what produced it: *from your library*, *from your library,
  with calculation*, *computed*, *from your own notes*, or *general knowledge — not from
  your library, unverified*. The label is worked out by the program from what actually
  happened (which passages were cited, whether the calculator ran), not written by the
  model.
- When the library does not cover a medical, drug or dose, chemical, electrical or other
  safety-critical question, saying so is the whole answer. "The library has nothing on
  this" is a correct and useful answer, and the assistant is told to give no doses,
  amounts or schedules from memory, however well known.
- For a general question the library does not cover (history, background science), it
  may answer briefly from general knowledge, under that last label and beginning with the
  words "The library has nothing on this, so from general knowledge". That is a deliberate
  allowance and the only one.
- Arithmetic is done by a calculator, not by the model, and the calculation is shown
  under the answer. A model scaling a bleach dose in its head got it wrong by a factor of
  five before this was so.

These are instructions to a language model, checked by tests and by a benchmark
([evals.md](evals.md)) that includes questions where the right response is an admission
of ignorance. They make a wrong answer less likely and easier to catch. They do not make
one impossible: a small local model can misread a passage, cite one that does not say
what it claims, or stray from its instructions. The citation is there so that you read
the page before acting on anything that matters.

## The agents

The agents (clinic, first steps, water planner and the rest) run on the same loop and
the same rules as the librarian. Three things are added for them:

- An agent that must answer from the library searches it before it answers; the program
  checks that it did.
- The clinic agent gives a medicine or a dose only from a passage it found, quoted as the
  book gives it, with danger signs first. If the library has no dose, it says so.
- An agent changes one of your notes or records only after you press Allow on a card
  that shows the change. Nothing is written behind your back.

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
