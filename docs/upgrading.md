# Upgrading Museion

A newer Museion can be put into an older one from a file, with no internet,
without losing anything of yours: not your library, your models, your notes or
settings, and not the changes you have made to Museion's own code.

This is for a copy that was downloaded or prepared as a drive (a folder with
`app/`, `python/`, `bin/` and a `museion-portable` file in it). A copy made
with git is brought up to date with `git pull`.

## What you need

A newer copy of Museion, for the same kind of computer: the download
(`museion-<version>-windows.zip`, `-macos.zip` or `-linux.tar.gz`), or a
folder or drive that already holds a newer Museion. Someone can hand it to you
on a stick; nothing is fetched.

## Two ways to do it

**From Museion's page.** Settings › Version and upgrades.

- *Upgrade this copy*: choose the newer download (or type the folder of a
  newer copy) and press Upgrade. Museion prepares the new version while it
  keeps running, then restarts itself as the new version. The page reloads
  when it is back and says what was done.
- *Bring another copy up to this version*: choose the folder or drive of an
  older copy. Close Museion in that copy first. If you have changed this
  copy's code, a tick box offers to take those changes along (below).
- *Check for a newer version*: asks GitHub what the latest release is, and
  can download it into this copy, check it against the release's checksum and
  upgrade from it. This is the only thing on the card that uses the internet,
  and nothing is asked unless you press it.

**From the command line.** Use the launcher in the copy's folder
(`Museion.cmd` on Windows, `Museion.command` on macOS, `./museion.sh` on
Linux), with Museion closed:

```
Museion.cmd upgrade D:\Downloads\museion-0.3.0-windows.zip    this copy, from a download
Museion.cmd upgrade E:\                                       this copy, from a newer copy or drive
Museion.cmd upgrade --into E:\                                put this version into that copy
Museion.cmd upgrade --into E:\ --with-changes                 ... and your changes to this copy's code
Museion.cmd upgrade --back                                    go back to the version before
Museion.cmd changes                                           what you have changed in the code
```

### Upgrading version 0.1.0

Version 0.1.0 has no upgrade command and no upgrade button, so it is upgraded
*by* the newer copy:

1. Unpack the newer download somewhere beside the old folder (not into it).
2. Close Museion.
3. In the **new** folder, run the launcher with `upgrade --into` and the old
   folder: `Museion.cmd upgrade --into C:\Museion`. Or start the new copy,
   open Settings › Version and upgrades, and use *Bring another copy up to
   this version*.
4. Start Museion from the old folder as before. The new folder can be deleted.

## What an upgrade touches

A copy's folder holds three kinds of thing.

| | Folders and files | An upgrade |
| --- | --- | --- |
| Yours | `library/` (archives, the index, your notes and records, maps, agents' work), `models/`, `config.json`, `tuning.toml`, `agents/` at the top (agents you made), `museion-portable` | never touches them |
| Museion's | `python/`, `bin/` (llama.cpp, kiwix-serve, pdfium, opencode, ripgrep) | replaces them, and only when the newer copy carries different builds |
| Both | `app/`: Museion's code, which you may have changed | merges them (below) |

`support/package.json` records which build of each helper a copy holds, which
is how an upgrade knows whether the Python or the helpers differ. Most
upgrades replace only `app/`, a few megabytes. Models and archives are never
copied, moved or downloaded again.

After an upgrade that replaced llama.cpp, the first start of the chat model on
each computer measures the graphics card again, as on first use.

## Your changes to the code

Museion is meant to be changed, with Edit Museion or by hand, so an upgrade
does not simply replace `app/`. Every copy keeps
`support/museion-app-original.zip`, the code exactly as that version shipped
it. That gives each file three versions: as shipped, as you have it, and as
the new version ships it.

| The file | What happens |
| --- | --- |
| You did not change it | the new version's file |
| You changed it, the new version did not | yours, as it is |
| You added it | kept |
| You deleted it, the new version did not change it | stays deleted |
| Both changed it, in different places | merged, line by line |
| Both changed the same lines | the new version's file; yours is set aside |

Nothing of yours is thrown away. A change that could not be carried is copied
to `my-changes/from-<old>-to-<new>/` beside `app/`: your whole file, and a
`.diff` of exactly what you had changed in it. `app/UPGRADE-NOTES.md` lists
each one with the reason. To put a change back, make the same change to the
new file and run the tests. Edit Museion can do this: ask it to "re-apply the
changes listed in UPGRADE-NOTES.md".

The merged code is tested before it is used: the new version's own tests are
run on it. If they fail with your changes in, the upgrade tries the new
version as shipped; if that passes, it goes in and *all* your changes are set
aside as above (`--keep-changes` keeps them in regardless). If the new version
fails its tests even as shipped, nothing is changed at all.

`python -m museion changes` (or the card in Settings) lists what you have
changed at any time; `--diff` shows each change.

### Two copies of your own

Someone with Museion on their computer and on a drive may have changed the
code on one of them. *Bring another copy up to this version* with the box
ticked (`upgrade --into <folder> --with-changes`) puts this copy's code into
the other **as it stands here**, changes included, merged with whatever was
changed there by the same rules. It works between two copies of the same
version too, when only the changes are new. The other copy still records the
version as it shipped, so everything taken along shows there as a change, and
its next upgrade carries it on. If the tests fail there with this copy's
changes in, the other copy gets the new version without them, and the summary
lists what was left behind (it is all still here).

## Going back

What an upgrade replaced is kept in `previous/<version>/`: the old `app/`, and
the old `python/` and `bin/` if they were replaced. *Go back* in Settings, or
`upgrade --back`, swaps them home again, and keeps the version you left in
`previous/` in its turn, so going back can be undone the same way. One
previous version is kept; the next upgrade replaces it. Deleting `previous/`
frees its space and only removes the way back.

Going back does not undo what you did since: your library, notes and settings
are as they are now.

## How it is done, and what can go wrong

**A newer copy installs itself into an older one.** The program that carries
out an upgrade is always the newer copy's own code, run by the newer copy's
own Python: `python -m museion upgrade --into <folder>`. So an upgrade never
depends on what an old version knew how to do, and no file of the copy being
changed is in use while it changes. *Upgrade this copy* unpacks the download
inside the copy (`upgrade/incoming/`) and starts that command there.

1. **Stage.** The new `app/` is built in `upgrade/new/` by the merge above,
   and its tests are run there; the new Python and helpers are copied beside
   it if they differ. Nothing in use has been touched, and Museion can be
   running. A failure here leaves the copy exactly as it was.
2. **Switch.** A list of renames is written to `upgrade/journal.json` and
   carried out: the parts in use move to `previous/<version>/`, the new ones
   into place. This takes a moment and needs Museion closed in that copy.
   On Windows a rename is refused while a program still runs from the folder;
   then the renames already made are undone and the upgrade says so.
3. **Finish.** The launchers and `README.txt` are rewritten as the new
   version writes them, `support/installed.json` gets a line, and the staging
   folder is removed.

- *The power fails, or the drive is pulled out, during the switch.* The list
  of renames is still there. Starting Museion, or running any upgrade command,
  finishes it. If Museion does not start, run the upgrade again from the
  newer copy.
- *"Museion is running."* Close its browser tab and its window, wait a few
  seconds, and run the command again.
- *"A copy for windows; this computer is linux."* The Python and the helpers
  are built for one kind of computer. Upgrade a drive on the kind of computer
  it runs on, with that system's download.
- *Not enough room.* An upgrade needs room for the new Python and helpers
  beside the old ones (about 1 GB on Windows when they are replaced, a few
  megabytes when only the code is).
- *The tests' output* of the last upgrade is in `upgrade/tests.log`.

## For whoever changes this

| What | Where |
| --- | --- |
| Staging, the checks, going back | `museion/upgrade.py` |
| The renames, the journal, finishing one cut short | `museion/switch.py` |
| Merging a person's changes | `museion/appmerge.py` (which file wins), `merge3.py` (lines within a file) |
| The commands, starting the newer copy's program, restarting Museion | `museion/upgrade_cli.py` |
| The card in Settings | `museion/upgrade_commands.py`, `ui/upgrade.js` |
| Asking for the latest release, fetching it | `museion/newer.py` |
| What a copy holds | `museion/package.py` (`support/package.json`) |
| Tests | `tests/test_upgrade.py`, `test_appmerge.py`, `test_merge3.py`, `test_upgrade_cli.py`, `test_newer.py`; the release job upgrades a real older release on each system (`.github/upgrade_check.py`) |

Two rules keep old copies upgradable. The folder layout above is a promise:
a new version may add to it, and must go on reading what the old ones wrote
(`config.json`, the index, notes, chats). And a release must be able to
upgrade the oldest release still in use, which the release job checks.
