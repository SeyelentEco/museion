"""Agents as files: the shipped ones load cleanly, and settings layer by
tier, by model and by the person's own copy."""

import tempfile
import unittest
from pathlib import Path

from museion import agents, hardware
from museion.agent_run import load_cases


def write(folder: Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


class ShippedAgentsTest(unittest.TestCase):
    """Run after any change under agents/: every agent and its cases load."""

    def test_every_shipped_agent_loads_without_problems(self):
        specs = agents.load_all(None, hardware.CAPABLE)
        self.assertIn("librarian", [s.id for s in specs])
        for spec in specs:
            with self.subTest(spec.id):
                self.assertEqual(spec.problems, [])
                self.assertTrue(load_cases(spec), f"{spec.id} has no test cases")

    def test_every_tier_resolves(self):
        for tier in hardware.TIERS:
            for spec in agents.load_all(None, tier):
                with self.subTest(f"{spec.id} on {tier}"):
                    self.assertEqual(spec.problems, [])

    def test_shared_rules_come_first(self):
        spec = agents.load("water-planner", None, hardware.CAPABLE)
        self.assertTrue(spec.prompt.startswith("You work inside Museion"))
        self.assertIn("You are the water planner", spec.prompt)

    def test_the_librarian_is_the_ask_tab(self):
        spec = agents.load("librarian", None, hardware.CAPABLE)
        self.assertEqual(spec.tools, ["search_library", "calculate"])
        self.assertTrue(spec.seed_search and spec.arithmetic_shortcut)
        self.assertEqual(spec.prompt, "")  # it comes from tuning.toml
        self.assertEqual(spec.cases, "benchmark")


class LayeringTest(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.shipped, self.mine = root / "shipped", root / "mine"
        write(self.shipped, agents.RULES_FILE, "RULES")
        write(self.shipped / "a", "agent.toml", '''
name = "A"
goal = "Do a."
tools = ["calculate"]
max_rounds = 4
[tier.Generous]
max_rounds = 10
[model."qwen3.6-35b"]
answer_tokens = 900
''')
        write(self.shipped / "a", "prompt.md", "Shipped prompt.")
        write(self.shipped / "a", "prompt.Modest.md", "Short prompt.")

    def load(self, tier=hardware.CAPABLE, model=""):
        return agents.load("a", self.mine, tier, model, self.shipped)

    def test_tier_table(self):
        self.assertEqual(self.load().max_rounds, 4)
        self.assertEqual(self.load(hardware.GENEROUS).max_rounds, 10)

    def test_model_table_matches_part_of_the_file_name(self):
        self.assertEqual(self.load(model="Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf").answer_tokens, 900)
        self.assertEqual(self.load(model="gemma.gguf").answer_tokens, 1500)

    def test_prompt_for_a_tier(self):
        self.assertEqual(self.load(hardware.MODEST).prompt, "RULES\n\nShort prompt.")
        self.assertEqual(self.load().prompt, "RULES\n\nShipped prompt.")

    def test_the_persons_copy_wins_field_by_field(self):
        write(self.mine / "a", "agent.toml", 'max_rounds = 6\nshared_rules = false\n[tier.Generous]\nmax_rounds = 20\n')
        write(self.mine / "a", "prompt.md", "My prompt.")
        spec = self.load()
        self.assertEqual(spec.max_rounds, 6)
        self.assertEqual(spec.tools, ["calculate"])  # kept from the shipped file
        self.assertEqual(spec.prompt, "RULES\n\nMy prompt.")  # the shared rules stay
        self.assertIn("shared_rules", spec.problems[0])
        self.assertEqual(self.load(hardware.GENEROUS).max_rounds, 20)

    def test_a_persons_own_agent(self):
        write(self.mine / "b", "agent.toml", 'goal = "Mine."\ntools = ["notes"]\n')
        write(self.mine / "b", "prompt.md", "Hello.")
        ids = [s.id for s in agents.load_all(self.mine, hardware.CAPABLE, "", self.shipped)]
        self.assertEqual(ids, ["a", "b"])

    def test_problems_are_reported_not_raised(self):
        write(self.mine / "a", "agent.toml", 'tools = ["shell"]\nguards = ["nope"]\nmax_rouds = 3\nmemory = "forever"\n')
        problems = " | ".join(self.load().problems)
        for part in ("no tool is called shell", "no guard is called nope", "unknown setting 'max_rouds'", "memory"):
            self.assertIn(part, problems)

    def test_locked_settings_stay_locked_inside_tier_and_model_tables(self):
        write(self.mine / "a", "agent.toml",
              '[tier.Capable]\nshared_rules = false\n[model."qwen3.6-35b"]\nprompt_from = "tuning"\n')
        spec = self.load(model="Qwen3.6-35B-A3B-Q4_K_M.gguf")
        self.assertTrue(spec.shared_rules)
        self.assertEqual(spec.prompt_from, "file")
        self.assertEqual(spec.prompt, "RULES\n\nShipped prompt.")
        self.assertEqual(len([p for p in spec.problems if "may not change" in p]), 2)

    def test_a_broken_cases_file_is_a_problem_not_a_crash(self):
        from museion.agent_run import count_cases
        write(self.shipped / "a", "cases.toml", '[[cases]]\nid = "x"\nprompt = "p"\nexpect_anyy = ["y"]\n')
        spec = self.load()
        self.assertEqual(count_cases(spec), 0)
        self.assertTrue(any(p.startswith("cases:") for p in spec.problems))

    def test_broken_toml_is_a_problem(self):
        write(self.mine / "a", "agent.toml", 'max_rounds = \n')
        self.assertTrue(any("agent.toml" in p for p in self.load().problems))

    def test_tier_gate(self):
        spec = self.load()
        spec.min_tier = hardware.CAPABLE
        self.assertIsNone(agents.tier_problem(spec, hardware.GENEROUS))
        self.assertIn("Needs a Capable computer", agents.tier_problem(spec, hardware.MODEST))
        # A big enough computer held back by a small chat model is told so.
        self.assertIn("bigger chat model", agents.tier_problem(spec, hardware.MODEST, hardware.CAPABLE))
        self.assertIn("Needs a Capable computer", agents.tier_problem(spec, hardware.MODEST, hardware.MODEST))

    def test_workspace(self):
        spec = self.load()
        root = Path("/lib/agent-work")
        self.assertEqual(agents.workspace(root, spec, "c1"), root / "a")
        spec.memory = "chat"
        self.assertEqual(agents.workspace(root, spec, "c1"), root / "a" / "chats" / "c1")
        spec.memory = "none"
        self.assertIsNone(agents.workspace(root, spec, "c1"))


if __name__ == "__main__":
    unittest.main()
