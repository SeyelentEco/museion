"""Hardware detection and tiers. The tier logic is tested directly; detection
is tested with a fake `run` standing in for nvidia-smi and sysctl, so every
platform's path is exercised on whatever machine runs the tests."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from museion import hardware
from museion.hardware import AMPLE, CAPABLE, GENEROUS, MODEST, Accelerator


def fake_run(answers: dict[str, str]):
    """A `run` that answers by command line, and fails for anything else, as a
    missing program would."""
    asked = []

    def run(args: list[str]) -> str | None:
        asked.append(" ".join(args))
        return answers.get(" ".join(args))

    run.asked = asked
    return run


NVIDIA_SMI = "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits"
NO_MEMINFO = Path("no-such-meminfo")


class TierTest(unittest.TestCase):
    def test_expert_offload_needs_system_ram_not_just_vram(self):
        card = Accelerator(hardware.CUDA, "RTX 3060", 12_288)
        # The whole point of the Capable tier is holding expert tensors in RAM.
        self.assertEqual(hardware.pick_tier(card, 64_000), CAPABLE)
        # Same card, not enough RAM to hold them: do not promise what it cannot do.
        self.assertEqual(hardware.pick_tier(card, 8_000), MODEST)

    def test_a_machine_with_no_gpu_still_gets_a_tier(self):
        self.assertEqual(hardware.pick_tier(Accelerator(hardware.CPU), 32_000), MODEST)

    def test_apple_silicon_is_judged_on_unified_memory(self):
        def m(mb):
            return Accelerator(hardware.METAL, "Apple M3", mb)
        self.assertEqual(hardware.pick_tier(m(8_000), 8_000), MODEST)
        self.assertEqual(hardware.pick_tier(m(18_000), 18_000), MODEST)
        self.assertEqual(hardware.pick_tier(m(36_864), 36_864), CAPABLE)
        self.assertEqual(hardware.pick_tier(m(64_000), 64_000), AMPLE)

    def test_gpu_tier_boundaries(self):
        cases = [
            # (VRAM, system RAM, tier)
            (32_000, 0, AMPLE),
            (31_999, 64_000, GENEROUS),
            (16_000, 16_000, GENEROUS),
            (16_000, 15_999, MODEST),
            (16_000, 24_000, GENEROUS),
            (15_999, 24_000, CAPABLE),
            (8_000, 24_000, CAPABLE),
            (8_000, 23_999, MODEST),
            (7_999, 64_000, MODEST),
            (0, 64_000, MODEST),
        ]
        for kind in (hardware.CUDA, hardware.VULKAN):
            for vram, ram, tier in cases:
                with self.subTest(kind=kind, vram=vram, ram=ram):
                    self.assertEqual(hardware.pick_tier(Accelerator(kind, "card", vram), ram), tier)

    def test_unified_memory_boundaries_ignore_system_ram(self):
        for mb, tier in [(65_536, AMPLE), (60_000, AMPLE), (49_152, GENEROUS), (36_864, CAPABLE),
                         (32_768, CAPABLE), (24_576, MODEST), (16_384, MODEST)]:
            with self.subTest(mb=mb):
                self.assertEqual(hardware.pick_tier(Accelerator(hardware.METAL, "Apple M2", mb), 0), tier)

    def test_cpu_memory_is_never_counted(self):
        self.assertEqual(hardware.accelerator_memory_mb(Accelerator(hardware.CPU, "", 64_000)), 0)
        self.assertEqual(hardware.pick_tier(Accelerator(hardware.CPU, "", 64_000), 64_000), MODEST)

    def test_tiers_are_ordered_and_described(self):
        self.assertEqual(hardware.TIERS, (MODEST, CAPABLE, GENEROUS, AMPLE))
        self.assertEqual(hardware.tier_below(AMPLE), GENEROUS)
        self.assertEqual(hardware.tier_below(CAPABLE), MODEST)
        self.assertIsNone(hardware.tier_below(MODEST))
        self.assertEqual(hardware.describe_tier(CAPABLE),
                         "a sparse MoE with expert tensors offloaded to system RAM")
        for tier in hardware.TIERS:
            self.assertTrue(hardware.describe_tier(tier))


class DetectTest(unittest.TestCase):
    def test_nvidia_card_on_linux(self):
        with tempfile.TemporaryDirectory() as tmp:
            meminfo = Path(tmp, "meminfo")
            meminfo.write_text("MemTotal:       65536000 kB\nMemFree:  1 kB\n")
            run = fake_run({NVIDIA_SMI: "NVIDIA GeForce RTX 3060, 12288\nNVIDIA T400, 2048\n"})
            hw = hardware.detect("linux", run, meminfo)
        self.assertEqual(hw.accelerator, Accelerator(hardware.CUDA, "NVIDIA GeForce RTX 3060", 12_288))
        self.assertEqual(hw.system_ram_mb, 64_000)
        self.assertEqual(hw.tier, CAPABLE)

    def test_no_gpu_on_linux_is_cpu(self):
        hw = hardware.detect("linux", fake_run({}), NO_MEMINFO)
        self.assertEqual(hw.accelerator, Accelerator(hardware.CPU))
        self.assertEqual(hw.system_ram_mb, 0)
        self.assertEqual(hw.tier, MODEST)

    def test_nvidia_smi_that_prints_nonsense_is_no_gpu(self):
        for out in ["", "\n", "NVIDIA GeForce RTX 3060\n", "NVIDIA GeForce RTX 3060, [N/A]\n"]:
            with self.subTest(out=out):
                self.assertIsNone(hardware.nvidia_smi(fake_run({NVIDIA_SMI: out})))

    def test_apple_silicon(self):
        run = fake_run({
            "sysctl -n hw.memsize": "17179869184\n",
            "sysctl -n machdep.cpu.brand_string": "Apple M3\n",
        })
        hw = hardware.detect("darwin", run, NO_MEMINFO)
        self.assertEqual(hw.accelerator, Accelerator(hardware.METAL, "Apple M3", 16_384))
        self.assertEqual(hw.system_ram_mb, 16_384)
        # The 22 GB model does not fit in 16 GB shared with macOS.
        self.assertEqual(hw.tier, MODEST)
        # A Mac never asks for an NVIDIA card.
        self.assertNotIn(NVIDIA_SMI, run.asked)

    def test_a_mac_with_no_brand_string_is_taken_for_apple_silicon(self):
        accel = hardware.detect_accelerator("darwin", fake_run({"sysctl -n hw.memsize": "8589934592"}))
        self.assertEqual(accel, Accelerator(hardware.METAL, "Apple Silicon", 8_192))

    def test_intel_mac_uses_the_cpu(self):
        run = fake_run({
            "sysctl -n hw.memsize": "34359738368\n",
            "sysctl -n machdep.cpu.brand_string": "Intel(R) Core(TM) i9-9880H CPU @ 2.30GHz\n",
        })
        self.assertEqual(hardware.detect_accelerator("darwin", run), Accelerator(hardware.CPU))

    def test_windows_falls_back_to_other_gpus(self):
        adapters = [
            ("Microsoft Basic Display Adapter", 0),
            ("AMD Radeon RX 7800 XT", 16 * 1024**3),
            ("Intel(R) UHD Graphics 770", 128 * 1024**2),
        ]
        accel = hardware.detect_accelerator("win32", fake_run({}), lambda: adapters)
        self.assertEqual(accel, Accelerator(hardware.VULKAN, "AMD Radeon RX 7800 XT", 16_384))

    def test_windows_judges_by_the_largest_adapter_not_the_first(self):
        adapters = [("Intel(R) UHD Graphics 770", 128 * 1024**2), ("AMD Radeon RX 7800 XT", 16 * 1024**3)]
        accel = hardware.detect_accelerator("win32", fake_run({}), lambda: adapters)
        self.assertEqual(accel, Accelerator(hardware.VULKAN, "AMD Radeon RX 7800 XT", 16_384))

    def test_windows_prefers_cuda(self):
        run = fake_run({NVIDIA_SMI: "NVIDIA GeForce RTX 4090, 24564\r\n"})
        accel = hardware.detect_accelerator("win32", run, lambda: [("AMD Radeon", 8 * 1024**3)])
        self.assertEqual(accel, Accelerator(hardware.CUDA, "NVIDIA GeForce RTX 4090", 24_564))

    def test_windows_with_no_gpu_worth_using(self):
        accel = hardware.detect_accelerator("win32", fake_run({}), lambda: [("Microsoft Basic Display Adapter", 0)])
        self.assertEqual(accel, Accelerator(hardware.CPU))

    def test_other_gpus_are_looked_for_only_on_windows(self):
        called = []
        accel = hardware.detect_accelerator("linux", fake_run({}), lambda: called.append(1) or [("AMD", 1)])
        self.assertEqual(accel, Accelerator(hardware.CPU))
        self.assertEqual(called, [])

    def test_windows_adapters_are_read_from_the_registry(self):
        # A stand-in for winreg holding the display adapter class key: a 64-bit
        # size, a driver that keeps only the 32-bit one, one that stores it as
        # bytes, and a subkey that is not an adapter at all.
        keys = {
            "0000": {"DriverDesc": "AMD Radeon RX 7800 XT",
                     "HardwareInformation.qwMemorySize": 16 * 1024**3,
                     "HardwareInformation.MemorySize": 0},
            "0001": {"DriverDesc": "Intel(R) UHD Graphics", "HardwareInformation.MemorySize": 128 * 1024**2},
            "0002": {"DriverDesc": "Old card", "HardwareInformation.MemorySize": (2 * 1024**3).to_bytes(4, "little")},
            "0003": {"DriverDesc": "Microsoft Basic Display Adapter"},
            "Properties": {},
        }

        class Key:
            def __init__(self, values):
                self.values = values

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeWinreg:
            HKEY_LOCAL_MACHINE = object()

            @staticmethod
            def OpenKey(parent, sub):
                if parent is FakeWinreg.HKEY_LOCAL_MACHINE:
                    return Key(keys)
                if sub == "Properties":
                    raise PermissionError("access denied")
                return Key(parent.values[sub])

            @staticmethod
            def EnumKey(key, i):
                if i >= len(key.values):
                    raise OSError("no more keys")
                return list(key.values)[i]

            @staticmethod
            def QueryValueEx(key, name):
                if name not in key.values:
                    raise FileNotFoundError(name)
                return key.values[name], 0

        with mock.patch.dict(sys.modules, {"winreg": FakeWinreg}):
            adapters = hardware.windows_adapters()
        self.assertEqual(adapters, [
            ("AMD Radeon RX 7800 XT", 16 * 1024**3),
            ("Intel(R) UHD Graphics", 128 * 1024**2),
            ("Old card", 2 * 1024**3),
        ])
        self.assertEqual(hardware.other_gpu(adapters), ("AMD Radeon RX 7800 XT", 16_384))

    def test_system_ram_prefers_sysctl_then_meminfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            meminfo = Path(tmp, "meminfo")
            meminfo.write_text("MemFree:  1 kB\nMemTotal:  16384000 kB\n")
            self.assertEqual(hardware.detect_system_ram_mb("linux", fake_run({}), meminfo), 16_000)
            sysctl = fake_run({"sysctl -n hw.memsize": "68719476736\n"})
            self.assertEqual(hardware.detect_system_ram_mb("linux", sysctl, meminfo), 65_536)
            meminfo.write_text("MemTotal: lots\n")
            self.assertEqual(hardware.detect_system_ram_mb("linux", fake_run({}), meminfo), 0)


class ThisMachineTest(unittest.TestCase):
    def test_run_returns_output_or_none(self):
        self.assertEqual(hardware.run([sys.executable, "-c", "print('hi')"]).strip(), "hi")
        self.assertIsNone(hardware.run([sys.executable, "-c", "import sys; sys.exit(1)"]))
        self.assertIsNone(hardware.run(["no-such-program-anywhere"]))

    def test_detect_gives_a_tier(self):
        hw = hardware.detect()
        self.assertIn(hw.tier, hardware.TIERS)
        self.assertIn(hw.accelerator.kind, (hardware.CUDA, hardware.METAL, hardware.VULKAN, hardware.CPU))
        self.assertGreater(hw.system_ram_mb, 0)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows_ram_and_adapters(self):
        self.assertGreater(hardware.windows_ram_mb(), 0)
        for name, size in hardware.windows_adapters():
            self.assertIsInstance(name, str)
            self.assertIsInstance(size, int)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows_ram_agrees_with_the_system(self):
        # The figure wmic got wrong from inside the app, checked against
        # PowerShell's own (which has no 32-bit wrap or pipe problem).
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
            capture_output=True, text=True, creationflags=hardware.CREATE_NO_WINDOW,
        ).stdout.strip()
        if not out.isdigit():
            self.skipTest("PowerShell did not answer")
        self.assertLess(abs(hardware.windows_ram_mb() - int(out) // 1024 // 1024), 1024)


if __name__ == "__main__":
    unittest.main()
