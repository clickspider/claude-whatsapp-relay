import datetime, os, sys, tempfile, unittest
from unittest import mock
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay

TZ = "Asia/Jerusalem"


def agent(d, **kw):
    a = dict(relay.AGENT_DEFAULTS, name="or", jid="972500000001@s.whatsapp.net", dir=d, home=os.path.join(d, "home"),
             timezone=TZ, live=True)
    a.update(kw)
    return a


class TurnsTrialTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        relay.CFG = {"limits": {"agent_turns_per_day": 150}, "prompts": dict(relay.DEFAULTS["prompts"]),
                     "threads": {"enabled": False}, "hooks": {"after_log": []}}
        self.sent, self.notes = [], []
        self.p = [mock.patch.object(relay, "send_text", lambda chat, text, **k: self.sent.append((chat, text))),
                  mock.patch.object(relay, "agent_note", lambda name, note, dry=False: self.notes.append(note))]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_turn_ok_counts_and_resets_next_day(self):
        self.assertEqual(relay.turn_ok(2, "", "2026-11-04"), (True, "2026-11-04 1"))
        self.assertEqual(relay.turn_ok(2, "2026-11-04 1", "2026-11-04"), (True, "2026-11-04 2"))
        self.assertEqual(relay.turn_ok(2, "2026-11-04 2", "2026-11-04"), (False, "2026-11-04 2"))
        self.assertEqual(relay.turn_ok(2, "2026-11-04 2", "2026-11-05"), (True, "2026-11-05 1"))
        self.assertTrue(relay.turn_ok(0, "2026-11-04 999", "2026-11-04")[0])  # 0 = no cap

    def test_cap_tells_contact_once_and_owner_once(self):
        a = agent(self.d, turns_per_day=2, turns_message="see you tomorrow")
        self.assertEqual([relay.contact_gate(a["jid"], a) for _ in range(4)], [True, True, False, False])
        self.assertEqual(self.sent, [(a["jid"], "see you tomorrow")])
        self.assertEqual(len(self.notes), 1)

    def test_global_default_applies_when_agent_has_none(self):
        relay.CFG["limits"]["agent_turns_per_day"] = 1
        a = agent(self.d)
        self.assertEqual([relay.contact_gate(a["jid"], a) for _ in range(2)], [True, False])
        self.assertEqual(self.sent[0][1], relay.DEFAULTS["prompts"]["turns_used_up"])

    def test_trial_starts_at_first_send_and_ends_after_trial_hours(self):
        a = agent(self.d, trial_hours=72, trial_message="trial over")
        self.assertIsNone(relay.trial_end(a))  # nothing sent yet: no clock
        relay.flag(a, "trial-start", "2026-11-04 11:40")
        end = relay.trial_end(a)
        self.assertEqual(end, datetime.datetime(2026, 11, 7, 11, 40, tzinfo=ZoneInfo(TZ)))
        self.assertFalse(relay.trial_over(a, end - datetime.timedelta(minutes=1)))
        self.assertTrue(relay.trial_over(a, end))

    def test_trial_over_blocks_and_says_so_once(self):
        a = agent(self.d, trial_hours=72, trial_message="trial over")
        relay.flag(a, "trial-start", "2026-11-01 09:00")
        self.assertFalse(relay.contact_gate(a["jid"], a))
        self.assertFalse(relay.contact_gate(a["jid"], a))
        self.assertEqual(self.sent, [(a["jid"], "trial over")])
        self.assertIsNone(relay.flag(a, "turns"))  # a blocked message uses no turn

    def test_watch_warns_before_end_then_at_end_and_again_after_extension(self):
        a = agent(self.d, trial_hours=72)
        now = relay.agent_now(a)
        relay.flag(a, "trial-start", f"{now - datetime.timedelta(hours=60):{relay.REMIND_FMT}}")
        relay.trial_watch(a); relay.trial_watch(a)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("Trial ends", self.notes[0])
        relay.flag(a, "trial-start", f"{now - datetime.timedelta(hours=73):{relay.REMIND_FMT}}")
        relay.trial_watch(a); relay.trial_watch(a)
        self.assertEqual(len(self.notes), 2)  # already past the new end: one "ended" note, no warning
        self.assertIn("Trial ended", self.notes[-1])
        a["trial_hours"] = 96  # owner extends in the config
        relay.trial_watch(a)
        self.assertIn("Trial ends", self.notes[-1])


    def test_agent_cannot_edit_its_own_cap_or_trial_files(self):
        cfg = {"paths": {"claude": "claude"}, "danger": {"allow_unrestricted_agents": False},
               "feedback": {"reactions": False}, "owner": {}}
        a = agent(self.d, tools=relay.FILE_TOOLS)
        args = relay.agent_args(cfg, a, "sid", True)
        deny = relay.json.loads(args[args.index("--settings") + 1])["permissions"]["deny"]
        for f in relay.LIMIT_FLAGS:
            self.assertIn(f"Edit(/{os.path.join(self.d, f)})", deny)


if __name__ == "__main__":
    unittest.main()
