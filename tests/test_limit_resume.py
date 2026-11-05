"""Usage limit: detect it, park the job, run it again after the reset (no AI).
Run: python3 -m unittest discover tests"""
import datetime, json, os, subprocess, sys, tempfile, unittest
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402

AMS = ZoneInfo("Europe/Amsterdam")
T2117 = datetime.datetime(2026, 11, 4, 21, 17, tzinfo=AMS).timestamp()  # when #109 stopped


def at(*a, tz=AMS):
    return datetime.datetime(*a, tzinfo=tz).timestamp()


def run(out, code=1):
    return subprocess.CompletedProcess([], code, out, "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = relay.load_config(write_cfg(self.tmp.name, "[threads]\nenabled = true\n"))
        cfg["owner"]["timezone"] = "Europe/Amsterdam"
        os.makedirs(cfg["paths"]["state_dir"], exist_ok=True)
        self.old = dict(relay.CFG), relay.OWNER
        relay.CFG.clear(), relay.CFG.update(cfg)
        relay.OWNER = cfg["owner"]["jid"]
        relay._inflight.clear(), relay._claimed.clear(), relay.LIMIT_RESUMED.clear()
        self.jobs, self.sent = [], []
        self.p = [mock.patch.object(relay, "workers", {}),
                  mock.patch.object(relay, "allowed", lambda c: True),
                  mock.patch.object(relay, "send_text", lambda c, t, **k: self.sent.append(t)),
                  mock.patch.object(relay, "enqueue", lambda k, fn: self.jobs.append(fn)),
                  mock.patch.object(relay, "digest_soon", lambda: None),
                  mock.patch.object(relay.threading, "Thread", mock.Mock())]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        relay.CFG.clear(), relay.CFG.update(self.old[0])
        relay.OWNER = self.old[1]
        self.tmp.cleanup()


class Parser(Base):
    def test_real_session_limit_line(self):
        out = "You've hit your session limit · resets 9:50pm (Europe/Amsterdam)"
        for code in (1, 0):  # 4 Nov 2026 it came through as the answer
            self.assertEqual(relay.limit_hit(run(out, code)), "9:50pm")
        self.assertEqual(relay.reset_at(out, T2117), at(2026, 11, 4, 21, 50))

    def test_weekly_limit_with_a_date(self):
        out = "You've hit your weekly limit · resets Nov 6, 10am (Europe/Amsterdam)"
        self.assertIsNotNone(relay.limit_hit(run(out, 0)))
        self.assertEqual(relay.reset_at(out, T2117), at(2026, 11, 6, 10, 0))
        self.assertEqual(relay.reset_at("You've hit your weekly limit · resets Jan 2 at 9am", at(2026, 12, 31, 12, 0)),
                         at(2027, 1, 2, 9, 0))

    def test_other_formats(self):
        self.assertEqual(relay.reset_at("5-hour limit reached ∙ resets 3pm (Europe/Amsterdam)", T2117),
                         at(2026, 11, 5, 15, 0))  # 3pm already passed: tomorrow
        self.assertEqual(relay.reset_at("You've hit your Opus limit · resets 2am (Asia/Jerusalem)", T2117),
                         at(2026, 11, 5, 2, 0, tz=ZoneInfo("Asia/Jerusalem")))
        self.assertEqual(relay.reset_at("Your limit will reset at 23:30", T2117), at(2026, 11, 4, 23, 30))
        self.assertEqual(relay.reset_at("Claude AI usage limit reached|1760540400"), 1760540400.0)

    def test_unreadable_time_is_none(self):
        self.assertIsNone(relay.reset_at("Claude usage limit reached.", T2117))
        self.assertIsNone(relay.reset_at("You've hit your monthly spend limit · resets next month", T2117))

    def test_answers_about_limits_are_not_limits(self):
        self.assertIsNone(relay.limit_hit(run("Your rate limit reached its cap yesterday.", 0)))
        self.assertIsNone(relay.limit_hit(run("Done.\nYou've hit your session limit earlier, it is fine now.", 0)))
        self.assertIsNone(relay.limit_hit(run("Fixed: we now catch 'You've hit your session limit' " * 8, 0)))


class Queue(Base):
    def hit(self, key, rec, e, now=T2117):
        relay.inflight(key, rec)
        with mock.patch.object(relay.time, "time", lambda: now):
            with self.assertRaises(relay.ClaudeLimit):
                relay.limit_guard([key], lambda: (_ for _ in ()).throw(e))
        relay.done_job(key)

    def limit(self, when="9:50pm", out="resets 9:50pm (Europe/Amsterdam)"):
        return relay.ClaudeLimit(when, relay.reset_at(out, T2117))

    def test_thread_parked_survives_restart_and_runs_after_reset(self):
        relay.threads_db("109", title="website", state="working")
        self.hit("thread-109-1", {"thread": ["109", "NOTE: go to production"]}, self.limit())
        jobs = json.loads(relay.read(relay.st("limit-paused.json")))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(list(jobs.values())[0]["at"], at(2026, 11, 4, 21, 52))  # reset + 2 min
        self.assertEqual(relay._inflight, {})
        # restart: the new process starts with empty memory; the file is the truth
        with mock.patch.object(relay, "thread_job") as tj:
            relay.resume_jobs()
            relay.resume_parked(at(2026, 11, 4, 21, 51))
            tj.assert_not_called()
            relay.resume_parked(at(2026, 11, 4, 21, 52))
            tj.assert_called_once_with("109", relay.LIMIT_NOTE + "NOTE: go to production")
        news = relay.read(relay.st("threads/main.md"))
        self.assertIn("The Claude limit is over. 1 stopped job(s) go on by themselves now: #109 website.", news)

    def test_dedupe_one_entry_one_note(self):
        relay.threads_db("7", title="x", state="working")
        self.hit("thread-7-1", {"thread": ["7", "do it"]}, self.limit())
        self.hit("thread-7-2", {"thread": ["7", relay.LIMIT_NOTE + "do it"]}, self.limit())
        self.assertEqual(len(relay.parked()), 1)
        with mock.patch.object(relay, "thread_job") as tj:
            relay.resume_parked(at(2026, 11, 5, 0, 0))
        tj.assert_called_once_with("7", relay.LIMIT_NOTE + "do it")

    def test_max_tries_then_one_line_to_the_coordinator(self):
        relay.threads_db("7", title="x", state="working")
        with mock.patch.object(relay, "thread_job"):
            for i in range(5):
                self.hit(f"thread-7-{i}", {"thread": ["7", relay.LIMIT_NOTE * min(i, 1) + "do it"]}, self.limit())
                relay.resume_parked(at(2026, 11, 5, 0, 0))
            self.assertEqual(list(relay.parked().values())[0]["tries"], 5)
            self.hit("thread-7-5", {"thread": ["7", relay.LIMIT_NOTE + "do it"]}, self.limit())
        self.assertEqual(relay.parked(), {})
        self.assertIn("Gave up on #7 x: it hit the Claude limit 5 times", relay.read(relay.st("threads/main.md")))

    def test_success_forgets_the_tries(self):
        relay.inflight("thread-7-9", {"thread": ["7", "do it"]})
        relay.parked(lambda j: j.update({relay.job_id({"thread": ["7", "do it"]}): {
            "rec": {}, "at": None, "tries": 2, "since": T2117}}))
        relay.limit_guard(["thread-7-9"], lambda: None)
        self.assertEqual(relay.parked(), {})

    def test_unknown_reset_time_retries_every_15_min(self):
        self.hit("m5", {"route": ["m5", relay.OWNER, "s", "hi", ""]}, relay.ClaudeLimit(""))
        self.assertEqual(relay.parked()["route:m5"]["at"], T2117 + 900)

    def test_off_when_tries_is_zero(self):
        relay.CFG["limits"]["limit_resume_tries"] = 0
        self.hit("m6", {"route": ["m6", relay.OWNER, "s", "hi", ""]}, self.limit())
        self.assertEqual(relay.parked(), {})

    def test_owner_message_goes_again_with_the_note(self):
        relay.route("m1", relay.OWNER, "s", "plan my week", "")  # -> owner batch
        relay.CFG["owner"]["batch_seconds"] = 0
        with mock.patch.object(relay, "owner_batch", side_effect=self.limit()):
            with self.assertRaises(relay.ClaudeLimit):
                relay.owner_drain()
        self.assertIn("route:m1", relay.parked())
        self.assertNotIn("m1", relay._inflight)
        with mock.patch.object(relay, "route") as r:
            relay.resume_parked(relay.time.time() + 3600)
        r.assert_called_once_with("m1", relay.OWNER, "s", "plan my week", "")
        with mock.patch.object(relay, "quoted_text", lambda *a: ""):
            self.assertTrue(relay.owner_prep("m1", "plan my week", "")[0].startswith(relay.LIMIT_NOTE))

    def test_news_read_by_a_stopped_run_is_given_back(self):
        relay.main_log("#3 x (review)", "result of #3")
        relay.inflight("m2", {"route": ["m2", relay.OWNER, "s", "hi", ""]})
        own = lambda who: False
        with self.assertRaises(relay.ClaudeLimit):
            relay.limit_guard(["m2"], lambda: (relay.main_since("coordinator", own),
                                               (_ for _ in ()).throw(self.limit())))
        self.assertIn("result of #3", relay.main_since("coordinator", own))

    def test_owner_told_once_per_reset_window(self):
        relay._limit_noticed.clear()
        for _ in range(3):
            relay.limit_notice(self.limit())
        self.assertEqual(self.sent, ["I hit my Claude limit. Back at 9:50pm. Stopped work goes on by itself then."])


if __name__ == "__main__":
    unittest.main()
