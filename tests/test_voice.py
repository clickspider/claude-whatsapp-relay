"""owner.voice_file + per-agent use_owner_voice. Run: python3 -m unittest discover tests"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402

AGENT = """
[[agent]]
name = "alex"
jid = "15550100002@s.whatsapp.net"
"""


def voice_arg(args):
    return next((args[i + 1] for i, x in enumerate(args) if x == "--append-system-prompt"
                 and "Write like the owner" in args[i + 1]), None)


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.own = relay.OWN[0]

    def tearDown(self):
        relay.OWN[0] = self.own

    def cfg(self, tmp, extra=AGENT, text="Short. Lowercase. Ends with 👍"):
        path = write_cfg(tmp, extra)
        if text is not None:
            with open(os.path.join(tmp, "dossier", "voice.md"), "w") as f:
                f.write(text)
        return relay.load_config(path)

    def test_voice_injected_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay.OWN[0] = True  # own number: the agent speaks as the owner -> default on
            cfg = self.cfg(tmp)
            self.assertIn("Ends with 👍", relay.owner_system(cfg))
            self.assertIn("Ends with 👍", voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))

    def test_not_injected_when_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay.OWN[0] = False  # second number: default off
            cfg = self.cfg(tmp)
            self.assertIsNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))
            relay.OWN[0] = True
            cfg = self.cfg(tmp, AGENT + "use_owner_voice = false\n")
            self.assertIsNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))
            cfg = self.cfg(tmp)
            cfg["owner"]["voice_file"] = ""  # owner.voice_file = "" turns it off everywhere
            self.assertNotIn("OWNER'S VOICE", relay.owner_system(cfg))
            self.assertIsNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))

    def test_explicit_on_with_second_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay.OWN[0] = False
            cfg = self.cfg(tmp, AGENT + "use_owner_voice = true\n")
            self.assertIsNotNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))

    def test_missing_file_no_crash_and_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay.OWN[0] = True
            cfg = self.cfg(tmp, text=None)
            self.assertNotIn("OWNER'S VOICE", relay.owner_system(cfg))
            self.assertIsNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))
            cfg = self.cfg(tmp, text="x" * 20_000)
            self.assertEqual(len(relay.owner_voice(cfg)), relay.VOICE_MAX)


if __name__ == "__main__":
    unittest.main()
