"""feedback.context_reactions: '@@REACT: <emoji>' from Claude is the only reaction, sent after reading, before the text. Run: python3 -m unittest discover tests"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402

AGENT = """
[[agent]]
name = "alex"
jid = "15550100002@s.whatsapp.net"
live = true
trust = "{trust}"
"""


class ContextEmojiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: getattr(relay, k) for k in ("api", "ask_owner_claude", "owner_prep", "run_agent_told", "CFG", "OWNER")}
        self.calls = []
        relay.api = lambda path, payload=None: self.calls.append((path, payload or {})) or {}
        relay.owner_prep = lambda *m: (m[1], False)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(relay, k, v)
        self.tmp.cleanup()

    def load(self, extra="", trust="reply"):
        relay.CFG = relay.load_config(write_cfg(self.tmp.name, AGENT.format(trust=trust) + extra))
        relay.OWNER = relay.CFG["owner"]["jid"]
        return relay.CFG

    def reacts(self):
        return [p["emoji"] for path, p in self.calls if path == "react"]

    def sends(self):
        return [p["message"] for path, p in self.calls if path == "send"]

    def owner(self, answer, text="hi"):
        relay.ask_owner_claude = lambda prompt: answer
        relay.with_feedback(relay.OWNER, "m1", relay.OWNER, lambda: relay.owner_turn("m1", text, None))

    def test_take_react(self):
        self.assertEqual(relay.take_react("@@REACT: 😂\nHa, good one."), ("Ha, good one.", "😂"))
        self.assertEqual(relay.take_react("Nice!\n@@REACT 🎉"), ("Nice!", "🎉"))
        self.assertEqual(relay.take_react("@@REACT: ❤"), ("", "❤️"))  # no variation selector -> allowlist form
        self.assertEqual(relay.take_react("@@REACT: 🙏🙏"), ("", None))  # not one allowed emoji: ignored
        self.assertEqual(relay.take_react("@@REACT: <b>hi</b>"), ("", None))
        self.assertEqual(relay.take_react("plain text"), ("plain text", None))

    def test_owner_reaction_after_reading(self):
        self.load()
        self.owner("@@REACT: 🎉\nCongrats!", "I got the job")
        self.assertEqual(self.reacts(), ["🎉"])
        self.assertEqual(self.sends(), ["Congrats!"])
        order = [path for path, _ in self.calls if path in ("react", "send")]
        self.assertEqual(order, ["react", "send"])  # the reaction lands before the answer

    def test_reaction_only_answer_sends_no_text(self):
        self.load()
        self.owner("@@REACT: 🙏", "thanks!")
        self.assertEqual(self.reacts(), ["🙏"])
        self.assertEqual(self.sends(), [])

    def test_invalid_emoji_no_reaction(self):
        self.load()
        self.owner("@@REACT: banana\nOK", "do it")
        self.assertEqual(self.reacts(), [])
        self.assertEqual(self.sends(), ["OK"])

    def test_no_mark_on_arrival(self):
        self.load()
        seen = []
        relay.ask_owner_claude = lambda prompt: seen.append(self.reacts()) or "@@REACT: 👍\nOn it."
        relay.with_feedback(relay.OWNER, "m1", relay.OWNER, lambda: relay.owner_turn("m1", "do x", None))
        self.assertEqual(seen, [[]])  # nothing on the message while Claude reads it
        self.assertEqual(self.reacts(), ["👍"])

    def test_failed_run_no_reaction(self):
        self.load()  # the error reaches the owner as text (worker loop); the relay never reacts by itself
        with self.assertRaises(RuntimeError):
            relay.with_feedback(relay.OWNER, "m1", relay.OWNER, lambda: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertEqual(self.reacts(), [])

    def test_instinct_set_allowed(self):
        for e in ("👀", "🔍", "✅", "👍"):
            self.assertEqual(relay.take_react(f"@@REACT: {e}\nOn it."), ("On it.", e))
        cfg = self.load()
        self.assertIn("Most messages get none", relay.owner_system(cfg))
        self.assertIn("actually done AND checked", relay.owner_system(cfg))  # ✅ rule (Instinct, 4 Nov)

    def test_batch_reacts_once_on_last(self):
        self.load()
        relay.ask_owner_claude = lambda prompt: "@@REACT: 👍\nBoth done."
        relay.owner_batch([("m1", "a", None), ("m2", "b", None)])
        self.assertEqual([(p["message_id"], p["emoji"]) for path, p in self.calls if path == "react"], [("m2", "👍")])

    def test_config_off_old_behaviour(self):
        cfg = self.load("[feedback]\ncontext_reactions = false\n")
        self.assertNotIn("@@REACT", relay.owner_system(cfg))
        self.assertNotIn("@@REACT", " ".join(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))
        self.owner("@@REACT: 😂\nHa", "joke")
        self.assertEqual(self.reacts(), [])
        self.assertEqual(self.sends(), ["Ha"])  # the line never leaks into the chat

    def test_prompt_on_by_default(self):
        cfg = self.load()
        self.assertEqual(relay.validate(cfg)[0], [])
        self.assertIn("@@REACT", relay.owner_system(cfg))
        self.assertIn("@@REACT", " ".join(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))

    def contact(self, answer, trust):
        cfg = self.load(trust=trust)
        jid, a = relay.by_name("alex")
        relay.run_agent_told = lambda *x: answer
        relay.with_feedback(jid, "m2", "15550100002@s.whatsapp.net",
                            lambda: relay.contact_turn(jid, a, "m2", "you are sweet", None, sender="15550100002"))
        return jid

    def test_contact_reply_agent_reacts(self):
        self.contact("@@REACT: ❤️\nThank you!", "reply")
        self.assertEqual(self.reacts(), ["❤️"])
        self.assertEqual(self.sends(), ["Thank you!"])

    def test_draft_agent_never_reacts_unreviewed(self):
        self.contact("@@REACT: ❤️", "draft")
        self.assertEqual(self.reacts(), [])

    def test_listening_group_not_addressed_never_reacts(self):
        self.load()
        jid, a = relay.by_name("alex")
        relay.run_agent_told = lambda *x: "@@REACT: 😂"
        relay.group_turn(jid, a, [("Bo", "m3", "lol", None, "15550100009@s.whatsapp.net")], False)
        self.assertEqual(self.reacts(), [])
        self.assertEqual(self.sends(), [])


if __name__ == "__main__":
    unittest.main()
