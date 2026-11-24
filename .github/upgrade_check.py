"""The release job's upgrade test: a real older release is upgraded by the
package just built, the way a person would do it, on the system the job runs
on. Unit tests cover the logic with stand-in copies (tests/test_upgrade.py);
this covers what only real files and programs show: Windows refusing to move
a folder in use, launchers, a Python replacing the Python beside it, and
Museion restarting itself from the page's button.

  python .github/upgrade_check.py --old museion-0.1.0-linux.tar.gz --new out/Museion --work tmp

1. The new package installs itself into the old copy (upgrade --into).
2. The old copy goes back (upgrade --back), and is upgraded again.
3. From its running page: upgrade to a stand-in "next" version with another
   Python and other helpers, Museion restarting itself; then Go back.
After every step the person's own files must be byte for byte as they were,
and their changes to the code still there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from museion import launchers, prepare, tools  # noqa: E402
from museion.portable import MARKER  # noqa: E402

PORT = 8079
NEXT = "99.0.0"
MINE = {"library/notes/stores.csv": "item,count\nrice,20\n", "models/mine.gguf": "a model", "tuning.toml": "# mine\n"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--old", required=True, type=Path, help="an older release's download")
    p.add_argument("--new", required=True, type=Path, help="the new package, unpacked")
    p.add_argument("--work", required=True, type=Path, help="an empty folder to work in")
    args = p.parse_args()
    new = args.new.resolve()
    work = args.work.resolve()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    step("Unpacking the older release")
    tools.extract(args.old, work / "old")
    old = next(f for f in (work / "old", *sorted((work / "old").iterdir())) if (f / MARKER).is_file())
    old_version, new_version = version(old), version(new)
    print(f"old copy: {old_version} in {old}\nnew package: {new_version} in {new}")
    assert old_version != new_version, "the new package is the same version as the older release"

    # A copy someone has lived in: their files, and their changes to the code.
    for path, text in MINE.items():
        (old / path).parent.mkdir(parents=True, exist_ok=True)
        (old / path).write_text(text, encoding="utf-8", newline="\n")
    calc = old / "app" / "museion" / "calc.py"
    calc.write_bytes(calc.read_bytes() + b"\n# my own note at the end of calc.py\n")
    (old / "app" / "my-notes.md").write_text("my own file\n", encoding="utf-8")
    before = mine(old)

    def unharmed(now: str) -> None:
        assert version(old) == now, f"the copy is version {version(old)}, not {now}"
        assert mine(old) == before, "the person's own files changed"
        assert calc.read_bytes().endswith(b"# my own note at the end of calc.py\n"), "a change to the code was lost"
        assert (old / "app" / "my-notes.md").is_file(), "a file added to the code was lost"
        print(f"ok: version {now}, the person's files and code changes intact")

    step("1. The new package installs itself into the old copy")
    run(launcher(new), "upgrade", "--into", str(old))
    unharmed(new_version)
    assert (old / "previous" / old_version / "app" / "museion").is_dir(), "the old version was not kept"
    changes = run(launcher(old), "changes")
    assert "museion/calc.py" in changes and "my-notes.md" in changes, "the changes are not listed"

    step("2. Going back, and upgrading again")
    run(launcher(old), "upgrade", "--back", wait=False)
    until(lambda: version(old) == old_version, 180, "the copy did not go back")
    time.sleep(3)  # the going back's last lines, if it was handed to another window
    unharmed(old_version)
    run(launcher(new), "upgrade", "--into", str(old), "--no-test")
    unharmed(new_version)

    step("3. From the page: a newer version with another Python and other helpers")
    nxt = work / "next"
    shutil.copytree(new, nxt, symlinks=True)
    v = nxt / "app" / "museion" / "version.py"
    v.write_text(v.read_text(encoding="utf-8").replace(f'"{new_version}"', f'"{NEXT}"'), encoding="utf-8")
    log = nxt / "app" / "CHANGELOG.md"
    log.write_text(log.read_text(encoding="utf-8").replace(f"## {new_version}", f"## {NEXT}\n\nA stand-in.\n\n## {new_version}", 1),
                   encoding="utf-8")
    (nxt / "support" / prepare.ORIGINAL_ZIP).unlink()  # so its code as it stands is what ships
    record = json.loads((nxt / "support" / "package.json").read_text(encoding="utf-8"))
    record["version"] = NEXT
    record["helpers"] = {name: "0" * 64 for name in record["helpers"]}
    (nxt / "support" / "package.json").write_text(json.dumps(record), encoding="utf-8")
    (nxt / "python" / "NEXT.txt").write_text("the next Python\n", encoding="utf-8")

    server = subprocess.Popen([*launcher(old), "--no-browser", "--port", str(PORT)], stdin=subprocess.DEVNULL)
    try:
        until(lambda: hello() is not None, 60, "Museion did not start")
        print(post("upgrade_from", path=str(nxt), port=PORT))
        post("quit")
        until(lambda: (hello() or {}).get("version") == NEXT, 240, "Museion did not come back upgraded")
        unharmed(NEXT)
        assert (old / "python" / "NEXT.txt").is_file(), "the Python was not replaced"
        state = post("upgrade_state")
        print(json.dumps(state, indent=1))
        assert state["last"]["to"] == NEXT and state["previous"] == new_version and not state["failed"], state
        assert "museion/calc.py" in state["changes"], state["changes"]

        step("4. From the page: Go back")
        print(post("upgrade_back", port=PORT))
        post("quit")
        until(lambda: (hello() or {}).get("version") == new_version, 240, "Museion did not come back")
        unharmed(new_version)
        assert not (old / "python" / "NEXT.txt").exists(), "the Python was not put back"
    finally:
        for name in ("tests.log", "switch.log", "museion.log"):
            log = old / "upgrade" / name
            if log.is_file():
                print(f"--- {name}\n{log.read_text(encoding='utf-8', errors='replace')[-3000:]}")
        try:
            post("quit")
        except OSError:
            pass
        server.wait(timeout=60)
    until(lambda: hello() is None, 60, "Museion did not stop")
    print("\nThe upgrade test passed.")


def step(text: str) -> None:
    print(f"\n=== {text}", flush=True)


def launcher(root: Path) -> list[str]:
    path = str(root / next(iter(launchers.launchers())))
    return ["cmd", "/c", path] if sys.platform == "win32" else [path]


def run(command: list[str], *args: str, wait: bool = True) -> str:
    """Run a launcher as a person would, with nobody at the keyboard."""
    done = subprocess.run([*command, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=1500)
    print(done.stdout, flush=True)
    assert not wait or done.returncode == 0, f"{' '.join(args)} exited with {done.returncode}"
    return done.stdout


def version(root: Path) -> str:
    done = subprocess.run([str(prepare.python_exe(root)), "-m", "museion", "version", "--number"], cwd=root / "app",
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    return done.stdout.strip() or f"(none: {done.stderr.strip()[-300:]})"


def mine(root: Path) -> dict[str, str]:
    """The person's files and settings, by checksum."""
    out = {}
    for top in ("library", "models", "agents", "config.json", "tuning.toml", MARKER):
        path = root / top
        for f in ([path] if path.is_file() else sorted(path.rglob("*")) if path.is_dir() else []):
            if f.is_file():
                out[f.relative_to(root).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def hello() -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/hello", timeout=2) as res:
            return json.load(res)
    except (OSError, ValueError):
        return None


def post(command: str, **args) -> object:
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/{command}", json.dumps(args).encode(),
                                     {"Content-Type": "application/json", "X-Museion": "1"})
    with urllib.request.urlopen(request, timeout=1500) as res:
        body = json.load(res)
    assert "ok" in body, f"{command}: {body.get('error')}"
    return body["ok"]


def until(done, seconds: int, why: str) -> None:
    end = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < end, why
        time.sleep(1)


if __name__ == "__main__":
    main()
