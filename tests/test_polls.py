"""Native choices: @@ASK parsing, draft polls and votes (ui.polls)."""
import json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_batch_publish import Patch  # noqa: E402


class TakeAsks(unittest.TestCase):
    def test_valid_and_invalid_lines(self):
        rest, asks = relay.take_asks("Hi\n@@ASK: Which day? | Mon | Tue | Mon\n@@ASK: broken | only")
        self.assertEqual(asks, [("Which day?", ["Mon", "Tue"])])
        self.assertEqual(rest, "Hi\n@@ASK: broken | only")  # bad line is kept, not lost


class Votes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adir = os.path.join(self.tmp.name, "mom")
        os.makedirs(self.adir)
        self.agent = {"name": "mom", "jid": "c@s.whatsapp.net", "dir": self.adir, "home": self.adir,
                      "live": True, "trust": "reply", "ask_contact": False, "timezone": "UTC"}
        self.cfg = {"paths": {"state_dir": self.tmp.name}, "ui": {"polls": True},
                    "owner": {"name": "Owner"}, "prompts": {"owner_says": "OWNER SAYS: "},
                    "hooks": {"after_log": []}, "agents": [self.agent]}
        self.sent, self.jobs, self.calls = [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def patch(self, **kw):
        base = dict(CFG=self.cfg, OWNER="o@s.whatsapp.net",
                    send_text=lambda c, t, quote=None: self.sent.append((c, t)),
                    enqueue=lambda k, fn: self.jobs.append(fn),
                    api=lambda path, p=None: self.calls.append((path, p)) or {"message_id": "P1"})
        base.update(kw)
        return Patch(**base)

    def test_draft_poll_send_runs_ok_once(self):
        ran = []
        with self.patch(owner_command=lambda n, arg: ran.append((n, arg)),
                        parse=lambda out: (out, {"NOTE": [], "EN": "", "HOLD": True, "NEXT_CHECKIN": "", "QUOTE": ""})):
            relay.deliver("c@s.whatsapp.net", self.agent, "Hello mom", False)
            self.assertEqual(self.calls[-1][0], "poll")
            self.assertEqual(self.calls[-1][1]["options"], ["Send", "Change", "Drop"])
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "Send")
            relay.on_vote("V2", "o@s.whatsapp.net", "o", "P1", "Send")  # second vote: ignored
            for job in self.jobs:
                job()
        self.assertEqual(ran, [("mom", "ok")])

    def test_vote_from_other_chat_or_stale_draft_does_nothing(self):
        with self.patch(owner_command=lambda n, arg: self.fail("must not send")):
            relay.polls({"P1": {"kind": "draft", "agent": "mom", "chat": "o@s.whatsapp.net",
                                "question": "q", "draft": relay.digest("old text")}})
            relay.on_vote("V1", "stranger@s.whatsapp.net", "x", "P1", "Send")
            self.assertEqual(self.jobs, [])
            relay.flag(self.agent, "pending", "new text")
            relay.on_vote("V2", "o@s.whatsapp.net", "o", "P1", "Send")
        self.assertIn("not waiting", self.sent[-1][1])
        self.assertEqual(self.jobs, [])

    def test_drop_clears_the_draft(self):
        with self.patch():
            relay.flag(self.agent, "pending", "text")
            relay.polls({"P1": {"kind": "draft", "agent": "mom", "chat": "o@s.whatsapp.net",
                                "question": "q", "draft": relay.digest("text")}})
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "Drop")
        self.assertIsNone(relay.flag(self.agent, "pending"))

    def test_owner_ask_answer_becomes_next_owner_message(self):
        got = []
        with self.patch(owner_add=lambda m, t, mt: got.append(t)):
            relay.ask("o@s.whatsapp.net", "Lunch?", ["Yes", "No"], kind="ask")
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "No")
        self.assertEqual(got, ['ANSWER to "Lunch?": No'])

    def test_polls_off_falls_back_to_text(self):
        self.cfg["ui"]["polls"] = False
        with self.patch():
            relay.ask("o@s.whatsapp.net", "Lunch?", ["Yes", "No"], kind="ask")
            relay.ask("o@s.whatsapp.net", "mom draft", relay.DRAFT_OPTIONS, kind="draft")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sent, [("o@s.whatsapp.net", "Lunch?\n1. Yes\n2. No")])
