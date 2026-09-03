# Preparing a drive for a machine with no connection

Museion needs internet once, to build the library. The machine that will *use* the library
never does. Those can be two different machines.

This matters more than it sounds. The common failure of preparedness tooling is that it
assumes you'll set it up before you need it. Being able to build a library on a working
connection and carry it to one that has none — a cabin, a boat, an air-gapped machine,
somewhere the network is simply gone — is what makes it usable after the fact rather than
only before.

## Build the library onto the drive

On the machine that has internet, point the fetcher straight at the removable drive:

```bash
git clone https://github.com/<you>/museion
cd museion
npm install
node scripts/fetch.js go-bag --dest /Volumes/MYDRIVE/museion-library
```

On Windows:

```bash
node scripts/fetch.js go-bag --dest E:\museion-library
```

Downloads resume, so unplugging the drive or losing the connection partway is recoverable —
run the same command again and it picks up where it stopped. Files are only given their
real name once the checksum matches, so a half-written archive can never be mistaken for a
complete one.

## If the downloads fail

Every archive has eight web mirrors, and the fetcher tries them all. If every one is
down or unreachable from where you are, it prints the archive's torrent and magnet link
instead — Kiwix publishes both for every file. Download it with any BitTorrent client,
put the file in your library folder, and run `verify.js` (or press Fetch again in the
app): it is checked against the same checksum and picked up as if it had come from a
mirror.

Museion deliberately does not download via BitTorrent itself, and never seeds. Whether
your machine joins a swarm, and what that does to your bandwidth and visibility, is a
decision for you and your own client, not one a tool should make for you.

## Verify on the drive, before you leave

```bash
node scripts/verify.js --dest /Volumes/MYDRIVE/museion-library
```

Do this while you still have a connection. If a file is corrupt, this is the last moment
re-fetching it is easy.

## Verify again later

Drives that sit in a cupboard for two years develop bad sectors, and nothing announces it.
Re-run `verify.js` occasionally. It re-hashes every file against the catalog and tells you
exactly which command re-fetches anything that has rotted.

This is the check worth running when you *don't* need the library, so that it works when
you do.

## Filesystem

Use exFAT if the drive will move between Windows and macOS. Avoid FAT32: it caps files at
4 GB, and several of the larger archives — Wikipedia especially — exceed that. A download
that fails at exactly 4 GB is almost always this.

## What to carry

The Go Bag is 822 MB and fits anywhere. If you have the space, the larger bundles are worth
carrying — but a small verified library you actually have beats a large one you meant to
download.
