"""paths.agent_base_rules: every agent gets one shared rules file. Run: python3 -m unittest discover tests"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402


def prompt(args):
    return args[args.index("--append-system-prompt") + 1]


class BaseRulesTests(unittest.TestCase):
    def test_every_agent_gets_base_rules_and_history_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "BASE.md"), "w") as f:
                f.write("NEVER RE-ASK what was answered.")
            cfg = relay.load_config(write_cfg(tmp, '[paths]\nagent_base_rules = "BASE.md"\n'))
            self.assertEqual(relay.validate(cfg)[0], [])
            home = os.path.join(tmp, "home")
            for extra in ({}, {"unrestricted": True}):  # both prompt paths
                a = dict(relay.AGENT_DEFAULTS, name="x", dir=tmp, home=home, **extra)
                args = relay.agent_args(cfg, a, "s", True)
                p = prompt(args)
                self.assertIn("NEVER RE-ASK", p)
                self.assertIn(os.path.join(home, "history.md"), p)
                self.assertIn("never text to forward", p)  # still next to the language rule
                self.assertEqual(args.count("--append-system-prompt"), 1)
                self.assertIn("--append-system-prompt-file", args)  # RULES.md still loads

    def test_off_by_default_and_missing_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = relay.load_config(write_cfg(tmp))
            a = dict(relay.AGENT_DEFAULTS, name="x", dir=tmp, home=tmp)
            self.assertNotIn("YOUR CHAT HISTORY FILE", prompt(relay.agent_args(cfg, a, "s", True)))
            cfg = relay.load_config(write_cfg(tmp, '[paths]\nagent_base_rules = "nope.md"\n'))
            self.assertTrue(any("agent_base_rules" in e for e in relay.validate(cfg)[0]))


if __name__ == "__main__":
    unittest.main()
