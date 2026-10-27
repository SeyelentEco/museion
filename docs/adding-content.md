# Adding content

Museion's catalogue is plain TOML, one file per archive in `catalog/sources/`. You can add
a source by hand, but you shouldn't have to, and you should never type a checksum yourself.

## Adding an archive you already have

With no internet, for an archive someone copied into the library:

```bash
python -m museion add-source /path/to/library/wikipedia_en_chemistry_nopic_2026-07.zim \
  --title "Chemistry by Wikipedia" --category reference
```

It measures the file and writes `catalog/sources/wikipedia_en_chemistry_nopic.toml` with
its size and checksum, and nothing that cannot be known offline: no download links, so
nothing that was never true is there to check later. An id already in the catalogue is
refused, so an entry with real download links is never overwritten.

## Adding a ZIM from Kiwix

Kiwix publishes a `.sha256`, a `.magnet` and a `.torrent` beside every archive. With
internet, `sync-source` reads all three and writes the entry for you:

```bash
python -m museion sync-source \
  https://download.kiwix.org/zim/other/zimgit-knots_en_2024-08.zim \
  --category survival --id zimgit-knots \
  --title "Knots and Rope Work"
```

That writes `catalog/sources/zimgit-knots.toml` containing the authentic upstream checksum,
the exact byte length, the torrent, and the mirror list harvested from the magnet's
webseeds — without downloading the archive. Its first lines record the command, so the
entry can be written again from upstream rather than edited by hand.

Browse what's available at [browse.library.kiwix.org](https://browse.library.kiwix.org) or
the raw index at [download.kiwix.org/zim](https://download.kiwix.org/zim/).

Categories are `survival`, `medicine`, `making`, `reference`. They drive how content is
grouped in the app, so pick the one a person looking for that material would reach for.

## Read what it prints. Filenames lie.

`sync-source` looks the archive up in the Kiwix catalogue and prints what upstream says it
actually is:

```
probing appropedia_en_all_maxi_2026-02.zim ... 555 MB, sha256 dcf200ba723c...
  upstream title : Appropedia
  upstream says  : Shared knowledge to build rich, sustainable lives
  tags           : appropedia;_pictures:yes;_ftindex:yes
```

This exists because of a near miss. `survivors_en_all_maxi` reads, in a preparedness
catalogue, exactly like the Survivor Library — a well-known collection of pre-industrial
technical works. It is in fact a YouTube archive for the video game *Vampire Survivors*:
13,780 entries of gameplay teasers. It was one command away from shipping in the Homestead
bundle, labelled "public domain works", for people to download expecting to learn how to
make things.

So the entry's title and description come from upstream, not from whoever adds it. If
the command reports **`!! not in the Kiwix catalogue`**, do not assume — fetch
the archive and look inside it with `zimdump list` before adding it.

The `_ftindex` tag is worth noticing too: it declares whether the archive carries a usable
full-text index, which decides how Museion has to index it. Kiwix is honest about it — the
zimgit archives declare `_ftindex:no`, exactly matching what measurement showed.

## Keeping the catalog fresh

Kiwix reissues archives periodically and removes old dated versions, so a manifest that
worked last year may point at a file that no longer exists. Check with:

```bash
python -m museion check-sources
```

It reports each source as current, stale (with the exact command to update it), or missing
upstream. Run it before a release. A manifest pointing at a 404 is worse than no manifest,
because it fails at the moment someone is trying to build their library.

## Making a bundle

A bundle is a named list of source ids:

```toml
id = "homestead"
title = "Homestead"
description = "Long-term self-sufficiency and repair."
sources = [
  "wikipedia_en_all_nopic",
  "ifixit_en_all",
  "appropedia_en_all_maxi",
]
```

Drop it in `catalog/bundles/`. It shows up in `python -m museion fetch --list` and the
set-up screen immediately.

## Adding content that isn't a ZIM

Any file with a stable URL and a known SHA-256 works — the fetcher doesn't care what's
inside. Write the entry by hand:

```toml
id = "my-source"
title = "Something Useful"
category = "making"
filename = "something-useful.zim"
version = "2026-01"
size_bytes = 12345678
sha256 = "<64 lowercase hex characters>"
urls = [
  "https://example.org/something-useful.zim",
]
license = { name = "CC-BY-SA-4.0", attribution = "Original authors" }
```

Name the file after its `id`, and make `sha256` 64 lowercase hex characters; if anything
is wrong, Museion names the file and the field when it reads the catalogue.

Get the checksum by downloading once and running `add-source` on the file (then add the
`urls` it leaves empty), or compute it with `sha256sum` / `Get-FileHash`.

## Licensing

Museion distributes no content. Only add sources that are freely licensed, and record the
license and attribution honestly — they are shown to the user with every citation drawn
from that source, which is both a legal requirement for CC BY-SA material and the thing
that lets someone judge whether to trust an answer.
