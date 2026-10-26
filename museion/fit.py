"""Computing the GPU offload split for a model on *this* machine.

Recent llama.cpp ships `llama-fit-params`, which measures the model against
the card actually present and prints ready-to-use `-ngl` and `-ot` arguments.
Museion runs it at setup rather than shipping a table, because two people with
the same nominal GPU have different amounts free -- a second monitor or an
open browser changes the answer -- and a split that is one layer too generous
fails to load, while one that is too cautious leaves speed on the table.

Measured here: against an RTX 3060 it put 41 layers on the GPU and the expert
tensors of blocks 15-40 on the CPU, matching a split that had been reached by
hand over several evenings.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Stops a console window flashing up on Windows while the fit runs.
CREATE_NO_WINDOW = 0x0800_0000

# What llama-fit-params leaves free on the card, in MiB. Its default, 1024,
# filled an RTX 3060 to 11.8 of 12 GB; whatever else the desktop had open
# then pushed half a gigabyte into shared memory, and the 35B read prompts at
# 106 tokens a second instead of 689 and wrote at 23 instead of 42. With 2048
# it measured fastest of the splits tried and left room for the rest.
MARGIN_MIB = 2048

# The largest context Museion runs a chat model with: opencode's, in edit.py.
# Measuring at it means the split that answers questions also serves `edit`,
# which raises the context afterwards without measuring again.
CONTEXT = 32768


@dataclass(frozen=True)
class Fit:
    # Arguments to pass straight to llama-server, e.g. -ngl 41 -ot "...".
    args: list[str]
    # The -ngl value on its own, since the supervisor sets it separately.
    gpu_layers: int | None


def fit_model(fit_exe: Path, model: Path, ctx: int) -> Fit:
    """Run llama-fit-params and parse what it prints.

    KV cache is measured at q8_0, not q4_0: that is what Museion runs with,
    because aggressive KV quantisation degrades tool calling, and the fit has
    to be measured for the configuration that will actually be used."""
    fit_exe = Path(fit_exe)
    if not fit_exe.exists():
        raise FileNotFoundError(
            f"llama-fit-params not found at {fit_exe}. It ships with llama.cpp builds "
            "from 2026 onward; fetch a current build."
        )
    try:
        out = subprocess.run(
            [str(fit_exe), "-m", str(model), "-c", str(ctx),
             "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "-fitt", str(MARGIN_MIB)],
            capture_output=True, stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as e:
        raise RuntimeError(f"running {fit_exe}: {e}") from e

    if out.returncode != 0:
        err = out.stderr.decode("utf-8", errors="replace").splitlines()
        status = f"exit status: {out.returncode}" if out.returncode >= 0 else f"signal: {-out.returncode}"
        raise RuntimeError(f"llama-fit-params failed ({status}):\n" + "\n".join(err[-6:]))

    return parse_fit_output(out.stdout.decode("utf-8", errors="replace"))


def parse_fit_output(stdout: str) -> Fit:
    """The arguments from what llama-fit-params printed on stdout."""
    # Everything diagnostic goes to stderr; stdout is the one line we want.
    lines = [line.strip() for line in stdout.splitlines() if line.strip().startswith("-")]
    if not lines:
        raise RuntimeError("llama-fit-params printed no arguments")

    args = split_shell_words(lines[-1])
    gpu_layers = None
    for flag, value in zip(args, args[1:]):
        if flag in ("-ngl", "--n-gpu-layers"):
            # Only the first one counts. A value that is not a whole number
            # (newer builds accept "auto") leaves the layer count unknown.
            if re.fullmatch(r"\+?[0-9]+", value) and int(value) < 2**32:
                gpu_layers = int(value)
            break
    return Fit(args, gpu_layers)


def split_shell_words(line: str) -> list[str]:
    """Split a command line the way a shell would, honouring double quotes.

    The -ot regex is quoted and contains characters like | and ( that must
    survive intact -- splitting on whitespace alone would break it. Python's
    shlex is not used: outside quotes it treats a backslash as an escape, so
    an unquoted regex like blk\\.15 would lose its backslashes."""
    out: list[str] = []
    cur: list[str] = []
    in_quotes = False
    had_token = False

    for c in line:
        if c == '"':
            in_quotes = not in_quotes
            had_token = True
        elif c.isspace() and not in_quotes:
            if had_token:
                out.append("".join(cur))
                cur = []
                had_token = False
        else:
            cur.append(c)
            had_token = True
    if had_token:
        out.append("".join(cur))
    return out
