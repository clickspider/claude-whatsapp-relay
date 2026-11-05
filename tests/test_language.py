"""Owner, thread and agent runs all get the language rule. Run: python3 -m unittest discover tests"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402


class LanguageRuleTests(unittest.TestCase):
    def test_rule_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = relay.load_config(write_cfg(tmp, '[threads]\nenabled = true\n'))
            self.assertIn("never text to forward", relay.owner_system(cfg))
            cfg["owner"]["system_prompt"] = "My own prompt."  # a custom prompt keeps the rule
            self.assertIn("in the language of their last message", relay.owner_system(cfg))
            self.assertIn("in the owner's language", relay.THREAD_RULES)
            a = dict(relay.AGENT_DEFAULTS, name="x", dir=tmp, home=tmp)
            args = relay.agent_args(cfg, a, "s", True)
            self.assertIn("never text to forward", args[args.index("--append-system-prompt") + 1])


if __name__ == "__main__":
    unittest.main()
