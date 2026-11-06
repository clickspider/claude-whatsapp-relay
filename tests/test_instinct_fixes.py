"""Lessons from the Instinct interview (4 Nov 2026): night digest, @@SKIP, prompt lines, /stop, no
double send after a restart. Run: python3 -m unittest discover tests"""
import datetime, json, os, queue, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
import test_threads  # noqa: E402

AT = lambda h: datetime.datetime(2026, 11, 4, h, 30, tzinfo=datetime.timezone.utc)


class InstinctFixes(unittest.TestCase):
    setUp, tearDown = test_threads.ThreadTests.setUp, test_threads.ThreadTests.tearDown

    def news(self):
        relay.main_log("#3 film (review)", "v4 ready")

    # 5: night
    def test_night_hours_and_config(self):
        relay.CFG["owner"]["night"] = [23, 8]
        self.assertEqual([relay.owner_night(AT(h)) for h in (22, 23, 2, 7, 8)], [False, True, True, True, False])
        relay.CFG["owner"]["night"] = []
        self.assertFalse(relay.owner_night(AT(2)))
        bad = dict(relay.CFG, owner=dict(relay.CFG["owner"], night=[23]))
        self.assertTrue(any("owner.night" in e for e in relay.validate(bad)[0]))

    def test_night_digest_skip_keeps_the_news_for_the_morning(self):
        relay.CFG["owner"]["night"] = [23, 8]
        self.news()
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=True), \
                mock.patch.object(relay, "ask_owner_claude", return_value="@@SKIP") as ask, \
                mock.patch.object(relay, "digest_after_night") as later, \
                mock.patch.object(relay, "coordinator_send") as cs:
            relay.coordinator_digest()
        self.assertIn("night", ask.call_args[0][0])
        later.assert_called_once()
        cs.assert_not_called()
        self.assertIn("v4 ready", relay.main_since("coordinator", lambda w: False, peek=True))

    def test_night_digest_urgent_is_sent_and_read(self):
        relay.CFG["owner"]["night"] = [23, 8]
        self.news()
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=True), \
                mock.patch.object(relay, "ask_owner_claude", return_value="Mom needs you now."), \
                mock.patch.object(relay, "coordinator_send") as cs:
            relay.coordinator_digest()
        cs.assert_called_once()
        self.assertEqual(relay.main_since("coordinator", lambda w: False, peek=True), "")

    def test_digest_after_night_waits_until_the_end_hour(self):
        relay.CFG["owner"]["night"] = [23, 8]
        with mock.patch.object(relay.threading, "Timer") as tm:
            relay.digest_after_night(AT(23))
        self.assertEqual(tm.call_args[0][0], 8.5 * 3600)

    # B: @@SKIP never goes out, for agents too
    def test_agent_skip_sends_nothing(self):
        a = {"name": "alex", "trust": "auto", "checkins": False, "images": False, "files": False}
        with mock.patch.object(relay, "apply_fixes", lambda c, o: o), \
                mock.patch.object(relay, "pick_react"), \
                mock.patch.object(relay, "send_text") as st:
            relay.deliver("1@s.whatsapp.net", a, "@@SKIP", False)
        st.assert_not_called()

    # 4/6/8: prompt lines
    def test_prompt_lines(self):
        p = relay.owner_system(relay.CFG)
        self.assertIn("a later correction wins", p)
        self.assertIn("names and numbers may be misheard", p)
        self.assertIn("never ask for passwords", p)
        with mock.patch.object(relay, "download", return_value="/tmp/x.ogg"), \
                mock.patch.object(relay, "transcribe", return_value="send 50 to Ran"), \
                mock.patch.object(relay, "RESUMED", set()):
            text, _ = relay.owner_prep("v1", "", "audio")
        self.assertTrue(text.startswith("(voice note, transcribed) send 50 to Ran"))

    # C: /stop
    def stop_setup(self):
        mine, other = mock.Mock(relay_key="owner"), mock.Mock(relay_key="#4")
        qo, q4 = queue.Queue(), queue.Queue()
        qo.put((lambda: None, relay.stop_mark("owner"))), q4.put((lambda: None, relay.stop_mark("#4")))  # (job, /stop count when queued)
        relay.threads_db("4", title="t", state="working", sid="s", last="")
        relay.inflight("thread-4-1", {"thread": ["4", "go"]})
        relay.inflight("m7", {"route": ["m7", relay.OWNER, "s", "plan", ""]})
        relay._procs.update({mine, other})
        return mine, other, {"owner": qo, "#4": q4}

    def stop(self, text, workers):
        try:
            with mock.patch.object(relay, "workers", workers), mock.patch.object(relay, "quoted_text", lambda c, m: ""):
                relay.route("s1", relay.OWNER, "s", text, "")
        finally:
            relay._procs.clear()

    def test_plain_stop_is_only_this_chat(self):
        mine, other, w = self.stop_setup()
        self.stop("/stop", w)
        mine.terminate.assert_called_once()
        other.terminate.assert_not_called()
        self.assertTrue(w["owner"].empty())
        self.assertFalse(w["#4"].empty())
        self.assertEqual(list(json.loads(relay.read(relay.st("inflight.json")))), ["thread-4-1"])
        self.assertEqual(relay.threads_db()["4"]["state"], "working")
        self.assertIn("Stopped this chat", self.sent[-1])
        relay._stopped.clear()

    def test_thread_stop_and_stop_all(self):
        mine, other, w = self.stop_setup()
        self.stop("#4 /stop", w)
        other.terminate.assert_called_once()
        mine.terminate.assert_not_called()
        self.assertEqual(relay.threads_db()["4"]["state"], "blocked")
        self.assertEqual(relay.stop_target("x", "/stop #4"), {"#4"})
        with mock.patch.object(relay, "quoted_text", lambda c, m: '[Replying to your message: "#4 film (review)'):
            self.assertEqual(relay.stop_target("x", "/stop"), {"#4"})
        self.assertIs(relay.stop_target("x", "stop the car"), False)
        mine, other, w = self.stop_setup()
        self.stop("/stop all", w)
        mine.terminate.assert_called_once()
        other.terminate.assert_called_once()
        self.assertEqual(json.loads(relay.read(relay.st("inflight.json"))), {})
        self.assertIn("Nothing from them is sent", self.sent[-1])
        relay._stopped.clear()

    def test_morning_digest_and_quiet_night_turn(self):
        relay.CFG["owner"]["night"] = [23, 8]
        with mock.patch.object(relay.threading, "Timer"):
            relay.digest_after_night(AT(23))
        self.news()
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value="@@SKIP") as ask, \
                mock.patch.object(relay, "coordinator_send"):
            relay.coordinator_digest()
        self.assertIn("matters to the owner this morning", ask.call_args_list[0][0][0])
        self.assertNotIn("morning", relay._digest)
        self.news()
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=True), \
                mock.patch.object(relay, "ask_owner_claude", return_value="ok") as ask, \
                mock.patch.object(relay, "coordinator_send") as cs:
            relay.owner_turn("n1", "whats up", "")
        self.assertTrue(ask.call_args[0][0].startswith(relay.NIGHT_TURN))
        self.assertEqual(cs.call_args[0][1], "whats up")  # the owner's own words stay clean
        self.assertTrue(relay._digest["due"])
        self.assertIn("v4 ready", relay.main_since("coordinator", lambda w: False, peek=True))

    def test_stopped_run_raises_and_worker_stays_quiet(self):
        class P:
            def communicate(self, *a, **k):
                relay._stopped.add(self)
                return "half an answer", ""
        with mock.patch.object(relay.subprocess, "Popen", return_value=P()):
            with self.assertRaises(relay.StopRun):
                relay.run_claude(["claude"], "/tmp", "hi")

    # D: no double send after a restart
    def test_answer_being_sent_is_not_run_again(self):
        relay.inflight("m1", {"route": ["m1", relay.OWNER, "s", "book the table", ""]})
        relay.coordinator_send("Booked.", "book the table",
                               refs={"#1": ("m1", relay.OWNER, "book the table")})
        self.assertTrue(relay._inflight["m1"]["sending"])
        relay.write(relay.st("inflight.json"), json.dumps(relay._inflight))
        n = len(self.sent)
        with mock.patch.object(relay, "route") as rt:
            relay.resume_jobs()
        rt.assert_not_called()
        self.assertIn("Delivery is uncertain", self.sent[n])
        relay.inflight("m1")

    def test_agent_answer_being_sent_is_not_run_again(self):
        a = {"name": "alex"}
        relay.inflight("c1", {"route": ["c1", "9@s.whatsapp.net", "9", "is 8 ok?", ""]})
        with mock.patch.object(relay, "member", return_value=""), \
                mock.patch.object(relay, "media_text", lambda a, c, m, t, mt: t), \
                mock.patch.object(relay, "log_thread"), mock.patch.object(relay, "full_jid", lambda x: x), \
                mock.patch.object(relay, "run_agent_told", return_value="yes"), \
                mock.patch.object(relay, "deliver", side_effect=lambda j, a, out, dry, refs=None, **k:
                                  relay.sending_text(refs, out)) as dv:
            relay.contact_turn("9@s.whatsapp.net", a, "c1", "is 8 ok?", "")
        dv.assert_called_once()
        self.assertTrue(relay._inflight["c1"]["sending"])
        relay.write(relay.st("inflight.json"), json.dumps(relay._inflight))
        n = len(self.sent)
        with mock.patch.object(relay, "route") as rt, mock.patch.object(relay, "by_jid", return_value=a):
            relay.resume_jobs()
        rt.assert_not_called()
        self.assertIn("alex: is 8 ok?", self.sent[n])
        relay.inflight("c1")

    # edits: the bridge's media_type "edit" reaches the agent as a correction
    def test_edit_reaches_the_agent_as_a_correction(self):
        class C:
            def __init__(self, row):
                self.row = row

            def execute(self, *a):
                return self

            def fetchone(self):
                return self.row

            def close(self):
                pass
        with mock.patch.object(relay, "db", return_value=C(("meet at 8", 0))), \
                mock.patch.object(relay, "route") as rt:
            relay.on_edit("e1", relay.OWNER, "s", "m1", "meet at 9")
        text = rt.call_args[0][3]
        self.assertTrue(text.startswith('(EDITED: this replaces their earlier message "meet at 8"'))
        self.assertTrue(text.endswith("\nmeet at 9"))
        self.assertIn("Say only what changes", text)
        with mock.patch.object(relay, "db", return_value=C(("our reply", 1))), \
                mock.patch.object(relay, "route") as rt:
            relay.on_edit("e2", relay.OWNER, "s", "m2", "our fixed reply")
        rt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
