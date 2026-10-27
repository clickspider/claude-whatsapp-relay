"""An update stops at once and unfinished jobs go on after the restart. Run: python3 -m unittest discover tests"""
import json, os, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = relay.load_config(write_cfg(self.tmp.name, "[threads]\nenabled = true\n"))
        os.makedirs(cfg["paths"]["state_dir"], exist_ok=True)
        self.old = dict(relay.CFG), relay.OWNER
        relay.CFG.clear(), relay.CFG.update(cfg)
        relay.OWNER = cfg["owner"]["jid"]
        relay._inflight.clear(), relay._claimed.clear(), relay.RESUMED.clear()
        self.jobs = []
        self.p = [mock.patch.object(relay, "workers", {}),
                  mock.patch.object(relay, "allowed", lambda c: True),
                  mock.patch.object(relay, "send_text", lambda *a, **k: None),
                  mock.patch.object(relay, "enqueue", lambda k, fn: self.jobs.append(fn)),
                  mock.patch.object(relay.threading, "Thread", mock.Mock())]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        relay.CFG.clear(), relay.CFG.update(self.old[0])
        relay.OWNER = self.old[1]
        self.tmp.cleanup()

    def saved(self):
        return json.loads(relay.read(relay.st("inflight.json")) or "{}")

    def test_queued_message_stays_until_its_job_ends(self):
        relay.route("m1", relay.OWNER, "s", "hello", "")  # -> owner batch
        self.assertIn("m1", self.saved())
        with mock.patch.object(relay, "owner_batch") as ob:
            relay.CFG["owner"]["batch_seconds"] = 0
            relay.owner_drain()
        ob.assert_called_once()
        self.assertEqual(self.saved(), {})

    def test_command_handled_at_once_is_not_kept(self):
        relay.route("m2", relay.OWNER, "s", "/threads", "")
        self.assertEqual(self.saved(), {})

    def test_thread_job_done_on_error_too(self):
        relay.threads_db("3", title="x", state="working", sid="", last="")
        with mock.patch.object(relay, "thread_run", side_effect=RuntimeError("boom")):
            relay.thread_job("3", "do it")
            self.assertEqual(len(self.saved()), 1)
            with self.assertRaises(RuntimeError):
                self.jobs[-1]()
        self.assertEqual(self.saved(), {})

    def test_resume_runs_unfinished_jobs_with_note(self):
        relay.threads_db("3", title="x", state="working", sid="", last="")
        relay.write(relay.st("inflight.json"), json.dumps({
            "m9": {"route": ["m9", relay.OWNER, "s", "plan my week", ""]},
            "thread-3-1": {"thread": ["3", "find movers"]}}))
        with mock.patch.object(relay, "thread_job") as tj, mock.patch.object(relay, "owner_add") as oa:
            relay.resume_jobs()
        oa.assert_called_once_with("m9", "plan my week", "")
        self.assertIn("m9", relay.RESUMED)
        self.assertTrue(tj.call_args[0][1].startswith(relay.RESUME_NOTE))
        with mock.patch.object(relay, "quoted_text", lambda c, m: ""):
            self.assertTrue(relay.owner_prep("m9", "plan my week", "")[0].startswith(relay.RESUME_NOTE))

    def test_update_stops_runs_at_once(self):
        p = mock.Mock()
        relay._procs.add(p)
        relay.busy[0] = 2  # busy no longer blocks the restart
        try:
            with mock.patch.object(relay, "START_MTIME", 0), \
                    mock.patch.object(relay.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                    mock.patch.object(relay.os, "_exit", side_effect=SystemExit) as ex:
                with self.assertRaises(SystemExit):
                    relay.restart_if_changed()
            p.terminate.assert_called_once()
            ex.assert_called_once_with(0)
        finally:
            relay._procs.discard(p)
            relay.busy[0] = 0


if __name__ == "__main__":
    unittest.main()
