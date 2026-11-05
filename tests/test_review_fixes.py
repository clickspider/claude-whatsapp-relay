"""Instinct's reliability review of ac4bb56 (5 Nov 2026): one test per finding, each fails on ac4bb56/f19b343."""
import json, os, sys, time, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay  # noqa: E402
import test_threads  # noqa: E402


class ReviewFixes(unittest.TestCase):
    setUp = test_threads.ThreadTests.setUp

    def tearDown(self):
        relay._inflight.clear(), relay.RESUMED.clear()
        test_threads.ThreadTests.tearDown(self)

    def digest(self, answer="ok", **kw):
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value=answer, **kw), \
                mock.patch.object(relay, "coordinator_send") as cs:
            relay.coordinator_digest()
        return cs

    # 1. news past main_chat_chars is never marked read or delivered unseen
    def test_overflow_news_is_paged_not_dropped(self):
        relay.CFG["threads"]["main_chat_chars"] = 300
        t = time.time() - 60
        for n in ("4", "5", "6"):
            relay.threads_db(n, title=f"t{n}", state="review", reported=t)
            relay.main_log(f"#{n} t{n} (review)", f"result {n} " + "x" * 200)
        self.digests.clear()
        self.digest()
        db = relay.threads_db()
        self.assertTrue(db["4"].get("delivered"))  # shown
        self.assertFalse(db["6"].get("delivered"))  # not shown: not delivered, so it never auto-closes
        self.assertIn("result 6", relay.main_since("coordinator", lambda w: False, peek=True))  # still unread
        self.assertTrue(self.digests)  # the next digest pages on

    # 2. a torn state file is never read as empty without a word
    def test_torn_state_file_comes_back_from_its_backup(self):
        relay.threads_db("7", title="film", state="review")
        relay.threads_db("8", title="cv", state="review")  # the write before this one is the .bak
        with open(relay.st("threads.json"), "w") as f:
            f.write('{"7": {"title": "fi')  # torn
        with mock.patch.object(relay.threading, "Timer") as tm:
            db = relay.threads_db()
        self.assertIn("7", db)  # restored, not {}
        tm.assert_called_once()  # the owner is told
        self.assertTrue([f for f in os.listdir(relay.st("")) if f.startswith("threads.json.corrupt-")])

    def test_write_is_atomic(self):
        p = relay.st("x.json")
        relay.write(p, "{}")
        with mock.patch.object(relay.os, "replace", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                relay.write(p, '{"a": 1}')
        self.assertEqual(relay.read(p), "{}")  # the old version is intact

    # 3. the 500 KB cut lands on an entry boundary
    def test_timeline_cut_starts_on_an_entry(self):
        for i in range(60):
            relay.main_log(f"#{i} t (review)", "y" * 9_000)
        self.assertTrue(relay.read(relay.st("threads/main.md")).startswith("**"))

    # 4. a contact answer cut by a restart: the bridge decides, never a blind re-run
    def contact_cut(self, got):
        relay.write(relay.st("inflight.json"), json.dumps({"m1": {
            "route": ["m1", "c@s.whatsapp.net", "c", "hi", ""], "sending": True, "after": 5}}))
        relay._inflight.clear()
        with mock.patch.object(relay, "landed", return_value=got), mock.patch.object(relay, "route") as r:
            relay.resume_jobs()
        return r

    def test_contact_send_that_landed_is_not_run_again(self):
        self.contact_cut(True).assert_not_called()
        self.assertEqual(self.sent, [])
        self.assertNotIn("m1", relay._inflight)

    def test_contact_send_that_never_landed_runs_again(self):
        self.contact_cut(False).assert_called_once()

    # 5. a crash inside resume_jobs loses no record
    def test_resume_keeps_records_until_their_next_step_is_registered(self):
        relay.threads_db("3", title="a", state="working")
        relay.write(relay.st("inflight.json"), json.dumps({
            "thread-3-1": {"thread": ["3", "go"]}, "m2": {"route": ["m2", relay.OWNER, "o", "hi", ""]}}))
        relay._inflight.clear()
        with mock.patch.object(relay, "route", side_effect=SystemExit):  # the process dies mid-resume
            with self.assertRaises(SystemExit):
                relay.resume_jobs()
        self.assertEqual(set(json.loads(relay.read(relay.st("inflight.json")))), {"thread-3-1", "m2"})

    # 7. a digest that fails (not the limit) tries again
    def test_failed_digest_is_armed_again(self):
        relay.main_log("#4 t (review)", "done")
        with mock.patch.object(relay.threading, "Timer") as tm:
            with self.assertRaises(RuntimeError):
                self.digest(side_effect=RuntimeError("timeout"))
        self.assertEqual(tm.call_args[0][0], 300)

    # 10. the "still needed?" ask says how to keep the thread open
    def test_still_needed_ask_says_how_to_keep_it(self):
        relay.threads_db("9", title="z", state="blocked", reported=time.time() - 8 * 86400)
        notes = []
        with mock.patch.object(relay, "agent_note", lambda n, t, dry=False: notes.append(t)):
            relay.thread_auto_close()
        self.assertIn("@@TO #9", notes[0])

    # 12. an inbox note whose turn never ended goes back to the inbox
    def test_inbox_note_cut_by_a_restart_is_delivered_again(self):
        d = os.path.join(self.tmp.name, "mom")
        os.makedirs(os.path.join(d, "inbox", "done"))
        with open(os.path.join(d, "inbox", "done", "n.md"), "w") as f:
            f.write("tell her the date")
        relay.write(relay.st("inflight.json"), json.dumps({"inbox:mom:n.md": {"inbox": ["mom", "n.md"]}}))
        relay._inflight.clear()
        with mock.patch.object(relay, "by_name", return_value=("j", {"dir": d})):
            relay.resume_jobs()
        self.assertTrue(os.path.exists(os.path.join(d, "inbox", "n.md")))


class JobDedupThreadsOff(unittest.TestCase):
    """6. the job-end dedup also holds with threads off (the default): a crash before the job file is
    removed never sends the note twice."""
    def test_threads_off(self):
        import tempfile
        d = tempfile.mkdtemp()
        old = relay.CFG
        relay.CFG = {"paths": {"state_dir": d}, "owner": {"timezone": "Europe/Amsterdam"}, "threads": {"enabled": False}}
        notes = []
        try:
            os.makedirs(os.path.join(d, "jobs"))
            job = {"title": "#1 run", "pid": 1, "started": "Mon Jan  1 00:00:00 2001", "log": "", "exit": 0}
            for _ in range(2):  # the second time = the file came back (crash before os.remove)
                with open(os.path.join(d, "jobs", "1.json"), "w") as f:
                    json.dump(job, f)
                with mock.patch.object(relay, "agent_note", lambda n, t, dry=False: notes.append(t)):
                    relay.job_watch()
        finally:
            relay.CFG = old
        self.assertEqual(len(notes), 1)


if __name__ == "__main__":
    unittest.main()
