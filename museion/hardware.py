"""Working out what the machine can run.

The instinct on a mid-range GPU is to pick the largest dense model that fits
in VRAM. That is usually wrong. A sparse mixture-of-experts model with its
expert tensors offloaded to system RAM beats a dense model that merely fits,
because only a fraction of its parameters fire per token: measured on one
12 GB card, a 35B-A3B MoE ran at 43.6 tok/s against 30.6 for a dense 12B that
sat entirely in VRAM. Bigger and faster at once.

So the tiers below reach for MoE architectures rather than the largest dense
model that fits. See docs/hardware.md.

Everything that asks the system goes through a `run` function and a
`platform` string that the tests replace with fakes, so detection can be
tested on any machine.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# The kinds of accelerator. These are also the keys the helper programs and
# models are listed under (a "cuda" build of llama-server, and so on).
CUDA = "cuda"      # NVIDIA, addressed through CUDA.
METAL = "metal"    # Apple Silicon, addressed through Metal against unified memory.
VULKAN = "vulkan"  # Anything else with a GPU worth using: AMD, Intel, via Vulkan.
CPU = "cpu"        # No usable GPU. Still works; just slowly.

# Which class of model this machine should run, smallest first. The names are
# the ones the model list uses ("tiers: [Capable, Generous]").
MODEST = "Modest"      # CPU only, or under ~6 GB of VRAM. A small dense model.
CAPABLE = "Capable"    # 8-12 GB. A ~30B-A3B-class MoE with its experts offloaded to RAM.
GENEROUS = "Generous"  # 16-24 GB. The same MoE with more resident, and a longer context.
AMPLE = "Ample"        # 32 GB+ of unified memory, or a very large card.
TIERS = (MODEST, CAPABLE, GENEROUS, AMPLE)

_DESCRIPTIONS = {
    MODEST: "a small model, or CPU-only inference",
    CAPABLE: "a sparse MoE with expert tensors offloaded to system RAM",
    GENEROUS: "a sparse MoE, more layers resident, longer context",
    AMPLE: "a large MoE",
}

# Stops a console window flashing up on Windows every time a program is asked.
CREATE_NO_WINDOW = 0x0800_0000

# Where Windows keeps one registry key per display adapter.
_DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"

# Runs a program and returns what it printed, or None. Tests pass a fake.
Run = Callable[[list[str]], str | None]


@dataclass(frozen=True)
class Accelerator:
    kind: str  # CUDA, METAL, VULKAN or CPU
    name: str = ""
    # VRAM in MB, or unified memory for Apple Silicon. 0 for CPU.
    memory_mb: int = 0


@dataclass(frozen=True)
class Hardware:
    accelerator: Accelerator
    system_ram_mb: int
    tier: str


def describe_tier(tier: str) -> str:
    return _DESCRIPTIONS[tier]


def tier_below(tier: str) -> str | None:
    """The next smaller tier, or None below Modest."""
    i = TIERS.index(tier)
    return TIERS[i - 1] if i > 0 else None


def accelerator_memory_mb(a: Accelerator) -> int:
    """Usable VRAM in MB, or unified memory for Apple Silicon."""
    return 0 if a.kind == CPU else a.memory_mb


def run(args: list[str]) -> str | None:
    """Run a program and return what it printed, or None if it is missing,
    fails, or does not answer in time. Absent or failing is a normal answer
    here (most machines have no nvidia-smi), never an error."""
    try:
        out = subprocess.run(
            args, capture_output=True, stdin=subprocess.DEVNULL, timeout=15,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="replace")


def detect(platform: str = sys.platform, run: Run = run,
           meminfo: Path = Path("/proc/meminfo"),
           adapters: Callable[[], list[tuple[str, int]]] | None = None,
           vulkan: Callable[[], bool] | None = None) -> Hardware:
    accelerator = detect_accelerator(platform, run, adapters, vulkan)
    system_ram_mb = detect_system_ram_mb(platform, run, meminfo)
    return Hardware(accelerator, system_ram_mb, pick_tier(accelerator, system_ram_mb))


def pick_tier(accel: Accelerator, system_ram_mb: int) -> str:
    """Tier from available memory.

    System RAM matters as well as VRAM: expert offload only works if the
    experts have somewhere to live. A 12 GB card with 8 GB of system RAM
    cannot run the tier that makes that card worth having."""
    vram = accelerator_memory_mb(accel)

    if accel.kind == METAL:
        # Unified memory is shared with the whole system, and macOS lets the
        # GPU use only about two thirds to three quarters of it. The tiers
        # once matched the 37 GB Q8 model to a 32 GB Mac and the 22 GB Q4 to
        # a 24 GB one: neither fits beside macOS and a browser.
        if vram >= 60_000:
            return AMPLE
        if vram >= 45_000:
            return GENEROUS
        if vram >= 32_000:
            return CAPABLE
        return MODEST

    if vram >= 32_000:
        return AMPLE
    if vram >= 16_000 and system_ram_mb >= 16_000:
        return GENEROUS
    # Expert offload needs room in system RAM for the experts.
    if vram >= 8_000 and system_ram_mb >= 24_000:
        return CAPABLE
    return MODEST


def detect_accelerator(platform: str = sys.platform, run: Run = run,
                       adapters: Callable[[], list[tuple[str, int]]] | None = None,
                       vulkan: Callable[[], bool] | None = None) -> Accelerator:
    """`adapters` lists display adapters as (name, bytes of memory); by
    default they are read from the registry on Windows and from /sys on
    Linux. `vulkan` says whether a Linux system can load Vulkan."""
    if platform == "darwin":
        mb = macos_unified_memory_mb(run)
        if mb is not None:
            name = sysctl("machdep.cpu.brand_string", run) or "Apple Silicon"
            # Metal is only the right answer on Apple Silicon; Intel Macs fall
            # through to CPU, which is what they should use anyway.
            if "Apple" in name:
                return Accelerator(METAL, name, mb)
        return Accelerator(CPU)

    found = nvidia_smi(run)
    if platform == "win32":
        if found is not None:
            return Accelerator(CUDA, *found)
        found = other_gpu((adapters or windows_adapters)())
        if found is not None:
            return Accelerator(VULKAN, *found)
        return Accelerator(CPU)
    # Linux. llama.cpp publishes no CUDA build for it, so an NVIDIA card runs
    # the Vulkan build like any other, and a card is only worth counting when
    # the system can load Vulkan at all: without it that build does not start.
    if found is None:
        found = other_gpu((adapters or linux_adapters)())
    if found is not None and (vulkan or vulkan_loader)():
        return Accelerator(VULKAN, *found)
    return Accelerator(CPU)


def nvidia_smi(run: Run = run) -> tuple[str, int] | None:
    """Ask nvidia-smi. Absent or failing means no usable NVIDIA GPU, which is
    a normal answer and not an error."""
    text = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    if not text or not text.splitlines():
        return None
    name, comma, mem = text.splitlines()[0].partition(",")
    mem = mem.strip()
    if not comma or not mem.isascii() or not mem.isdigit():
        return None
    return name.strip(), int(mem)


def other_gpu(adapters: list[tuple[str, int]]) -> tuple[str, int] | None:
    """The display adapter with the most memory, as (name, MB). Not the
    first: a processor's own graphics is usually installed first, and judging
    a PC with a 16 GB Radeon by its 128 MB of integrated graphics fetched the
    smallest model.

    The Rust version asked wmic, whose AdapterRAM is a 32-bit field that wraps
    above 4 GB, so it could only establish that a GPU existed. The registry
    holds the true size in a 64-bit value on current drivers, so an AMD or
    Intel card with more than 4 GB is now judged on its real memory."""
    usable = [(size, name.strip()) for name, size in adapters if name.strip() and size > 0]
    if not usable:
        return None
    size, name = max(usable, key=lambda a: a[0])
    return name, size // 1024 // 1024


# Below this a Linux adapter is a processor's own graphics borrowing system
# memory, and the CPU build serves better.
LINUX_MIN_VRAM = 2 << 30


def linux_adapters(drm: Path = Path("/sys/class/drm")) -> list[tuple[str, int]]:
    """Graphics cards that say how much memory they have, as (name, bytes).
    The amdgpu driver does, in mem_info_vram_total; NVIDIA cards are asked
    through nvidia-smi before this is reached, and Intel's say nothing, so
    they count as no card."""
    vendors = {"0x1002": "AMD Radeon", "0x10de": "NVIDIA", "0x8086": "Intel"}
    out: list[tuple[str, int]] = []
    try:
        cards = sorted(p for p in drm.iterdir() if p.name.startswith("card") and "-" not in p.name)
    except OSError:
        return out
    for card in cards:
        try:
            size = int((card / "device" / "mem_info_vram_total").read_text().strip())
            vendor = (card / "device" / "vendor").read_text().strip().lower()
        except (OSError, ValueError):
            continue
        if size >= LINUX_MIN_VRAM:
            out.append((f"{vendors.get(vendor, 'Graphics card')} ({card.name})", size))
    return out


def vulkan_loader() -> bool:
    """Can this Linux system load Vulkan? The Vulkan build of llama.cpp needs
    libvulkan.so.1, which comes with a desktop's graphics drivers."""
    try:
        ctypes.CDLL("libvulkan.so.1")
        return True
    except OSError:
        return False


def windows_adapters() -> list[tuple[str, int]]:
    """Display adapters from the registry, as (name, bytes of memory).

    wmic, which the Rust version used, is deprecated and absent on current
    Windows 11 builds. The registry needs no process and no WMI."""
    import winreg

    found = []
    try:
        cls = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS)
    except OSError:
        return found
    with cls:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(cls, i)
            except OSError:
                break
            i += 1
            if not sub.isdigit():  # "Properties" and the like are not adapters
                continue
            try:
                with winreg.OpenKey(cls, sub) as key:
                    name = _reg_value(key, "DriverDesc")
                    size = _reg_value(key, "HardwareInformation.qwMemorySize")
                    if size is None:
                        size = _reg_value(key, "HardwareInformation.MemorySize")
            except OSError:
                continue
            if isinstance(size, bytes):  # some drivers store it as raw bytes
                size = int.from_bytes(size[:8], "little")
            if isinstance(name, str) and isinstance(size, int):
                found.append((name, size))
    return found


def _reg_value(key, name: str):
    import winreg

    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def macos_unified_memory_mb(run: Run = run) -> int | None:
    v = sysctl("hw.memsize", run)
    return int(v) // 1024 // 1024 if v and v.isascii() and v.isdigit() else None


def sysctl(key: str, run: Run = run) -> str | None:
    out = run(["sysctl", "-n", key])
    return None if out is None else out.strip()


def detect_system_ram_mb(platform: str = sys.platform, run: Run = run,
                         meminfo: Path = Path("/proc/meminfo")) -> int:
    if platform == "win32":
        return windows_ram_mb()
    v = sysctl("hw.memsize", run)
    if v and v.isascii() and v.isdigit():
        return int(v) // 1024 // 1024
    try:
        lines = meminfo.read_text(errors="replace").splitlines()
    except OSError:
        return 0
    for line in lines:
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) > 1 and parts[1].isascii() and parts[1].isdigit():
                return int(parts[1]) // 1024
            return 0
    return 0


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("memory_load", ctypes.c_uint32),
        ("total_phys", ctypes.c_uint64),
        ("avail_phys", ctypes.c_uint64),
        ("total_page_file", ctypes.c_uint64),
        ("avail_page_file", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64),
        ("avail_virtual", ctypes.c_uint64),
        ("avail_extended_virtual", ctypes.c_uint64),
    ]


def windows_ram_mb() -> int:
    """System RAM straight from the Win32 API.

    This used to shell out to `wmic`, which reported 65 GB from the command
    line and 0 MB from inside the app -- dropping a 12 GB card to the Modest
    tier and fetching the wrong model. wmic is also deprecated and absent on
    current Windows 11 builds, and writes UTF-16 to a pipe in some contexts.
    GlobalMemoryStatusEx has none of those problems and needs no process."""
    m = _MemoryStatusEx()
    # The call requires only that the length field is set.
    m.length = ctypes.sizeof(_MemoryStatusEx)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
    kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
    if kernel32.GlobalMemoryStatusEx(ctypes.byref(m)) == 0:
        return 0
    return m.total_phys // 1024 // 1024
