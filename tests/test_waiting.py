"""Every owner and thread run gets the no-foreground-wait rule. Run: python3 -m unittest discover tests"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402


class WaitRuleTests(unittest.TestCase):
    def test_rule_in_owner_and_custom_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = relay.load_config(write_cfg(tmp, '[threads]\nenabled = true\n'))
            self.assertIn("never wait in the foreground for the relay", relay.owner_system(cfg))
            cfg["owner"]["system_prompt"] = "My own prompt."  # a custom prompt keeps the rule
            args = relay.owner_args(cfg, "s", True, extra_prompt="THREAD")
            self.assertIn("nohup", args[args.index("--append-system-prompt") + 1])


if __name__ == "__main__":
    unittest.main()
