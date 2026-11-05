"""A restart or a time limit never makes a thread report twice (4 Nov 2026, #111). Run: python3 -m unittest discover tests"""
import json, os, subprocess, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
import test_threads  # noqa: E402


class ThreadResumeMerge(unittest.TestCase):
    setUp, tearDown = test_threads.ThreadTests.setUp, test_threads.ThreadTests.tearDown

    def resume(self, inflight):
        relay.write(relay.st("inflight.json"), json.dumps(inflight))
        with mock.patch.object(relay, "thread_job") as tj:
            relay.resume_jobs()
        return [c[0] for c in tj.call_args_list]

    def test_queued_notes_become_one_turn_with_one_note(self):
        relay.threads_db("3", title="x", state="working", sid="s", last="")
        calls = self.resume({"thread-3-1": {"thread": ["3", relay.RESUME_NOTE + "make v3"], "ran": True},
                             "thread-3-2": {"thread": ["3", "fix the table"]},
                             "thread-3-3": {"thread": ["3", "report short"]}})
        self.assertEqual(len(calls), 1)
        n, prompt = calls[0]
        self.assertEqual(n, "3")
        self.assertEqual(prompt.count(relay.RESUME_NOTE), 1)
        self.assertTrue(prompt.startswith(relay.RESUME_NOTE + "(3 notes"))
        self.assertIn("#1 make v3\n\n#2 fix the table\n\n#3 report short", prompt)

    def test_note_that_never_started_gets_no_resume_note(self):
        relay.threads_db("4", title="y", state="working", sid="s", last="")
        self.assertEqual(self.resume({"thread-4-1": {"thread": ["4", "check the eyes"]}}), [("4", "check the eyes")])

    def test_note_kept_for_a_job_already_resumed(self):
        relay.threads_db("6", title="v", state="working", sid="s", last="")
        calls = self.resume({"thread-6-1": {"thread": ["6", relay.TIMEOUT_NOTE + "render 4K"]}})
        self.assertEqual(calls, [("6", relay.RESUME_NOTE + "render 4K")])

    def test_started_job_is_marked_ran(self):
        relay.threads_db("5", title="z", state="working", sid="s", last="")
        seen = []
        with mock.patch.object(relay, "thread_run", lambda n, p: seen.append(dict(relay._inflight))):
            relay.thread_job("5", "go")
            self.jobs.pop()[1]()
        self.assertEqual([r.get("ran") for r in seen[0].values()], [True])
        self.assertEqual(relay._inflight, {})

    def test_timeout_goes_to_coordinator_and_goes_on_once(self):
        relay.threads_db("7", title="film", state="working", sid="s7", last="")
        boom = subprocess.TimeoutExpired("claude", 1800)
        with mock.patch.object(relay, "run_claude", side_effect=boom), \
                mock.patch.object(relay, "thread_job") as tj:
            relay.thread_run("7", relay.RESUME_NOTE + "make v3")  # no raise: no raw error to the owner
            t = relay.threads_db()["7"]
            self.assertEqual((t["state"], t["sid"], t["timeouts"]), ("working", "s7", 1))
            tj.assert_called_once_with("7", relay.TIMEOUT_NOTE + "make v3")
            relay.thread_run("7", relay.TIMEOUT_NOTE + "make v3")  # second time in a row: it waits
            self.assertEqual(tj.call_count, 1)
        self.assertEqual(relay.threads_db()["7"]["state"], "blocked")
        self.assertEqual(self.sent, [])
        news = relay.main_since("coordinator", lambda who: False, peek=True)
        self.assertTrue(relay.THREAD_RESULT_RE.search(news))
        self.assertIn("time limit", news)
        self.assertIn("Not checked", news)  # never a fake completion

    def test_good_run_resets_the_timeout_count(self):
        relay.threads_db("8", title="w", state="working", sid="s8", last="", timeouts=1)

        class R:
            stdout, stderr, returncode = "ok\n@@STATE: review", "", 0
        with mock.patch.object(relay, "run_claude", return_value=R()):
            relay.thread_run("8", "go")
        self.assertEqual(relay.threads_db()["8"]["timeouts"], 0)

    def test_job_id_ignores_every_note(self):
        a = relay.job_id({"thread": ["3", "go"]})
        for note in (relay.RESUME_NOTE, relay.LIMIT_NOTE, relay.TIMEOUT_NOTE + relay.RESUME_NOTE):
            self.assertEqual(relay.job_id({"thread": ["3", note + "go"]}), a)


if __name__ == "__main__":
    unittest.main()
