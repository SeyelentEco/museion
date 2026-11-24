"""Merging a person's changes to a file with a new version's."""

import random
import unittest

from museion import merge3

BASE = ["def a():", "    return 1", "", "def b():", "    return 2", "", "def c():", "    return 3"]


class MergeTest(unittest.TestCase):
    def test_changes_in_different_places_both_land(self):
        mine = [*BASE[:1], "    return 10", *BASE[2:]]
        new = [*BASE[:7], "    return 30", "", "def d():", "    return 4"]
        self.assertEqual(merge3.merge(BASE, mine, new),
                         ["def a():", "    return 10", "", "def b():", "    return 2", "", "def c():", "    return 30",
                          "", "def d():", "    return 4"])

    def test_one_side_unchanged_gives_the_other(self):
        changed = [*BASE, "", "# more"]
        self.assertEqual(merge3.merge(BASE, BASE, changed), changed)
        self.assertEqual(merge3.merge(BASE, changed, BASE), changed)
        self.assertEqual(merge3.merge(BASE, BASE, BASE), BASE)

    def test_the_same_change_on_both_sides_is_taken_once(self):
        both = [*BASE[:4], "    return 22", *BASE[5:]]
        self.assertEqual(merge3.merge(BASE, both, both), both)

    def test_different_changes_to_the_same_lines_collide(self):
        mine = [*BASE[:4], "    return 20", *BASE[5:]]
        new = [*BASE[:4], "    return 200", *BASE[5:]]
        self.assertIsNone(merge3.merge(BASE, mine, new))

    def test_a_deletion_meets_an_edit_elsewhere(self):
        mine = BASE[3:]  # the first function removed
        new = [*BASE[:7], "    return 33"]
        self.assertEqual(merge3.merge(BASE, mine, new), [*BASE[3:7], "    return 33"])

    def test_insertions_at_the_same_place_collide(self):
        self.assertIsNone(merge3.merge(BASE, [*BASE, "# mine"], [*BASE, "# theirs"]))

    def test_empty_files(self):
        self.assertEqual(merge3.merge([], [], ["x"]), ["x"])
        self.assertEqual(merge3.merge([], ["y"], []), ["y"])
        self.assertIsNone(merge3.merge([], ["y"], ["x"]))

    def test_a_merge_never_loses_a_one_sided_change(self):
        # Many random edits to separate halves of a file: the merge must be
        # exactly my half followed by their half.
        rng = random.Random(7)
        for _ in range(200):
            top = [f"top {i}" for i in range(rng.randint(3, 12))]
            bottom = [f"bottom {i}" for i in range(rng.randint(3, 12))]
            middle = ["# ---- middle ----", "# ---- still middle ----"]

            def edit(lines):
                lines = list(lines)
                for _ in range(rng.randint(1, 4)):
                    at = rng.randrange(len(lines) + 1)
                    if lines and rng.random() < 0.4:
                        del lines[min(at, len(lines) - 1)]
                    else:
                        lines.insert(at, f"edit {rng.random()}")
                return lines

            mine_top, new_bottom = edit(top), edit(bottom)
            base = [*top, *middle, *bottom]
            self.assertEqual(merge3.merge(base, [*mine_top, *middle, *bottom], [*top, *middle, *new_bottom]),
                             [*mine_top, *middle, *new_bottom])


class MergeTextTest(unittest.TestCase):
    def test_bytes_in_bytes_out_with_the_new_line_endings(self):
        base = b"one\ntwo\nthree\n"
        mine = b"ONE\r\ntwo\r\nthree\r\n"  # an editor on Windows rewrote every ending
        new = b"one\ntwo\nthree\nfour\n"
        self.assertEqual(merge3.merge_text(base, mine, new), b"ONE\ntwo\nthree\nfour\n")

    def test_collisions_and_binary_files_are_none(self):
        self.assertIsNone(merge3.merge_text(b"a\n", b"b\n", b"c\n"))
        self.assertIsNone(merge3.merge_text(b"\xff\xfe", b"\xff\xfe\x00", b"\xff"))

    def test_line_endings_alone_are_not_a_change(self):
        self.assertTrue(merge3.same_text(b"a\nb\n", b"a\r\nb\r\n"))
        self.assertFalse(merge3.same_text(b"a\nb\n", b"a\nB\n"))
        self.assertTrue(merge3.same_text(b"\xff", b"\xff"))
        self.assertFalse(merge3.same_text(b"\xff", b"\xfe"))
