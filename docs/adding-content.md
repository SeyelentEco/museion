# Adding content

Museion's catalog is plain YAML. You can add a source by hand, but you shouldn't have to,
and you should never type a checksum yourself.

## Adding a ZIM from Kiwix

Kiwix publishes a `.sha256`, a `.magnet` and a `.torrent` beside every archive. The sync
tool reads all three and writes the manifest for you:

```bash
node scripts/catalog-sync.js add \
  https://download.kiwix.org/zim/other/zimgit-knots_en_2024-08.zim \
  --category survival \
  --title "Knots and Rope Work"
```

That writes `catalog/sources/zimgit-knots.yaml` containing the authentic upstream checksum,
the exact byte length, the torrent, and the mirror list harvested from the magnet's
webseeds — without downloading the archive.

Browse what's available at [browse.library.kiwix.org](https://browse.library.kiwix.org) or
the raw index at [download.kiwix.org/zim](https://download.kiwix.org/zim/).

Categories are `survival`, `medicine`, `making`, `reference`. They drive how content is
grouped in the app, so pick the one a person looking for that material would reach for.

## Read what it prints. Filenames lie.

`catalog-sync` looks the archive up in the Kiwix catalogue and prints what upstream says it
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

So the manifest's title and description come from upstream, not from whoever adds the
entry. If the tool reports **`!! not found in the Kiwix catalogue`**, do not assume — fetch
the archive and look inside it with `zimdump list` before adding it.

The `_ftindex` tag is worth noticing too: it declares whether the archive carries a usable
full-text index, which decides how Museion has to index it. Kiwix is honest about it — the
zimgit archives declare `_ftindex:no`, exactly matching what measurement showed.

## Keeping the catalog fresh

Kiwix reissues archives periodically and removes old dated versions, so a manifest that
worked last year may point at a file that no longer exists. Check with:

```bash
node scripts/catalog-sync.js check
```

It reports each source as current, stale (with the exact command to update it), or missing
upstream. Run it before a release. A manifest pointing at a 404 is worse than no manifest,
because it fails at the moment someone is trying to build their library.

## Making a bundle

A bundle is a named list of source ids:

```yaml
id: homestead
title: Homestead
description: Long-term self-sufficiency and repair.
sources:
  - wikipedia-en-nopic
  - ifixit
  - appropedia
```

Drop it in `catalog/bundles/`. It shows up in `node scripts/fetch.js --list` immediately.

## Adding content that isn't a ZIM

Any file with a stable URL and a known SHA-256 works — the fetcher doesn't care what's
inside. Write the manifest by hand:

```yaml
id: my-source
title: Something Useful
category: making
filename: something-useful.zim
version: "2026-01"
size_bytes: 12345678
sha256: <64 lowercase hex characters>
urls:
  - https://example.org/something-useful.zim
license:
  name: CC-BY-SA-4.0
  attribution: Original authors
```

The `id` must match the filename of the YAML file, and `sha256` must be 64 lowercase hex
characters — validation will tell you precisely which file and field is wrong if not.

Get the checksum by downloading once and running `node scripts/verify.js`, or compute it
with `sha256sum` / `Get-FileHash`.

## Licensing

Museion distributes no content. Only add sources that are freely licensed, and record the
license and attribution honestly — they are shown to the user with every citation drawn
from that source, which is both a legal requirement for CC BY-SA material and the thing
that lets someone judge whether to trust an answer.
