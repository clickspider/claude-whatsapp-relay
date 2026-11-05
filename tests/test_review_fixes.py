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
    def contact_cut(self, got, text="see you at 8"):
        relay.write(relay.st("inflight.json"), json.dumps({"m1": {
            "route": ["m1", "c@s.whatsapp.net", "c", "hi", ""], "sending": True, "after": 5, "text": text}}))
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

    def test_contact_answer_that_never_reached_the_send_runs_again(self):
        self.contact_cut(True, text=None).assert_called_once()  # deliver() never got to a contact send

    def test_held_or_silent_contact_answer_is_never_run_again(self):  # review B4: "" = nothing went to them
        self.contact_cut(False, text="").assert_not_called()

    def test_old_record_without_a_receipt_is_unknown_not_rerun(self):
        relay.write(relay.st("inflight.json"), json.dumps({"m1": {
            "route": ["m1", "c@s.whatsapp.net", "c", "hi", ""], "sending": True, "after": 5}}))
        relay._inflight.clear()
        with mock.patch.object(relay, "route") as r:
            relay.resume_jobs()
        r.assert_not_called()
        self.assertIn("uncertain", self.sent[-1])

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

    def test_a_note_to_the_thread_clears_the_still_needed_clock(self):
        relay.threads_db("9", title="z", state="blocked", reported=time.time() - 8 * 86400, asked=time.time())
        relay.thread_job("9", "still needed, go on")
        self.assertEqual(relay.threads_db()["9"]["asked"], 0)

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


class SecondReview(unittest.TestCase):
    """Bugs an independent review found in the first fix round (B1-B8)."""
    setUp = ReviewFixes.setUp
    tearDown = ReviewFixes.tearDown
    digest = ReviewFixes.digest

    def test_b1_a_page_without_a_result_does_not_stall_the_digest(self):
        relay.CFG["threads"]["main_chat_chars"] = 300
        relay.main_log("DANIEL to #4", "long note " + "z" * 400)  # not a result, bigger than one page
        relay.threads_db("4", title="t4", state="review", reported=time.time() - 60)
        relay.main_log("#4 t4 (review)", "the result")
        for _ in range(3):
            self.digest()
        self.assertNotIn("the result", relay.main_since("coordinator", lambda w: False, peek=True))  # it went out

    def test_b2_inbox_job_on_the_limit_never_takes_the_digest_slot(self):
        self.assertNotEqual(relay.job_id({"inbox": ["mom", "n.md"]}), relay.job_id({"digest": True}))
        self.assertEqual(relay.job_name({"inbox": ["mom", "n.md"]}), "a note to mom")

    def test_b3_receipt_is_the_part_that_goes_out_last(self):
        a = "#4 is done.\n@@DRAFT\nhi vendor\n@@END\nsend it?\n@@TO #4: do this:\n- step a"
        self.assertEqual(relay.last_sent(a).splitlines()[-1], "send it?")
        self.assertEqual(relay.last_sent("@@DRAFT\nonly a draft\n@@END"), "only a draft")

    def test_b5_digest_retry_backs_off_stops_and_never_after_the_send(self):
        relay.main_log("#4 t (review)", "done")
        relay._digest["fails"] = 0
        delays = []
        with mock.patch.object(relay.threading, "Timer", lambda d, f: delays.append(d) or mock.Mock()):
            for _ in range(6):
                with self.assertRaises(RuntimeError):
                    self.digest(side_effect=RuntimeError("timeout"))
            self.assertEqual(delays, [300, 600, 1200, 2400, 3600])  # then it waits for the next news
            relay._digest["fails"] = 0
            with mock.patch.object(relay, "main_mark", side_effect=RuntimeError("disk full")):
                with self.assertRaises(RuntimeError):
                    self.digest()  # it went out, then failed: no second digest
        self.assertEqual(len(delays), 5)

    def test_b6_one_bad_record_never_stops_the_rest(self):
        relay.threads_db("3", title="a", state="working")
        relay.write(relay.st("inflight.json"), json.dumps({
            "m2": {"route": ["m2", relay.OWNER, "o", "hi", ""]}, "thread-3-1": {"thread": ["3", "go"]}}))
        relay._inflight.clear()
        with mock.patch.object(relay, "route", side_effect=ValueError("bad")), \
                mock.patch.object(relay, "thread_job") as tj:
            relay.resume_jobs()
        tj.assert_called()  # the thread after the bad record still goes on
        self.assertTrue(any("could not go on" in t for t in self.sent))

    def test_b7_stop_in_the_owner_chat_keeps_inbox_jobs(self):
        self.assertEqual(relay.job_worker({"inbox": ["mom", "n.md"]}), "mom")

    def test_b8_a_cut_entry_says_so(self):
        relay.CFG["threads"]["main_chat_chars"] = 100
        relay.main_log("#4 t (review)", "x" * 300)
        news, _ = relay.main_since("coordinator", lambda w: False, peek=True, offset=True)
        self.assertIn(relay.PAGE_CUT, news)


class ThirdReview(unittest.TestCase):
    """Instinct's re-review of 7f37b23 (N1-N4 and its test gaps)."""
    setUp = ReviewFixes.setUp
    tearDown = ReviewFixes.tearDown
    digest = ReviewFixes.digest

    def shown(self):
        return relay.main_since("coordinator", lambda w: False, peek=True)

    def test_n1_an_entry_bigger_than_a_page_is_paged_inside_and_delivered_at_its_end(self):
        relay.CFG["threads"]["main_chat_chars"] = 300
        relay.threads_db("4", title="t4", state="review", reported=time.time() - 60)
        relay.main_log("#4 t4 (review)", "START " + "x" * 5000 + " THE-END")
        prompts = []
        for _ in range(40):
            relay._digest["due"] = True
            with mock.patch.object(relay, "owner_night", return_value=False), \
                    mock.patch.object(relay, "ask_owner_claude", side_effect=lambda p: prompts.append(p) or "ok"), \
                    mock.patch.object(relay, "coordinator_send"):
                relay.coordinator_digest()
            if len(prompts) == 1:
                self.assertIn(relay.PAGE_CUT, prompts[0])  # the cut is said
                self.assertFalse(relay.threads_db()["4"].get("delivered"))  # its end was not shown yet
                self.assertIn("THE-END", self.shown())  # the tail is still unread
            if not relay.unread_result():
                break
        self.assertIn("THE-END", prompts[-1])
        self.assertIn("#4 t4 (review)", prompts[-1])  # every piece carries its header
        self.assertTrue(relay.threads_db()["4"].get("delivered"))  # delivered once the end went out

    def pages(self, n=40):
        prompts = []
        for _ in range(n):
            relay._digest["due"] = True
            with mock.patch.object(relay, "owner_night", return_value=False), \
                    mock.patch.object(relay, "ask_owner_claude", side_effect=lambda p: prompts.append(p) or "ok"), \
                    mock.patch.object(relay, "coordinator_send"):
                relay.coordinator_digest()
            yield prompts[-1] if prompts else None
            if not relay.unread_result():
                break

    def test_n1_a_bold_line_in_the_first_piece_is_not_its_end(self):  # independent review, round 3
        relay.CFG["threads"]["main_chat_chars"] = 300
        relay.threads_db("4", title="t4", state="review", reported=time.time() - 60)
        relay.main_log("#4 t4 (review)", "Intro\n\n**Summary**\nabc\n" + "x" * 2000 + " THE-END")
        next(self.pages())
        self.assertFalse(relay.threads_db()["4"].get("delivered"))

    def test_n1_a_bold_line_is_never_taken_for_the_header(self):
        relay.CFG["threads"]["main_chat_chars"] = 300
        relay.threads_db("4", title="t4", state="review", reported=time.time() - 60)
        relay.main_log("#4 t4 (review)", "x" * 250 + "\n**Next steps**\n" + "y" * 2000 + " THE-END")
        last = list(self.pages())[-1]
        self.assertIn("#4 t4 (review)", last)
        self.assertTrue(relay.threads_db()["4"].get("delivered"))

    def test_n1_the_delivery_check_runs_once_per_result_not_per_piece(self):
        relay.CFG["threads"]["main_chat_chars"] = 300
        relay.main_log("#4 t4 (review)", "x" * 2000 + " THE-END")
        n = 0
        for _ in range(40):
            relay._digest["due"] = True
            with mock.patch.object(relay, "owner_night", return_value=False), \
                    mock.patch.object(relay, "ask_owner_claude", return_value="@@SKIP") as ac, \
                    mock.patch.object(relay, "coordinator_send"):
                relay.coordinator_digest()
            n += sum("DELIVERY CHECK" in c.args[0] for c in ac.call_args_list)
            if not relay.unread_result():
                break
        self.assertEqual(n, 1)

    def test_the_timeline_cut_keeps_offsets_exact(self):  # independent review: the cut was off by one
        relay.main_log("#1 t (review)", "a" * 450_000)
        relay.main_log("#2 t (review)", "MARK" + "b" * 200)
        relay.main_mark("coordinator", relay.read(relay.st("threads/main.md")).index("MARK"))  # inside #2
        relay.main_log("#3 t (review)", "c" * 60_000)  # > 500 KB: the front is cut
        news, _ = relay.main_since("coordinator", lambda w: False, peek=True, offset=True)
        self.assertIn("(continued)\nMARK", news)

    def test_n2_a_result_in_the_middle_of_a_big_backlog_is_not_stalled(self):
        relay.CFG["threads"]["main_chat_chars"] = 300
        relay.main_log("#4 t (review)", "first")
        for i in range(3):
            relay.main_log("DANIEL to #9", f"note {i} " + "n" * 250)
        relay.main_log("#5 t (review)", "middle result")
        for i in range(5):
            relay.main_log("DANIEL to #9", f"later {i} " + "n" * 250)  # more than one window after it
        self.digests.clear()
        self.digest()
        self.assertTrue(self.digests)  # the next page is armed

    def test_n3_a_reaction_or_first_chunk_is_not_a_receipt(self):
        import sqlite3
        store = relay.CFG["bridge"]["store"]
        os.makedirs(store, exist_ok=True)
        c = sqlite3.connect(os.path.join(store, "messages.db"))
        c.execute("CREATE TABLE messages (id TEXT, chat_jid TEXT, content TEXT, is_from_me BOOLEAN)")
        c.execute("INSERT INTO messages VALUES ('0', 'c', 'before', 1)")
        c.commit()
        after = relay.bridge_rowid()
        c.execute("INSERT INTO messages VALUES ('1', 'c', '👍', 1)")
        c.execute("INSERT INTO messages VALUES ('2', 'c', 'part one of the answer', 1)")
        c.commit(), c.close()
        self.assertFalse(relay.landed("c", after, "part one of the answer\npart two, the end"))

    def test_n4_a_digest_ending_in_a_draft_anchors_on_its_body(self):
        relay.main_log("#4 t (review)", "done")
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value="#4 is done, forward this:\n@@DRAFT\nhi vendor\n@@END"), \
                mock.patch.object(relay, "coordinator_send", side_effect=relay.StopRun()):
            with self.assertRaises(relay.StopRun):
                relay.coordinator_digest()
        self.assertEqual(relay._inflight["digest"]["text"], "#4 is done, forward this:")
        relay.inflight("digest")

    def test_a_digest_that_sent_nothing_is_no_uncertain_case(self):
        relay.write(relay.st("inflight.json"), json.dumps({"digest": {
            "digest": True, "sending": True, "upto": 0, "after": 1, "text": ""}}))
        relay._inflight.clear()
        relay.resume_jobs()
        self.assertFalse(any("uncertain" in t for t in self.sent))


class FourthReview(unittest.TestCase):
    """Instinct's re-review of c802dbf (M1-M3 and the 3.10 timestamp)."""
    setUp = ReviewFixes.setUp
    tearDown = ReviewFixes.tearDown

    def cut_digest(self, got=True, **rec):
        relay.write(relay.st("inflight.json"), json.dumps({"digest": dict({
            "digest": True, "sending": True, "upto": 0, "after": 1, "text": "#4 is done."}, **rec)}))
        relay._inflight.clear()
        with mock.patch.object(relay, "landed", return_value=got), mock.patch.object(relay, "ask") as ask, \
                mock.patch.object(relay, "send_owner_file") as sf, mock.patch.object(relay, "digest_soon"):
            relay.resume_jobs()
        return ask, sf

    def test_m1_the_question_and_file_after_the_body_are_tracked(self):
        relay.main_log("#4 t (review)", "done")
        relay._digest["due"] = True
        answer = "#4 is done.\n@@ASK: keep it? | yes | no\n@@FILE: report.pdf"
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value=answer), \
                mock.patch.object(relay, "ask", side_effect=relay.StopRun()):  # a crash after the body
            with self.assertRaises(relay.StopRun):
                relay.coordinator_digest()
        rec = relay._inflight["digest"]
        self.assertEqual(rec["tail"], [["ask", "keep it?", ["yes", "no"]], ["file", "report.pdf"]])
        self.assertEqual(rec["tail_at"], 0)  # the question was on its way
        relay.inflight("digest")

    def test_m1_a_body_that_landed_still_sends_the_rest_of_the_bundle(self):
        ask, sf = self.cut_digest(tail=[["ask", "keep it?", ["yes", "no"]], ["file", "report.pdf"]])
        ask.assert_called_once_with(relay.OWNER, "keep it?", ["yes", "no"], kind="ask")
        sf.assert_called_once_with("report.pdf")
        self.assertFalse(any("uncertain" in t for t in self.sent))

    def test_m1_the_item_on_its_way_is_named_never_sent_twice(self):
        ask, sf = self.cut_digest(tail=[["ask", "keep it?", ["yes", "no"]], ["file", "report.pdf"]], tail_at=0)
        ask.assert_not_called()  # it may have gone out: say so, no copy
        sf.assert_called_once_with("report.pdf")
        self.assertTrue(any("uncertain" in t and "keep it?" in t for t in self.sent))

    def test_m1_a_contact_answer_of_only_pictures_is_unknown_not_silent(self):
        relay._inflight["m1"] = {"route": ["m1", "c@s", "c", "hi", ""], "sending": True, "after": 5, "text": None}
        seen = []
        a = {"name": "mom", "home": "/tmp", "trust": "reply", "checkins": False, "files": False, "images": True,
             "mentions": False, "gifs": False, "ask_contact": False}
        with mock.patch.object(relay, "send_agent_image", lambda *x, **k: seen.append(relay._inflight["m1"]["text"])), \
                mock.patch.object(relay, "pick_react"), mock.patch.object(relay, "apply_fixes", lambda c, t: t):
            relay.deliver("c@s", a, "@@IMAGE: a.jpg", False, refs={"#1": ("m1", "c@s", "hi")})
        self.assertEqual(seen, ["?"])  # a crash mid-pictures: "delivery uncertain", never silently "nothing went"
        self.assertEqual(relay._inflight["m1"]["text"], "")  # all out: nothing to re-run

    def test_m2_an_old_digest_record_without_a_receipt_is_uncertain(self):
        relay.write(relay.st("inflight.json"), json.dumps({"digest": {
            "digest": True, "sending": True, "upto": 0, "after": 1}}))
        relay._inflight.clear()
        relay.resume_jobs()
        self.assertTrue(any("uncertain" in t for t in self.sent))

    def test_m3_the_sweep_rearms_news_the_retries_gave_up_on(self):
        relay.main_log("#4 t (review)", "done")
        relay._digest["timer"] = None
        with mock.patch.object(relay, "digest_soon") as ds:
            relay.digest_sweep()
            ds.assert_called_once()
            relay._inflight["digest"] = {"digest": True}  # one is queued or running: leave it
            relay.digest_sweep()
            ds.assert_called_once()
            relay.inflight("digest")
            relay._digest["timer"] = mock.Mock(is_alive=lambda: True)  # a timer waits (backoff, night)
            relay.digest_sweep()
            ds.assert_called_once()
        relay._digest["timer"] = None

    def test_m1_a_second_crash_during_resume_sends_nothing_twice(self):  # independent review, round 4
        tail = [["file", "a.pdf"], ["file", "b.pdf"], ["file", "c.pdf"]]
        relay._inflight["digest"] = {
            "digest": True, "sending": True, "upto": 0, "after": 1, "text": "x", "tail": tail, "tail_at": 0}
        with mock.patch.object(relay, "landed", return_value=True), \
                mock.patch.object(relay, "send_owner_file", side_effect=relay.StopRun()):  # dies during b.pdf
            with self.assertRaises(relay.StopRun):
                relay.resume_one("digest", relay._inflight["digest"], {}, [])
        self.assertEqual(relay._inflight["digest"]["tail_at"], 1)  # b.pdf was on its way, not a.pdf
        relay.inflight("digest")

    def test_m1_tail_at_proves_the_body_went_out(self):  # a long body cut over two bubbles may miss landed()
        ask, sf = self.cut_digest(got=False, tail=[["file", "a.pdf"], ["file", "b.pdf"]], tail_at=0)
        sf.assert_called_once_with("b.pdf")

    def test_m1_a_failed_question_never_repeats_the_body(self):  # the retry after an error re-sent the digest
        relay.main_log("#4 t (review)", "done")
        relay._digest["due"], relay._digest["fails"] = True, 0
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value="#4 is done.\n@@ASK: keep? | yes | no"), \
                mock.patch.object(relay, "ask", side_effect=OSError("bridge down")), \
                mock.patch.object(relay.threading, "Timer") as tm:
            relay.coordinator_digest()
        tm.assert_not_called()  # no retry: the body is out
        self.assertTrue(any("not sent" in t and "keep?" in t for t in self.sent))
        self.assertNotIn("#4 t", relay.main_since("coordinator", lambda w: False, peek=True))

    def test_m1_a_failed_note_after_the_body_never_repeats_the_digest(self):  # independent review, round 4
        relay.main_log("#4 t (review)", "done")
        relay._digest["due"], relay._digest["fails"] = True, 0
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", return_value="#4 is done.\n@@ASK: keep? | yes | no"), \
                mock.patch.object(relay, "ask", side_effect=OSError("bridge down")), \
                mock.patch.object(relay, "send_text", side_effect=[None, OSError("bridge down")]), \
                mock.patch.object(relay.threading, "Timer") as tm:
            relay.coordinator_digest()
        tm.assert_not_called()

    def test_m1_a_body_with_pictures_is_never_run_again(self):  # a re-run sent the pictures twice
        relay._inflight["m1"] = {"route": ["m1", "c@s", "c", "hi", ""], "sending": True, "after": 5, "text": None}
        seen = []
        a = {"name": "mom", "home": "/tmp", "trust": "reply", "checkins": False, "files": False, "images": True,
             "mentions": False, "gifs": False, "ask_contact": False, "trial_hours": 0}
        with mock.patch.object(relay, "send_agent_image", lambda *x, **k: seen.append(relay._inflight["m1"]["text"])), \
                mock.patch.object(relay, "send_text", lambda *x, **k: seen.append(relay._inflight["m1"]["text"])), \
                mock.patch.object(relay, "log_thread"), mock.patch.object(relay, "pick_quote"), \
                mock.patch.object(relay, "pick_react"), mock.patch.object(relay, "apply_fixes", lambda c, t: t):
            relay.deliver("c@s", a, "look\n@@IMAGE: a.jpg", False, refs={"#1": ("m1", "c@s", "hi")})
        self.assertEqual(seen, ["?", "?"])
        relay._inflight.pop("m1")

    def test_m3_the_sweep_leaves_a_digest_parked_on_the_limit(self):
        relay.main_log("#4 t (review)", "done")
        relay._digest["timer"] = None
        with mock.patch.object(relay, "parked", return_value={"digest": {"rec": {"digest": True}, "at": 1}}), \
                mock.patch.object(relay, "digest_soon") as ds:
            relay.digest_sweep()
        ds.assert_not_called()

    def test_a_second_digest_timer_keeps_the_receipt_of_the_one_going_out(self):
        relay._inflight["digest"] = {"digest": True, "sending": True, "text": "x", "upto": 0, "after": 1}
        with mock.patch.object(relay, "enqueue_job"):
            relay.digest_job()
        self.assertTrue(relay._inflight["digest"]["sending"])
        relay._inflight.pop("digest")

    def test_nanosecond_timestamps_parse_on_python_3_10_too(self):  # relay needs 3.11; reviewers shim 3.10
        self.assertEqual(relay.msg_epoch("2025-01-01 10:00:00.123456789+02:00"),
                         relay.msg_epoch("2025-01-01 10:00:00.123456+02:00"))
        self.assertIsNotNone(relay.msg_epoch("2025-01-01 10:00:00.123456789+02:00"))


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
