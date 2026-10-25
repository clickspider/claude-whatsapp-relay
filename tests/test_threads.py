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
        bad = dict(relay.CFG, threads={"enabled": "yes", "max_parallel": 0, "main_chat_chars": -1, "auto_close_hours": -1, "effort_default": "huge", "effort_high": "", "effort_high_words": []})
        self.assertEqual(len([e for e in relay.validate(bad)[0] if "threads." in e]), 5)
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

    def test_context_flows_everywhere(self):
        # main chat -> thread, thread -> other thread, thread -> coordinator; nobody gets their own back
        relay.threads_db("5", title="money", state="working", sid="", last="")
        relay.threads_db("6", title="house", state="working", sid="", last="")
        relay.main_log("OWNER", "split the cost 50/50")
        relay.main_log("COORDINATOR", "Noted: 50/50.")
        with mock.patch.object(relay, "run_claude", return_value=R("insulation cancelled\n@@STATE: review")) as rc:
            relay.thread_run("6", "go")
            self.assertIn("50/50", rc.call_args[0][2])
        with mock.patch.object(relay, "run_claude", return_value=R("ok\n@@STATE: review")) as rc:
            relay.thread_run("5", "go")
            p = rc.call_args[0][2]
            self.assertIn("insulation cancelled", p)
            self.assertIn("50/50", p)
            relay.thread_run("5", "again")  # seen already, and its own answer never comes back
            self.assertNotIn("MAIN CHAT", rc.call_args[0][2])
        news = relay.main_since("coordinator", lambda w: w in ("OWNER", "COORDINATOR"))
        self.assertIn("#6 house (review)", news)
        self.assertNotIn("50/50", news)

    def test_failure_and_restart_never_hide_a_thread(self):
        relay.threads_db("7", title="x", state="working", sid="", last="")
        with mock.patch.object(relay, "run_claude", side_effect=TimeoutError("timed out")):
            with self.assertRaises(TimeoutError):
                relay.thread_run("7", "go")
        self.assertEqual(relay.threads_db()["7"]["state"], "blocked")
        relay.threads_db("8", title="y", state="working", sid="", last="")
        relay.thread_recover()
        self.assertEqual(relay.threads_db()["8"]["state"], "blocked")
        self.assertIn("#8 y", self.sent[-1])

    def test_working_means_a_run_and_old_reviews_close(self):
        relay.threads_db("9", title="z", state="working", sid="", last="")
        with mock.patch.object(relay, "run_claude", return_value=R("agents started\n@@STATE: working")):
            relay.thread_run("9", "go")
        self.assertEqual(relay.threads_db()["9"]["state"], "review")  # nothing runs after a reply
        relay.thread_auto_close()
        self.assertEqual(relay.threads_db()["9"]["state"], "review")
        with mock.patch.object(relay.time, "time", return_value=relay.time.time() + 73 * 3600):
            relay.thread_auto_close()
        self.assertEqual(relay.threads_db()["9"]["state"], "closed")

    def test_big_session_hands_off(self):
        relay.threads_db("3", title="w", state="working", sid="old", last="")
        with mock.patch.object(relay, "context_tokens", return_value=10**9), \
                mock.patch.object(relay, "run_claude", side_effect=[R("open: X"), R("ok\n@@STATE: review")]) as rc:
            relay.thread_run("3", "go")
        self.assertIn("--session-id", rc.call_args[0][0])
        self.assertIn("open: X", rc.call_args[0][2])
        self.assertNotEqual(relay.threads_db()["3"]["sid"], "old")

    def test_coordinator_gets_thread_news(self):
        relay.main_log("#4 trip (review)", "both trips fit")
        prompts = []
        with mock.patch.object(relay, "ask_owner_claude", lambda p: prompts.append(p) or "Fine."), \
                mock.patch.object(relay, "pick_react", lambda c, e: None), \
                mock.patch.object(relay, "apply_fixes", lambda c, t: t):
            relay.owner_turn("m1", "and the trips?", None)
            relay.owner_turn("m2", "ok", None)
        self.assertIn("THREAD NEWS", prompts[0])
        self.assertIn("both trips fit", prompts[0])
        self.assertNotIn("THREAD NEWS", prompts[1])  # its own turn is not news
        self.assertEqual(self.sent[-1], "Fine.")

    def test_effort_marked_or_automatic(self):
        relay.CFG["threads"].update(effort_default="medium", effort_high_words=["budget"])
        self.assertIn("effort=", relay.owner_system(relay.CFG))
        relay.thread_dispatch(("new", "a", "plan it | effort=xhigh"), "x")
        relay.thread_dispatch(("new", "Trip budget", "check"), "x")
        relay.thread_dispatch(("new", "skills", "install"), "x")
        db = relay.threads_db()
        self.assertEqual([db[k]["effort"] for k in ("1", "2", "3")], ["xhigh", "high", "medium"])
        with mock.patch.object(relay, "run_claude", return_value=R("ok\n@@STATE: review")) as rc:
            self.jobs[0][1]()
        args = rc.call_args[0][0]
        self.assertEqual(args[args.index("--effort") + 1], "xhigh")
        self.assertNotIn("effort=", rc.call_args[0][2])  # the marker is not part of the brief

    def test_decisions_by_topic(self):
        relay.thread_dispatch(("new", "a", "plan | topic=budget"), "x")
        relay.thread_dispatch(("new", "b", "plan | topic=mom"), "x")
        self.assertIn("topic=budget", relay.thread_list())
        with mock.patch.object(relay, "run_claude",
                               return_value=R("Done.\n@@DECISION: no insulation\n@@DECISION [general]: Mac on power\n@@STATE: review")):
            self.jobs[0][1]()  # #1 budget
        self.assertEqual(self.sent[-1], "#1 a: Done.")  # markers never reach the chat
        for i in range(50):  # lots of other news must not push it out
            relay.take_decisions("#2", f"@@DECISION: mom item {i}", "mom")
        relay.threads_db("3", title="c", state="working", sid="", last="", topic="budget")
        with mock.patch.object(relay, "run_claude", return_value=R("ok\n@@STATE: review")) as rc:
            relay.thread_run("3", "go")
            p = rc.call_args[0][2]
            self.assertIn("[budget]: no insulation", p)
            self.assertIn("[general]: Mac on power", p)
            self.assertNotIn("mom item", p.split("MAIN CHAT")[0])  # other topics stay out of DECISIONS
            relay.take_decisions("#1", "@@DECISION: Lara 1729", "budget")
            relay.thread_run("3", "again")  # running session: only what is new
            self.assertIn("Lara 1729", rc.call_args[0][2])
            self.assertNotIn("no insulation", rc.call_args[0][2].split("MAIN CHAT")[0])

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
