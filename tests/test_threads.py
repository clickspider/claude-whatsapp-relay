"""[threads]: the owner chat as a coordinator with parallel threads. Run: python3 -m unittest discover tests"""
import os, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402


class R:  # a finished claude run
    def __init__(self, out):
        self.stdout, self.stderr, self.returncode = out, "", 0


class ThreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = relay.load_config(write_cfg(self.tmp.name, "[threads]\nenabled = true\nmax_parallel = 2\n"))
        os.makedirs(cfg["paths"]["state_dir"], exist_ok=True)
        self.old = dict(relay.CFG), relay.OWNER
        relay.CFG.clear(), relay.CFG.update(cfg)
        relay.OWNER = cfg["owner"]["jid"]
        self.sent, self.jobs = [], []
        self.p = [mock.patch.object(relay, "send_text", lambda c, t, quote=None: self.sent.append(t)),
                  mock.patch.object(relay, "enqueue", lambda k, fn: self.jobs.append((k, fn))),
                  mock.patch.object(relay, "quoted_text", lambda c, m: "")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        relay.CFG.clear(), relay.CFG.update(self.old[0])
        relay.OWNER = self.old[1]
        self.tmp.cleanup()

    def test_config(self):
        self.assertEqual(relay.validate(relay.CFG)[0], [])
        bad = dict(relay.CFG, threads={"enabled": "yes", "max_parallel": 0, "main_chat_chars": -1})
        self.assertEqual(len([e for e in relay.validate(bad)[0] if "threads." in e]), 3)
        self.assertIn("THREADS:", relay.owner_system(relay.CFG))

    def test_markers(self):
        rest, notes = relay.take_thread_markers("On it.\n@@THREAD: flat move | find movers\n@@TO #2: add dates")
        self.assertEqual(rest, "On it.")
        self.assertEqual(notes, [("new", "flat move", "find movers"), ("to", "2", "add dates")])

    def test_start_run_followup_close(self):
        self.assertEqual(relay.thread_dispatch(("new", "flat move", "find movers"), "help me move"),
                         "Started #1: flat move")
        key, job = self.jobs.pop()
        self.assertEqual(key, "#1")
        with mock.patch.object(relay, "run_claude", return_value=R("3 movers found.\n@@STATE: review")) as rc:
            job()
        args = rc.call_args[0][0]
        self.assertIn("--session-id", args)
        self.assertTrue(any("THREAD: you work on ONE thread (#1: flat move)" in a for a in args))
        self.assertEqual(self.sent[-1], "#1 flat move: 3 movers found.")
        t = relay.threads_db()["1"]
        self.assertEqual((t["state"], t["last"]), ("review", "3 movers found."))
        self.assertIn("#1 flat move [review]", relay.thread_list())
        # "#1 ..." goes to the thread, which resumes its session; done closes it
        self.assertTrue(relay.thread_route("m2", "#1 book the cheapest", None))
        with mock.patch.object(relay, "with_feedback", lambda c, m, s, fn: fn()), \
                mock.patch.object(relay, "run_claude", return_value=R("Booked.\n@@STATE: done")) as rc:
            self.jobs.pop()[1]()
        self.assertIn("--resume", rc.call_args[0][0])
        self.assertIn("SAYS: book the cheapest", rc.call_args[0][2])
        self.assertEqual(relay.thread_list(), "")
        self.assertIn("closed", relay.thread_list(closed=True))

    def test_main_chat_reaches_threads(self):
        relay.threads_db("5", title="x", state="working", sid="", last="")
        relay.main_log("split the cost 50/50", "Noted: 50/50.")
        with mock.patch.object(relay, "run_claude", return_value=R("ok\n@@STATE: review")) as rc:
            relay.thread_run("5", "go")
            self.assertIn("50/50", rc.call_args[0][2])
            relay.thread_run("5", "again")  # already seen: not sent twice
            self.assertNotIn("MAIN CHAT", rc.call_args[0][2])

    def test_routing(self):
        relay.threads_db("4", title="x", state="working", sid="", last="")
        self.assertFalse(relay.thread_route("m", "hello", None))  # coordinator
        self.assertFalse(relay.thread_route("m", "#9 no such thread", None))
        with mock.patch.object(relay, "quoted_text", lambda c, m: '[Replying to your message: "#4 x: hi"]\n'):
            self.assertTrue(relay.thread_route("m", "and then?", None))
        self.assertTrue(relay.thread_route("m", "/done 4", None))
        self.assertEqual(self.sent[-1], "Closed #4.")
        self.assertTrue(relay.thread_route("m", "/threads", None))
        self.assertEqual(self.sent[-1], "No open threads.")


if __name__ == "__main__":
    unittest.main()
