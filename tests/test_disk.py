"""Free space and folder sizes, on the real file system."""

import os
import tempfile
import unittest
from pathlib import Path

from museion import disk


class DiskTest(unittest.TestCase):
    def test_free_space_of_the_temp_dir_is_a_real_number(self):
        self.assertGreater(disk.free_space(Path(tempfile.gettempdir())), 0)

    def test_free_space_for_a_folder_that_does_not_exist_yet_uses_its_parent(self):
        p = Path(tempfile.gettempdir(), "museion-not-created-yet", "deeper")
        self.assertGreater(disk.free_space(p), 0)

    def test_free_space_of_a_drive_that_does_not_exist(self):
        # Walking up from a missing drive reaches its root, which has no
        # parent. On macOS and Linux "Q:" is just a relative folder name.
        if os.name != "nt" or Path("Q:/").exists():
            self.skipTest("needs Windows with no Q: drive")
        with self.assertRaisesRegex(OSError, "no existing ancestor"):
            disk.free_space(Path("Q:/museion-library"))

    def test_dir_size_counts_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "sub").mkdir()
            (d / "a.bin").write_bytes(bytes(1000))
            (d / "sub" / "b.bin").write_bytes(bytes(500))
            self.assertEqual(disk.dir_size(d), 1500)

    def test_dir_size_stops_four_levels_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp, "1", "2", "3")
            deep.mkdir(parents=True)
            (deep / "counted.bin").write_bytes(bytes(10))
            (deep / "4").mkdir()
            (deep / "4" / "ignored.bin").write_bytes(bytes(1000))
            self.assertEqual(disk.dir_size(Path(tmp)), 10)

    def test_dir_size_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            Path(elsewhere, "big.bin").write_bytes(bytes(5000))
            Path(tmp, "small.bin").write_bytes(bytes(7))
            try:
                os.symlink(elsewhere, Path(tmp, "link"), target_is_directory=True)
                os.symlink(Path(elsewhere, "big.bin"), Path(tmp, "big-link.bin"))
            except OSError:
                self.skipTest("no permission to make symlinks here")
            self.assertEqual(disk.dir_size(Path(tmp)), 7)

    def test_dir_size_of_a_missing_folder_is_zero(self):
        self.assertEqual(disk.dir_size(Path(tempfile.gettempdir(), "museion-no-such-folder")), 0)

    def test_volume_names(self):
        self.assertEqual(disk.volume_name(Path(r"E:\museion-library")), "E:")
        self.assertEqual(disk.volume_name(Path("/home/x")), "/")
        self.assertEqual(disk.volume_name("e:/museion"), "E:")


if __name__ == "__main__":
    unittest.main()
