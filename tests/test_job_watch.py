"""A detached job's end reaches the coordinator; finished work is never @@SKIP (5 Nov 2026, #134)."""
import json, os, subprocess, sys, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay  # noqa: E402
import test_threads  # noqa: E402


class JobWatch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conf = os.path.join(self.dir, "relay.config.toml")
        with open(self.conf, "w") as f:
            f.write('[paths]\nstate_dir = "."\n')
        self.notes = []
        self.old = {k: getattr(relay, k) for k in ("CFG", "agent_note")}
        relay.CFG = {"paths": {"state_dir": self.dir}, "owner": {"timezone": "Europe/Amsterdam"}}
        relay.agent_note = lambda name, note, dry=False: self.notes.append((name, note))

    def tearDown(self):
        for k, v in self.old.items():
            setattr(relay, k, v)

    def register(self, *args):
        return subprocess.run([sys.executable, "-I", os.path.join(HERE, "scripts/watch_job.py"), "--config", self.conf,
                               *args], capture_output=True, text=True)

    def test_running_job_is_quiet_and_its_end_is_reported_once(self):
        log = os.path.join(self.dir, "run.log")
        with open(log, "w") as f:
            f.write("old line\n" * 20 + "transcript: 30/30 ok\nindex done\n")
        p = subprocess.Popen(["sleep", "30"])
        r = self.register("#7 long transcription run", str(p.pid), log)
        self.assertIn("WATCHING", r.stdout, r.stderr)
        relay.job_watch()
        self.assertEqual(self.notes, [])  # still running: nothing
        p.kill(); p.wait()
        relay.job_watch()
        self.assertEqual(len(self.notes), 1)
        name, note = self.notes[0]
        self.assertEqual(name, "job")
        self.assertIn("#7 long transcription run", note)
        self.assertIn("index done", note)
        relay.job_watch()
        self.assertEqual(len(self.notes), 1)  # never twice
        self.assertEqual(os.listdir(os.path.join(self.dir, "jobs")), [])

    def test_reused_pid_counts_as_ended(self):
        d = os.path.join(self.dir, "jobs")
        os.makedirs(d)
        with open(os.path.join(d, "1.json"), "w") as f:  # pid 1 lives, but started at another time
            json.dump({"title": "old job", "pid": 1, "started": "Mon Jan  1 00:00:00 2001", "log": ""}, f)
        relay.job_watch()
        self.assertEqual([n for n, _ in self.notes], ["job"])

    def test_run_mode_reports_the_exit_code(self):
        log = os.path.join(self.dir, "run.log")
        for cmd, want in ((["sh", "-c", "echo all done"], "exit code 0 (OK)"), (["sh", "-c", "echo boom; exit 3"], "exit code 3 (FAILED)")):
            r = self.register("#7 run", "--run", log, "--", *cmd)
            self.assertEqual(r.returncode, 0, r.stderr)
            relay.job_watch()
            self.assertIn(want, self.notes[-1][1])
        self.assertIn("boom", self.notes[-1][1])
        self.assertEqual(len(self.notes), 2)

    def test_a_logged_end_is_never_sent_again(self):
        p = subprocess.Popen(["sleep", "30"])
        self.register("#7 run", str(p.pid))
        started = json.loads(relay.read(os.path.join(self.dir, "jobs", f"{p.pid}.json")))["started"]
        p.kill(); p.wait()
        os.makedirs(os.path.join(self.dir, "threads"))
        with open(os.path.join(self.dir, "threads", "main.md"), "w") as f:  # a crash after the note, before cleanup
            f.write(f"**2026-11-05 19:00 🔔 job:** #7 run ended [job {p.pid} {started}]\n")
        relay.job_watch()
        self.assertEqual(self.notes, [])
        self.assertEqual(os.listdir(os.path.join(self.dir, "jobs")), [])

    # The two crash gaps Instinct asked about (5 Nov 2026): no lost completion, no duplicate note.
    def test_crash_before_the_note_is_written_loses_nothing(self):
        self.register("#7 run", "--run", os.path.join(self.dir, "run.log"), "--", "sh", "-c", "echo done")
        def crash(name, note, dry=False):
            raise OSError("relay died while writing the note")
        relay.agent_note = crash
        with self.assertRaises(OSError):
            relay.job_watch()
        self.assertEqual(len(os.listdir(os.path.join(self.dir, "jobs"))), 1)  # the job file is still there
        relay.agent_note = lambda name, note, dry=False: self.notes.append((name, note))
        relay.job_watch()  # the next check after the restart
        relay.job_watch()
        self.assertEqual(len(self.notes), 1)
        self.assertIn("exit code 0 (OK)", self.notes[0][1])

    def test_crash_after_the_note_before_cleanup_sends_no_duplicate(self):
        main = os.path.join(self.dir, "threads", "main.md")
        os.makedirs(os.path.dirname(main))
        def note(name, text, dry=False):  # like the real agent_note: the line lands in the timeline
            self.notes.append((name, text))
            with open(main, "a") as f:
                f.write(f"**2026-11-05 19:00 🔔 {name}:** {text}\n")
        relay.agent_note = note
        self.register("#7 run", "--run", os.path.join(self.dir, "run.log"), "--", "sh", "-c", "echo done")
        with mock.patch.object(relay.os, "remove", side_effect=OSError("relay died before the cleanup")):
            with self.assertRaises(OSError):
                relay.job_watch()
        self.assertEqual(len(self.notes), 1)
        relay.job_watch()  # after the restart: the key is in the timeline, so no second note
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(os.listdir(os.path.join(self.dir, "jobs")), [])

    # Instinct's review (5 Nov 2026 18:47)
    def test_dead_wrapper_with_a_living_command_is_not_completed(self):
        log = os.path.join(self.dir, "run.log")
        w = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "scripts/watch_job.py"), "--config", self.conf,
                              "#7 run", "--run", log, "--", "sleep", "30"])
        d = os.path.join(self.dir, "jobs")
        for _ in range(50):  # wait until the wrapper wrote the command's own pid
            fs = os.listdir(d) if os.path.isdir(d) else []
            if fs and "child" in json.loads(relay.read(os.path.join(d, fs[0]))):
                break
            time.sleep(0.05)
        child = json.loads(relay.read(os.path.join(d, fs[0])))["child"]["pid"]
        w.kill(); w.wait()  # the wrapper dies, the command runs on
        relay.job_watch()
        self.assertEqual(self.notes, [])  # still running: never announced as done
        os.kill(child, 9)
        for _ in range(50):
            if not relay.proc_start(child):
                break
            time.sleep(0.05)
        relay.job_watch()
        self.assertIn("OUTCOME UNKNOWN", self.notes[0][1])
        self.assertNotIn("exit code 0", self.notes[0][1])

    def test_the_command_keeps_its_own_config_argument(self):
        log = os.path.join(self.dir, "run.log")
        r = self.register("#7 run", "--run", log, "--", "sh", "-c", 'echo "args: $*"', "x", "--config", "model.toml")
        self.assertEqual(r.returncode, 0, r.stderr)
        relay.job_watch()
        self.assertIn("args: --config model.toml", self.notes[0][1])

    def test_registration_errors_are_never_silent(self):
        log = os.path.join(self.dir, "r.log")
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "scripts/watch_job.py"), "#1 x", "--run", log,
                            "--config", os.path.join(self.dir, "missing.toml"), "--", "true"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        with open(log) as f:
            self.assertIn("NOT REGISTERED", f.read())  # under nohup >/dev/null the log still says why
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "scripts/watch_job.py"), "#1 x", "123", "--config"],
                           capture_output=True, text=True)
        self.assertIn("Give a detached job an owner", r.stderr)  # usage, not an IndexError

    def test_log_tail_reads_from_the_end(self):
        log = os.path.join(self.dir, "big.log")
        with open(log, "w") as f:
            f.write("x" * 5_000_000 + "\nlast line\n")
        self.assertTrue(relay.log_tail(log).endswith("last line"))

    def test_bad_input_is_refused(self):
        self.assertIn("NOT REGISTERED", self.register("x", "999999").stderr)
        self.assertNotEqual(self.register("only-title").returncode, 0)

    def test_note_reaches_the_digest(self):
        self.assertTrue(relay.THREAD_RESULT_RE.search("**2026-11-05 19:50 🔔 job:**"))

    def test_finished_work_is_never_skip(self):
        self.assertIn("now finished", relay.DIGEST_PROMPT)
        self.assertIn("never @@SKIP", relay.COORDINATOR_RULES)
        self.assertIn("watch_job.py", relay.WAIT_RULE)



class DigestCrashGaps(unittest.TestCase):
    """The news is marked read only after it went out; a restart in between loses nothing and sends nothing twice."""
    setUp = test_threads.ThreadTests.setUp

    def tearDown(self):
        relay.inflight("digest")  # in the relay the owner queue clears it (done_job)
        test_threads.ThreadTests.tearDown(self)

    def unread(self):
        return relay.main_since("coordinator", lambda w: False, peek=True)

    def test_restart_during_the_digest_turn_keeps_the_news(self):
        relay.main_log("🔔 job", "#7 run: the job ended, exit code 0 (OK)")
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", side_effect=relay.StopRun()):
            with self.assertRaises(relay.StopRun):
                relay.coordinator_digest()
        self.assertIn("exit code 0", self.unread())  # the resumed digest still finds it

    def test_sent_digest_marks_the_news_read(self):
        relay.main_log("🔔 job", "#7 run ended")
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value="#7 is done."), \
                mock.patch.object(relay, "coordinator_send") as cs:
            relay.coordinator_digest()
        cs.assert_called_once()
        self.assertEqual(self.unread(), "")

    def test_restart_while_the_digest_went_out_is_not_sent_twice(self):
        relay.main_log("🔔 job", "#7 run ended")
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value="#7 is done."), \
                mock.patch.object(relay, "coordinator_send", side_effect=relay.StopRun()):
            with self.assertRaises(relay.StopRun):
                relay.coordinator_digest()
        n = len(self.sent)
        with mock.patch.object(relay, "digest_soon") as ds:
            relay.resume_jobs()
        ds.assert_not_called()
        self.assertIn("Delivery is", self.sent[n])
        self.assertEqual(self.unread(), "")

    def cut_send(self):
        relay.main_log("🔔 job", "#7 run ended")
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value="#7 is done."), \
                mock.patch.object(relay, "bridge_rowid", return_value=10), \
                mock.patch.object(relay, "coordinator_send", side_effect=relay.StopRun()):
            with self.assertRaises(relay.StopRun):
                relay.coordinator_digest()
        self.assertEqual(relay._inflight["digest"]["text"], "#7 is done.")
        return len(self.sent)

    def test_cut_send_that_landed_is_not_sent_again(self):
        n = self.cut_send()
        with mock.patch.object(relay, "landed", return_value=True), mock.patch.object(relay, "digest_soon") as ds:
            relay.resume_jobs()
        ds.assert_not_called()
        self.assertEqual(self.sent[n:], [])  # the receipt says it landed: no copy, no "uncertain" line
        self.assertEqual(self.unread(), "")

    def test_cut_send_that_never_landed_goes_out_again(self):
        self.cut_send()
        with mock.patch.object(relay, "landed", return_value=False), mock.patch.object(relay, "digest_soon") as ds:
            relay.resume_jobs()
        ds.assert_called()
        self.assertIn("#7 run ended", self.unread())  # not lost: the next digest sends it

    def test_landed_reads_the_bridge_after_the_anchor(self):
        import sqlite3
        store = relay.CFG["bridge"]["store"]
        os.makedirs(store, exist_ok=True)
        c = sqlite3.connect(os.path.join(store, "messages.db"))
        c.execute("CREATE TABLE messages (id TEXT, chat_jid TEXT, content TEXT, is_from_me BOOLEAN)")
        c.execute("INSERT INTO messages VALUES ('1', 'me', 'old answer', 1)")
        c.commit()
        after = relay.bridge_rowid()
        self.assertFalse(relay.landed("me", after, "1. #7 is done.\n2. #8 waits for you."))
        c.execute("INSERT INTO messages VALUES ('2', 'me', '1. #7 is done.', 1)")
        c.commit(), c.close()
        self.assertFalse(relay.landed("me", after, "1. #7 is done.\n2. #8 waits for you."))  # first bubble only: cut
        self.assertTrue(relay.landed("me", after, "1. #7 is done."))
        self.assertTrue(relay.landed("me", after))  # no text (a contact): anything of ours counts
        self.assertIsNone(relay.landed("me", None, "x"))
        c = sqlite3.connect(os.path.join(store, "messages.db"))
        mid = c.execute("SELECT max(rowid) FROM messages").fetchone()[0]
        c.execute("INSERT INTO messages VALUES ('3', 'me', '1. #9 is done.\n2. #10 waits.', 1)")
        c.commit(), c.close()
        # a marker line between two lines was removed before the send: the sent text is still found
        self.assertTrue(relay.landed("me", mid, "1. #9 is done.\n@@TO #9: thanks\n2. #10 waits."))

    def test_read_offsets_never_go_back(self):
        relay.main_log("🔔 job", "#7 run ended")
        relay.main_since("coordinator", lambda w: False)  # a newer read
        relay.main_mark("coordinator", 5)  # a late recovery with an old offset
        self.assertEqual(self.unread(), "")

    def test_the_500kb_cut_never_brings_old_news_back(self):
        relay.main_log("#3 film (review)", "old news " + "x" * 450_000)
        relay.main_since("coordinator", lambda w: False)  # all read
        _, upto = relay.main_since("coordinator", lambda w: False, peek=True, offset=True)  # a digest in flight
        relay.main_log("#3 film (review)", "y" * 60_000)  # > 500 KB: the front is cut
        relay.main_log("#4 cv (done)", "new news")
        self.assertNotIn("x" * 50, self.unread())  # before: the whole file came back, every turn
        self.assertIn("new news", self.unread())
        relay.main_mark("coordinator", upto)  # the in-flight digest from before the cut ends
        self.assertIn("new news", self.unread())
        for _ in range(3):  # paged: the long entry, then the rest
            relay.main_mark("coordinator", relay.main_since("coordinator", lambda w: False, peek=True, offset=True)[1])
        self.assertEqual(self.unread(), "")

    def test_restart_during_an_owner_turn_keeps_the_news(self):
        relay.main_log("#3 film (review)", "v4 ready")
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", side_effect=relay.StopRun()):
            with self.assertRaises(relay.StopRun):
                relay.owner_turn("n1", "whats up", "")
        self.assertIn("v4 ready", self.unread())
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value="all good"), \
                mock.patch.object(relay, "coordinator_send"):
            relay.owner_turn("n2", "whats up", "")
        self.assertEqual(self.unread(), "")

    def test_news_whose_digest_timer_died_is_picked_up_after_a_restart(self):
        relay.main_log("🔔 job", "#7 run ended")  # crash before the timer fired: no inflight record
        relay.write(relay.st("inflight.json"), "{}")
        with mock.patch.object(relay, "digest_soon") as ds:
            relay.resume_jobs()
        ds.assert_called_once()


if __name__ == "__main__":
    unittest.main()
