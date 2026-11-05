"""A newer owner message cuts the answer being written: one turn answers all (5 Nov 2026, like Instinct)."""
import os, sys, threading, time, unittest

sys.path[:0] = [os.path.join(os.path.dirname(__file__), ".."), os.path.dirname(__file__)]
import relay  # noqa: E402
from test_batch_publish import Patch  # noqa: E402


class Preempt(unittest.TestCase):
    def setUp(self):
        relay.owner_buf.clear()
        relay.owner_sched[0] = False
        relay._owner_cut.clear()

    def test_cut_turn_goes_back_in_front_and_stays_in_flight(self):
        turns, done = [], []

        def turn(*a, batch=None):
            turns.append(batch or [a])
            if len(turns) == 1:  # the first answer is cut by the newer message
                relay.owner_buf.append(("m2", "and this", "", time.time()))
                raise relay.Preempted()

        cfg = {"owner": {"batch_seconds": 0}, "feedback": {"reactions": False}}
        with Patch(CFG=cfg, OWNER="o", enqueue=lambda k, fn: None, with_feedback=lambda c, m, s, fn: fn(),
                   owner_turn=turn, done_job=done.append):
            relay.owner_add("m1", "first", "")
            relay.owner_drain()
            self.assertEqual(done, [])  # m1 is still in flight
            self.assertEqual(relay._owner_cut, {"m1"})
            relay.owner_drain()
        self.assertEqual([m[0] for m in turns[1]], ["m1", "m2"])  # one turn, old message first
        self.assertEqual(done, ["m1", "m2"])

    def test_owner_add_kills_only_the_preemptible_run_and_not_for_a_reaction(self):
        class P:
            def __init__(self, preempt):
                self.preempt, self.killed = preempt, False

            def terminate(self):
                self.killed = True

        answer, handoff = P(True), P(False)
        with Patch(enqueue=lambda k, fn: None, _procs={answer, handoff}):
            relay.owner_add("r", "👍", "reaction")
            self.assertFalse(answer.killed)
            relay.owner_add("m", "more", "")
        self.assertTrue(answer.killed)
        self.assertFalse(handoff.killed)
        relay._preempted.clear()

    def test_run_claude_raises_preempted(self):
        cfg = {"limits": {"run_timeout_seconds": 10}}
        got = []

        def run():
            relay._wk.preempt = True
            try:
                relay.run_claude(["sleep", "5"], ".", "")
            except relay.Preempted:
                got.append("cut")
            finally:
                relay._wk.preempt = False

        with Patch(CFG=cfg, enqueue=lambda k, fn: None):
            t = threading.Thread(target=run)
            t.start()
            for _ in range(50):
                if relay._procs:
                    break
                time.sleep(0.05)
            relay.owner_add("m", "new", "")
            t.join(5)
        self.assertEqual(got, ["cut"])

    def test_turn_after_a_cut_gets_the_cut_note(self):
        asked = []
        cfg = {"owner": {"voice_replies": False, "name": "Dan"}, "threads": {"enabled": False}}
        relay._owner_cut.add("m1")
        with Patch(CFG=cfg, OWNER="o", owner_prep=lambda m, t, mt: (t, False),
                   ask_owner_claude=lambda p: asked.append(p) or "ok", pick_react=lambda *a: None,
                   coordinator_send=lambda *a, **k: None):
            relay.owner_turn(None, None, None, batch=[("m1", "a", ""), ("m2", "b", "")])
            relay.owner_turn("m3", "c", "")
        self.assertTrue(asked[0].startswith(relay.CUT_NOTE))
        self.assertFalse(asked[1].startswith(relay.CUT_NOTE))
        self.assertEqual(relay._owner_cut, set())


if __name__ == "__main__":
    unittest.main()
