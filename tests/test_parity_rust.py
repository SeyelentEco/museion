"""The Python ask loop against the Rust one: both command lines ask the same
question of the same index, with the fake llama-server playing the same
script to each, and must send the model exactly the same requests and print
exactly the same answer.

Runs when MUSEION_RUST names the Rust `museion` program (built with
`cargo build -p museion-core --bin museion`), so only until the Rust code is
retired. The index is the one the Rust indexer built, tests/fixtures/rust-index.db.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from fake_llama import FakeLlama, call_fragment, text
from museion import sidecar

RUST = os.environ.get("MUSEION_RUST")
REPO = Path(__file__).resolve().parent.parent
DB = REPO / "tests" / "fixtures" / "rust-index.db"
QUESTION = "how much water should I store per person"

# What the model says, one entry per chat request. The first answers the
# request to rewrite the question into searches.
REWRITE = [text("water purification\nboiling water")]
SCENARIOS = {
    "answers citing a passage": [REWRITE, [text("Boil it for one minute [1].")]],
    "calculates, then answers": [
        REWRITE,
        [call_fragment(0, id="c0", name="calculate", arguments='{"expression": "convert(0.25 * 55, tsp, ml)"}')],
        [text("Add 67.77 ml [2].")],
    ],
    "searches, forgets to cite, is asked to": [
        REWRITE,
        [call_fragment(0, id="c0", name="search_library", arguments='{"query": "storing water"}')],
        [text("Store it.")],
        [text("Store it [3].")],
    ],
    "does sums in prose and runs out of tokens": [
        REWRITE,
        [text("4 people × 2 litres = 8 litres", finish="length")],
        [text("per day [1].")],
        [text("Use 8 litres [1].")],
    ],
    # Two documents in one archive both have a page 2; the Rust version once
    # gave them one number between them.
    "cites the second of two page 2s": [REWRITE, [text("Process pints for twenty minutes [7].")]],
}


@unittest.skipUnless(RUST, "set MUSEION_RUST to the Rust museion program")
class RustParityTest(unittest.TestCase):
    def ask(self, program: list[str], script: list[list[dict]], rerank: bool) -> tuple[list[dict], str]:
        fake = FakeLlama()
        try:
            fake.script = [list(chunks) for chunks in script]
            fake.rerank_scores = [0.9 - i * 0.02 for i in range(40)]
            # The question goes first: the Rust parser reads the word after a
            # bare flag like --no-rerank as that flag's value.
            args = program + ["ask", QUESTION, "--db", str(DB), "--llama", fake.url,
                              "--kiwix", f"http://127.0.0.1:{sidecar.free_port()}"]
            args += ["--reranker", fake.url] if rerank else ["--no-rerank"]
            out = subprocess.run(args, cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120)
            return fake.requests, out.stdout
        finally:
            fake.close()

    def test_python_asks_and_answers_exactly_as_rust_does(self):
        for name, script in SCENARIOS.items():
            for rerank in (False, True):
                with self.subTest(name, rerank=rerank):
                    rust_requests, rust_out = self.ask([RUST], script, rerank)
                    python_requests, python_out = self.ask([sys.executable, "-m", "museion"], script, rerank)
                    self.assertGreater(len(rust_requests), 1, rust_out)
                    self.assertEqual(len(python_requests), len(rust_requests))
                    for i, (r, p) in enumerate(zip(rust_requests, python_requests)):
                        self.assertEqual(p, r, f"request {i}:\n{json.dumps(p, indent=1)[:3000]}")
                    self.assertEqual(python_out, rust_out)


if __name__ == "__main__":
    unittest.main()
