"""Owner message batching and the opt-in publish reminder."""
import os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402


class Patch:
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(relay, k)
            setattr(relay, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(relay, k, v)


class Batching(unittest.TestCase):
    def test_close_messages_become_one_turn(self):
        turns, marks = [], []
        cfg = {"owner": {"batch_seconds": 0.2}, "feedback": {"reactions": True}}
        with Patch(CFG=cfg, OWNER="o", enqueue=lambda k, fn: None,
                   with_feedback=lambda c, m, s, fn: fn(),
                   owner_turn=lambda *a, batch=None: turns.append(batch or [a]),
                   react=lambda c, m, s, e: marks.append((m, e))):
            for i in range(3):
                relay.owner_add(f"m{i}", f"hello {i}", "")
            relay.owner_drain()
        self.assertEqual(len(turns), 1)
        self.assertEqual([m[0] for m in turns[0]], ["m0", "m1", "m2"])
        self.assertIn(("m0", "✅"), marks)
        self.assertFalse(relay.owner_sched[0])

    def test_new_runs_on_its_own(self):
        turns = []
        cfg = {"owner": {"batch_seconds": 0}, "feedback": {"reactions": False}}
        with Patch(CFG=cfg, OWNER="o", enqueue=lambda k, fn: None,
                   with_feedback=lambda c, m, s, fn: fn(),
                   owner_turn=lambda *a, batch=None: turns.append(batch or [a])):
            relay.owner_add("a", "/new", "")
            relay.owner_add("b", "hi", "")
            relay.owner_drain()
        self.assertEqual(turns, [[("a", "/new", "")], [("b", "hi", "")]])


class Publish(unittest.TestCase):
    def run_check(self, commits, ok=True, asked=""):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"publish": {"remind": True, "remote": "origin", "branch": "main", "ssh_key": "",
                               "check_hours": 6}, "paths": {"state_dir": tmp}}
            if asked:
                open(os.path.join(tmp, "publish-asked"), "w").write(asked)
            class R:
                stdout = "abc123\n"
            with Patch(CFG=cfg, OWNER="o", unpublished=lambda: (commits, 0),
                       tests_pass=lambda: (ok, ""), git=lambda *a, **k: R(),
                       send_text=lambda c, t, quote=None: sent.append(t)):
                relay.publish_check(force=True)
        return sent

    def test_asks_once_when_tested_commits_wait(self):
        sent = self.run_check(["abc123 New feature"])
        self.assertEqual(len(sent), 1)
        self.assertIn("/publish", sent[0])
        self.assertEqual(self.run_check(["abc123 New feature"], asked="abc123"), [])

    def test_silent_when_tests_fail_or_nothing_new(self):
        self.assertEqual(self.run_check(["abc123 x"], ok=False), [])
        self.assertEqual(self.run_check([]), [])


if __name__ == "__main__":
    unittest.main()
