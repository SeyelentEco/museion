"""The command-line side of the catalogue: fetch what it lists, check a
library against it, and add to it. Kept apart from cli.py, whose commands are
about the library's answers.

  python -m museion fetch --list
  python -m museion fetch go-bag --dest E:/museion-library
  python -m museion verify --library E:/museion-library
  python -m museion add-source E:/museion-library/found_2026-07.zim --title "..." --category reference
  python -m museion sync-source <kiwix url> --category survival     (needs the internet)
  python -m museion check-sources                                   (needs the internet)
"""

from __future__ import annotations

import sys
from pathlib import Path

from museion import catalog, catalog_add, catalog_sync, download
from museion.disk import gb


def add_commands(commands) -> None:
    c = commands.add_parser("fetch", help="download a bundle, an archive or a model, checking each against its sha256")
    c.add_argument("id", nargs="?", help="a bundle, archive or model id (see --list)")
    c.add_argument("--dest", type=Path, help="the folder to put it in: the library, or the models folder")
    c.add_argument("--list", action="store_true", help="show every bundle, archive and model, then stop")
    c.add_argument("--force", action="store_true", help="download again even if present and correct")
    c.set_defaults(run=cmd_fetch)

    c = commands.add_parser("add-source", help="add an archive in the library to the catalogue (offline)")
    c.add_argument("file", type=Path, help="the .zim file")
    c.add_argument("--title", required=True, help="what the Library tab shows")
    c.add_argument("--category", required=True, choices=catalog_add.CATEGORIES,
                   help="the Library tab's shelf for it")
    c.add_argument("--description", default="", help="one line about it")
    c.set_defaults(run=lambda a: print(
        f"Added {catalog_add.add_source(a.file, a.title, a.category, a.description)}. "
        "Reload the Library tab to see it."))

    c = commands.add_parser("sync-source", help="write a catalogue entry from a Kiwix download link (online)")
    c.add_argument("url", help="the .zim file's address on download.kiwix.org")
    c.add_argument("--category", required=True, choices=catalog_add.CATEGORIES,
                   help="the Library tab's shelf for it")
    c.add_argument("--id", help="the catalogue id (default: the file name without its date)")
    c.add_argument("--title", help="what the Library tab shows (default: the Kiwix catalogue's title)")
    c.add_argument("--license", default=catalog_sync.DEFAULT_LICENSE,
                   help=f"default {catalog_sync.DEFAULT_LICENSE}; give the archive's own where it differs")
    c.add_argument("--attribution", default=catalog_sync.DEFAULT_ATTRIBUTION,
                   help=f"default: {catalog_sync.DEFAULT_ATTRIBUTION}")
    c.set_defaults(run=lambda a: print("wrote", catalog_sync.sync_source(
        a.url, a.category, a.id, a.title, a.license, a.attribution)))

    c = commands.add_parser("check-sources", help="look for newer releases of the catalogue's archives (online)")
    c.set_defaults(run=lambda a: sys.exit(1 if catalog_sync.check() else 0))


def items(id: str) -> tuple[str, list]:
    """What an id names: a bundle's archives, one archive, or one model."""
    if any(b.id == id for b in catalog.bundles()):
        b, sources = catalog.bundle(id)
        return f"Bundle: {b.title}", sources
    for s in catalog.sources():
        if s.id == id:
            return f"Archive: {s.title}", [s]
    for m in catalog.models():
        if m.id == id:
            return f"Model: {m.title}", [m]
    raise ValueError(f"nothing called {id!r} in the catalogue; `python -m museion fetch --list` shows what is")


def urls_of(item) -> list[str]:
    return item.all_urls() if isinstance(item, catalog.Source) else item.urls


def cmd_fetch(args) -> None:
    if args.list:
        show_list()
        return
    if not args.id or not args.dest:
        raise ValueError("say what to fetch and where: fetch <id> --dest <folder> (or fetch --list)")
    what, wanted = items(args.id)
    dest: Path = args.dest
    print(f"\n{what}\nInto:  {dest}\nSize:  {gb(sum(i.size_bytes for i in wanted))} in {len(wanted)} file(s)\n")
    fetched = present = 0
    for item in wanted:
        target = dest / item.filename
        if not args.force and download.is_verified(target, item.size_bytes, item.sha256):
            print(f"  ok      {item.filename}  (already here, checked)")
            present += 1
            continue
        # With --force the file already there stays until the new copy has
        # passed its checksum and replaces it.
        try:
            url = download.download_verified(download.Spec(urls_of(item), target, item.size_bytes, item.sha256),
                                             lambda p, name=item.filename: progress(name, p))
        except (OSError, RuntimeError) as e:
            print()
            print(f"  FAILED  {item.filename}\n          {str(e).replace(chr(10), chr(10) + '          ')}",
                  file=sys.stderr)
            # Every web source is down. BitTorrent has no single host to
            # lose, so offer it -- as a link for the person's own client, not
            # something Museion does itself. A preparedness tool that quietly
            # made every user a seed would be deciding about their bandwidth
            # and their visibility on their behalf.
            if getattr(item, "torrent", None) or getattr(item, "magnet", None):
                print("\n  Every web source failed. This archive is also on BitTorrent:", file=sys.stderr)
                if item.torrent:
                    print(f"    torrent: {item.torrent}", file=sys.stderr)
                if item.magnet:
                    print(f"    magnet:  {item.short_magnet()}", file=sys.stderr)
                print(f"  Download it with your own client, put {item.filename} in {dest}, then run\n"
                      f"    python -m museion verify --library {dest} {item.id}", file=sys.stderr)
            raise RuntimeError(f"{item.id} could not be fetched") from None
        print(f"\r  done    {item.filename}  {gb(item.size_bytes)}  from {url.split('/')[2]}          ")
        fetched += 1
    print(f"\n{fetched} downloaded, {present} already here, in {dest}\n")


def progress(name: str, p: download.Progress) -> None:
    doing = "checking" if p.resuming else "fetching"
    share = f"{p.received * 100 // p.total}%" if p.total else gb(p.received)
    print(f"\r  {doing} {name}  {share}          ", end="", flush=True)


def show_list() -> None:
    sources = {s.id: s for s in catalog.sources()}
    print("\nBundles\n")
    for b in catalog.bundles():
        total = sum(sources[i].size_bytes for i in b.sources if i in sources)
        print(f"  {b.id:<22} {gb(total):>8}  {b.title}")
        if b.description:
            print(f"  {'':<22} {'':>8}  {b.description}")
    print("\nArchives\n")
    for s in sources.values():
        print(f"  {s.id:<34} {gb(s.size_bytes):>8}  {s.category:<9} {s.title}")
    print("\nModels  (`python -m museion hardware` says which tier this computer is)\n")
    for m in catalog.models():
        tier = "/".join(m.tiers) if m.tiers else m.role
        print(f"  {m.id:<34} {gb(m.size_bytes):>8}  {tier:<16} {m.title}")
    print()


def verify_library(library: Path, id: str | None) -> None:
    """Re-hash the library's archives against the catalogue. This is the
    check to run before you need it -- after a long download, after moving a
    drive, and once in a while on a drive that has been sitting in a
    cupboard. Silent bit rot in a file you are relying on is the whole
    problem."""
    wanted = items(id)[1] if id else catalog.sources()
    print(f"\nChecking {len(wanted)} archive(s) in {library}\n")
    ok, bad, missing = [], [], []
    for item in wanted:
        target = library / item.filename
        if not target.exists():
            print(f"  --      {item.filename}  (not downloaded)")
            missing.append(item)
        elif target.stat().st_size != item.size_bytes:
            print(f"  BAD     {item.filename}  {target.stat().st_size} bytes, should be {item.size_bytes}")
            bad.append(item)
        elif download.is_verified(target, item.size_bytes, item.sha256):
            print(f"  ok      {item.filename}  {gb(item.size_bytes)}")
            ok.append(item)
        else:
            print(f"  BAD     {item.filename}  checksum differs from the catalogue's {item.sha256}")
            bad.append(item)
    print(f"\n{len(ok)} correct, {len(bad)} damaged, {len(missing)} not downloaded\n")
    if bad:
        print("Fetch the damaged ones again with:")
        for item in bad:
            print(f"  python -m museion fetch {item.id} --dest {library} --force")
        print()
        sys.exit(2)
