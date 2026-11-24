# Contributing to Museion

Museion is one person's hobby project. Reports and changes are welcome; answers may take
a while.

## The most useful things to send

- **"It did not start on my computer."** Museion has been run by a person on Windows
  with an NVIDIA card and on Linux without a graphics card. Anything else (a Mac, Linux
  with a graphics card, an AMD or Intel card on Windows) is new ground. Open an issue
  with the version, your system, and what `python -m museion hardware` prints.
- **A wrong answer that matters.** The question, the answer, the label above it and the
  sources it cited. If the page it cited does not say what the answer claims, say so:
  that is the failure this project most wants to hear about.
- **A real Meshtastic radio.** The Radio tab has only met simulated radios
  ([docs/radio.md](docs/radio.md)).
- **An archive that belongs in the catalogue.** Freely licensed, published as a ZIM by
  Kiwix or as PDFs. [docs/adding-content.md](docs/adding-content.md) is one command.

## Changing the code

[AGENTS.md](AGENTS.md) is the short version of everything below, written for people and
coding agents alike. Read it first. The rules that decide whether a change fits:

- **Standard library only.** No `pip install`, no new dependency, no build step. The
  point is that someone with no internet can still read and change every part.
- **Short files, comments that say why.** Most files are under 400 lines so each can be
  read whole, by a person or by a small local model.
- **Tests pass:** `python -m unittest discover -s tests` runs offline in under a minute.
  A change to how questions are answered should also say what it did to the benchmark
  ([docs/evals.md](docs/evals.md)).
- **Nothing that weakens the grounding.** Answers come from the library and cite it;
  where the library is silent on something safety-critical, the answer is that it is
  silent. See [docs/safety.md](docs/safety.md).

A small pull request that does one thing is easier to say yes to than a large one. For
anything large, open an issue first and describe it.

## AI-assisted changes

Most of Museion was written with an AI coding assistant, so changes made that way are
welcome here. Say so in the pull request, and say what you checked yourself: that the
tests pass, and that you ran the thing you changed.

## Licence

Museion is MIT licensed. By sending a change you agree it can be distributed under the
same licence.
