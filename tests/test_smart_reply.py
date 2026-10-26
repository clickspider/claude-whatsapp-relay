"""auto_reply = "smart": untagged group messages meant for the agent get an answer."""
import os, sys, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_batch_publish import Patch  # noqa: E402

A = {"name": "wedding", "jid": "1@g.us", "auto_reply": "smart", "group_label": "GROUP.", "dir": "/tmp"}


class SmartCheck(unittest.TestCase):
    def setUp(self):
        relay._smart_last.clear()

    def run_check(self, yes):
        flushed = []
        with Patch(smart_yes=lambda a, lines: yes, recent_lines=lambda a: "",
                   flush=lambda *args: flushed.append(args)):
            relay.smart_check("1@g.us", A, "m1", "s@s.whatsapp.net")
        return flushed

    def test_meant_for_agent_flushes_as_smart(self):
        flushed = self.run_check(True)
        self.assertEqual(flushed, [("wedding", "1@g.us", A, "smart", "m1", "s@s.whatsapp.net")])

    def test_chitchat_stays_in_the_buffer(self):
        self.assertEqual(self.run_check(False), [])

    def test_cooldown_blocks_a_second_answer(self):
        self.assertEqual(len(self.run_check(True)), 1)
        self.assertEqual(self.run_check(True), [])
        relay._smart_last["wedding"] = time.time() - relay.SMART_COOLDOWN - 1
        self.assertEqual(len(self.run_check(True)), 1)

    def test_failed_check_falls_back_to_the_digest(self):
        def boom(a, lines):
            raise RuntimeError("claude down")
        flushed = []
        with Patch(smart_yes=boom, recent_lines=lambda a: "", flush=lambda *args: flushed.append(args),
                   owner_now=lambda: relay.datetime.datetime.now()):
            relay.smart_check("1@g.us", A, "m1", "s")
        self.assertEqual(flushed, [])


class Heads(unittest.TestCase):
    def head(self, a, addressed):
        prompts = []
        with Patch(media_text=lambda *x: "hi", quoted_text=lambda *x: "",
                   run_agent_told=lambda j, a, p, t: prompts.append(p) or "",
                   deliver=lambda *x, **k: None):
            relay.group_turn("1@g.us", a, [("Lara", "m1", "hi", "", "s")], addressed)
        return prompts[0]

    def test_smart_turn_says_meant_for_you(self):
        self.assertIn("seems meant for you", self.head(A, "smart"))

    def test_smart_digest_may_answer_open_instructions(self):
        self.assertIn("Answer only when", self.head(A, False))

    def test_plain_listen_digest_unchanged(self):
        self.assertIn("really important note", self.head(dict(A, auto_reply=""), False))


if __name__ == "__main__":
    unittest.main()
