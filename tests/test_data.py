"""The TOML the Python app reads must hold exactly what the YAML the Rust app
reads holds. scripts/yaml-to-toml.mjs writes one from the other, and its
--check only proves the TOML is what the script writes today; this test reads
both sides and compares the data, so a converter mistake that changes a value
(a prompt losing its last newline, a version read as a date, true becoming 1)
fails here.

The YAML side is read by Node with the repo's own `yaml` package, the parser
the converter uses, so this needs node and `npm ci`; it is skipped without
them."""

import json
import shutil
import subprocess
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "yaml-to-toml.mjs"
DATA_DIRS = ["catalog/sources", "catalog/bundles", "models", "tools"]


def same(yaml_side, toml_side) -> bool:
    """Equal, and of the same type: Python's own == lets True equal 1."""
    a, b = yaml_side, toml_side
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        # JSON writes 2.0 as 2, so a whole number from the YAML side may
        # rightly be a float in the TOML.
        return a == b and (type(a) is type(b) or float(a).is_integer())
    return type(a) is type(b) and a == b


@unittest.skipUnless(shutil.which("node") and (ROOT / "node_modules" / "yaml").is_dir(),
                     "needs node and the repo's node_modules (npm ci)")
class TomlMatchesYamlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(SCRIPT), "--json"], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8", check=True).stdout
        cls.pairs = json.loads(out)

    def test_every_toml_holds_what_its_yaml_holds(self):
        self.assertGreater(len(self.pairs), 20)
        for yaml_path, entry in self.pairs.items():
            with self.subTest(yaml_path):
                toml_path = ROOT / entry["toml"]
                self.assertTrue(toml_path.is_file(), f"{entry['toml']} is missing; run node {SCRIPT.relative_to(ROOT)}")
                with open(toml_path, "rb") as f:
                    toml_data = tomllib.load(f)
                if not same(entry["data"], toml_data):
                    self.assertEqual(toml_data, entry["data"], f"{entry['toml']} differs from {yaml_path}")
                    self.fail(f"{entry['toml']} differs from {yaml_path} in a value's type")

    def test_no_toml_without_a_yaml(self):
        # Python would read it and Rust would not.
        generated = {entry["toml"] for entry in self.pairs.values()}
        for d in DATA_DIRS:
            for p in sorted((ROOT / d).glob("*.toml")):
                self.assertIn(f"{d}/{p.name}", generated, f"{d}/{p.name} has no YAML")

    def test_the_prompts_come_across_exactly(self):
        # The block scalars are the riskiest part of the conversion.
        tuning = self.pairs["crates/museion-core/tuning.yaml"]
        self.assertEqual(tuning["toml"], "museion/tuning.toml")
        with open(ROOT / tuning["toml"], "rb") as f:
            toml_data = tomllib.load(f)
        for key in ("system_prompt", "rewrite_prompt"):
            self.assertEqual(toml_data[key], tuning["data"][key])
            self.assertTrue(toml_data[key].endswith(".\n"), key)


if __name__ == "__main__":
    unittest.main()
