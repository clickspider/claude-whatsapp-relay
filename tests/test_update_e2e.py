"""End to end, with a real (fake) claude process: an update stops a running thread at once, nothing
reaches WhatsApp, and after the 'restart' the job goes on in the same session and finishes once.
Run: python3 -m unittest discover tests"""
import json, os, sys, tempfile, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
args, prompt = sys.argv[1:], sys.stdin.read()
log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl")
sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
known = os.path.join(os.path.dirname(log), "sessions")
os.makedirs(known, exist_ok=True)
if "--resume" in args and not os.path.exists(os.path.join(known, sid)):
    print("No conversation found with session ID: " + sid); sys.exit(1)
open(os.path.join(known, sid), "w").close()
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "prompt": prompt}) + "\n")
if "relay restarted for an update" not in prompt:
    time.sleep(60)  # a long job: the update stops it here
print("Booked the movers.\n@@STATE: done")
'''


def wait(cond, secs=15):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class UpdateEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake = os.path.join(self.tmp.name, "claude")
        with open(fake, "w") as f:
            f.write(FAKE)
        os.chmod(fake, 0o755)
        cfg = relay.load_config(write_cfg(self.tmp.name, f'[threads]\nenabled = true\n[paths]\nclaude = "{fake}"\n'))
        os.makedirs(cfg["paths"]["state_dir"], exist_ok=True)
        self.old = dict(relay.CFG), relay.OWNER
        relay.CFG.clear(), relay.CFG.update(cfg)
        relay.OWNER = cfg["owner"]["jid"]
        self.sent = []
        self.p = [mock.patch.object(relay, "workers", {}),
                  mock.patch.object(relay, "_tsem", []),
                  mock.patch.object(relay, "send_text", lambda c, t, quote=None: (relay.hold_if_stopping(), self.sent.append(t))),
                  mock.patch.object(relay, "decisions_for", lambda *a: "", create=True),
                  mock.patch.object(relay, "digest_soon", lambda: None)]  # results go to the coordinator
        for p in self.p:
            p.start()
        relay._inflight.clear(), relay._claimed.clear(), relay._procs.clear(), relay.STOPPING.clear()

    def tearDown(self):
        for p in self.p:
            p.stop()
        relay.STOPPING.clear()
        relay.CFG.clear(), relay.CFG.update(self.old[0])
        relay.OWNER = self.old[1]
        self.tmp.cleanup()

    def calls(self):
        path = os.path.join(self.tmp.name, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f]

    def test_update_stops_then_resumes_in_same_session(self):
        relay.threads_db("1", title="flat move", state="working", sid="", last="")
        relay.thread_job("1", "find movers")
        self.assertTrue(wait(lambda: relay._procs and self.calls()), "the first run never started")
        proc = next(iter(relay._procs))
        # --- the update: stop at once
        exited = threading.Event()
        with mock.patch.object(relay, "START_MTIME", 0), mock.patch.object(relay, "_digest", {"due": False}), \
                mock.patch.object(relay.os, "_exit", lambda code: exited.set()):
            relay.restart_if_changed()
        self.assertTrue(exited.is_set())
        self.assertIsNotNone(proc.poll(), "the claude run was not stopped")
        time.sleep(0.5)
        self.assertEqual(self.sent, [], "a stopped run sent something to WhatsApp")
        kept = json.loads(relay.read(relay.st("inflight.json")))
        self.assertEqual([r["thread"][0] for r in kept.values()], ["1"])
        first_sid = relay.threads_db()["1"]["sid"]
        self.assertTrue(first_sid)
        # --- the new process (fresh in-memory state, same state folder)
        relay.STOPPING.clear()
        relay._inflight.clear(), relay._claimed.clear()
        with mock.patch.object(relay, "workers", {}):
            relay.resume_jobs()
            self.assertTrue(wait(lambda: relay.threads_db()["1"]["state"] == "closed"), "the job did not come back")
        self.assertEqual(self.sent, [])  # nobody saw the stop
        self.assertIn("Booked the movers.", relay.read(relay.st("threads/1.md")))
        last = self.calls()[-1]
        self.assertIn("--resume", last["args"])
        self.assertEqual(last["args"][last["args"].index("--resume") + 1], first_sid)  # same session
        self.assertIn("relay restarted for an update", last["prompt"])
        self.assertIn("find movers", last["prompt"])
        self.assertTrue(wait(lambda: json.loads(relay.read(relay.st("inflight.json"))) == {}))

    def test_resume_before_session_was_saved_starts_it(self):
        sid = "11111111-2222-4333-8444-555555555555"
        r = relay.run_claude([relay.CFG["paths"]["claude"], "-p", "--resume", sid], self.tmp.name,
                             relay.RESUME_NOTE + "x")
        self.assertIn("Booked", r.stdout)
        self.assertIn("--session-id", self.calls()[-1]["args"])


if __name__ == "__main__":
    unittest.main()
