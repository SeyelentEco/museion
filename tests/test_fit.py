"""Fitting a model to the GPU. A small Python module stands in for
llama-fit-params, run as `python -m <module>`, which is the shape of the
command Museion builds (`<exe> -m <model> ...`) with Python as the exe and the
module name as the model."""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from museion import fit

FAKE_FIT_PARAMS = textwrap.dedent(r'''
    import os, sys
    with open(os.environ["FAKE_FIT_ARGS"], "w") as f:
        f.write("\n".join(sys.argv[1:]))
    print("llama_model_loader: loaded meta data", file=sys.stderr)
    mode = os.environ["FAKE_FIT_MODE"]
    if mode == "ok":
        print("fitting to 1 device")
        print("-ngl 99")
        print('  -c 32768 -ngl 41 -ot "blk\.15\.ffn_down.*=CPU,blk\.16\.ffn_(up|down)_exps=CPU"  ')
    elif mode == "fail":
        for i in range(10):
            print(f"stderr line {i}", file=sys.stderr)
        sys.exit(3)
    else:
        print("no arguments here")
''')


class SplitShellWordsTest(unittest.TestCase):
    def test_keeps_the_quoted_regex_as_one_argument(self):
        line = r'-c 32768 -ngl 41 -ot "blk\.15\.ffn_down.*=CPU,blk\.16\.ffn_(up|down)_exps=CPU"'
        args = fit.split_shell_words(line)
        self.assertEqual(len(args), 6)
        self.assertEqual(args[4], "-ot")
        self.assertEqual(args[5], r"blk\.15\.ffn_down.*=CPU,blk\.16\.ffn_(up|down)_exps=CPU")

    def test_empty_and_whitespace_only_lines_yield_nothing(self):
        self.assertEqual(fit.split_shell_words(""), [])
        self.assertEqual(fit.split_shell_words("   "), [])

    def test_quotes_join_and_an_empty_pair_is_an_argument(self):
        self.assertEqual(fit.split_shell_words('a"b c"d "" e'), ["ab cd", "", "e"])
        self.assertEqual(fit.split_shell_words(r"x\.y  z"), [r"x\.y", "z"])


class ParseTest(unittest.TestCase):
    def test_takes_the_last_argument_line(self):
        f = fit.parse_fit_output("loading\n-ngl 99\n  -ngl 41 -c 8192\nbye\n")
        self.assertEqual(f.args, ["-ngl", "41", "-c", "8192"])
        self.assertEqual(f.gpu_layers, 41)

    def test_long_flag_and_unknown_layer_counts(self):
        self.assertEqual(fit.parse_fit_output("--n-gpu-layers 12").gpu_layers, 12)
        self.assertIsNone(fit.parse_fit_output("-ngl auto").gpu_layers)
        self.assertIsNone(fit.parse_fit_output("-ngl").gpu_layers)
        self.assertIsNone(fit.parse_fit_output("-c 4096").gpu_layers)
        # Only the first -ngl counts, even when it is not a number.
        self.assertIsNone(fit.parse_fit_output("-ngl all -ngl 7").gpu_layers)

    def test_no_argument_line_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "printed no arguments"):
            fit.parse_fit_output("nothing\n")


class FitModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, "fake_fit_params.py").write_text(FAKE_FIT_PARAMS)
        self.args_file = Path(self.tmp.name, "args.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def run_fake(self, mode: str) -> fit.Fit:
        env = {"PYTHONPATH": self.tmp.name, "FAKE_FIT_MODE": mode, "FAKE_FIT_ARGS": str(self.args_file)}
        with mock.patch.dict(os.environ, env):
            return fit.fit_model(Path(sys.executable), Path("fake_fit_params"), 32768)

    def test_runs_with_the_kv_cache_museion_uses(self):
        f = self.run_fake("ok")
        self.assertEqual(self.args_file.read_text().split("\n"),
                         ["-c", "32768", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0"])
        self.assertEqual(f.gpu_layers, 41)
        self.assertEqual(f.args[:4], ["-c", "32768", "-ngl", "41"])
        self.assertEqual(f.args[5], r"blk\.15\.ffn_down.*=CPU,blk\.16\.ffn_(up|down)_exps=CPU")

    def test_a_failure_shows_the_end_of_stderr(self):
        with self.assertRaises(RuntimeError) as caught:
            self.run_fake("fail")
        message = str(caught.exception)
        self.assertTrue(message.startswith("llama-fit-params failed (exit status: 3):\n"), message)
        self.assertEqual(message.split("\n")[1:], [f"stderr line {i}" for i in range(4, 10)])

    def test_no_arguments_printed(self):
        with self.assertRaisesRegex(RuntimeError, "printed no arguments"):
            self.run_fake("silent")

    def test_a_missing_program_says_where_it_looked(self):
        with self.assertRaisesRegex(FileNotFoundError, "llama-fit-params not found at .*nowhere"):
            fit.fit_model(Path(self.tmp.name, "nowhere", "llama-fit-params"), Path("m.gguf"), 4096)


if __name__ == "__main__":
    unittest.main()
