"""Instinct's reliability review of ac4bb56 (5 Nov 2026): one test per finding, each fails on ac4bb56/f19b343."""
import json, os, sys, time, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay  # noqa: E402
REAL_ENQUEUE = relay.enqueue  # setUp patches it; the round 9 test needs a real worker thread
import test_threads  # noqa: E402


class ReviewFixes(unittest.TestCase):
    setUp = test_threads.ThreadTests.setUp

    def tearDown(self):
        relay._inflight.clear(), relay.RESUMED.clear(), relay._stopped_votes.clear()
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


class FifthReview(unittest.TestCase):
    """Instinct's re-review of b849494 (O1, O2)."""
    setUp = ReviewFixes.setUp
    tearDown = ReviewFixes.tearDown

    def test_o1_the_sweep_runs_on_real_time_not_loop_ticks(self):
        relay._last_run.pop("x", None)
        self.assertFalse(relay.each("x", 1800, now=100.0))  # the first call starts the clock
        self.assertFalse(relay.each("x", 1800, now=1899.0))
        self.assertTrue(relay.each("x", 1800, now=1900.0))  # 30 min later, whatever poll_seconds is
        self.assertFalse(relay.each("x", 1800, now=1901.0))
        import inspect
        self.assertIn('each("digest_sweep", 1800)', inspect.getsource(relay.main_loop))

    def test_o1_the_main_loop_runs_the_sweep(self):  # the loop's own name "every" once hid a helper of that name
        class Out(Exception):
            pass
        relay._last_run["digest_sweep"] = (-1e9, -1e9)
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = (0,)
        with mock.patch.object(relay, "db", return_value=conn), mock.patch.object(relay, "resume_jobs"), \
                mock.patch.object(relay, "new_rows", return_value=([], 0)), \
                mock.patch.object(relay, "digest_sweep") as sweep, \
                mock.patch.object(relay.time, "sleep", side_effect=Out()):
            with self.assertRaises(Out):
                relay.main_loop()
        sweep.assert_called_once()

    def test_o1_auto_close_also_runs_on_real_time(self):
        relay._last_run["thread_auto_close"] = (-1e9, -1e9)
        relay._last_run["digest_sweep"] = (-1e9, -1e9)
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = (0,)
        with mock.patch.object(relay, "db", return_value=conn), mock.patch.object(relay, "resume_jobs"), \
                mock.patch.object(relay, "new_rows", return_value=([], 0)), mock.patch.object(relay, "digest_sweep"), \
                mock.patch.object(relay, "thread_auto_close") as close, \
                mock.patch.object(relay.time, "sleep", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                relay.main_loop()
        close.assert_called_once()  # at tick 1, not tick 1800

    def test_o1_the_sweep_waits_for_a_pending_retry(self):
        relay.main_log("#4 t (review)", "done")
        relay._digest["timer"] = None
        relay._digest["retry"] = mock.Mock(is_alive=lambda: True)
        with mock.patch.object(relay, "digest_soon") as ds:
            relay.digest_sweep()
        ds.assert_not_called()
        relay._digest["retry"] = None

    def test_o1_a_clock_set_back_does_not_stop_the_sweeps(self):
        relay._last_run["y"] = (10_000.0, 0.0)
        self.assertFalse(relay.each("y", 1800, now=2_000.0, mono=10.0))  # clock set back
        self.assertTrue(relay.each("y", 1800, now=3_800.0, mono=1_810.0))

    def test_o2_a_note_the_bridge_refuses_never_drops_the_rest(self):
        rec = {"route": ["o1", relay.OWNER, relay.OWNER, "cv?", ""], "sending": True, "tail_at": 0,
               "tail": [["ask", "tailor it?", ["yes", "no"]], ["file", "cv.pdf"]]}
        relay._inflight["o1"] = rec
        with mock.patch.object(relay, "send_text", side_effect=OSError("bridge down")), \
                mock.patch.object(relay, "send_owner_file") as sf:
            relay.resume_tail("o1", rec, True)
        sf.assert_called_once_with("cv.pdf")
        relay._inflight.pop("o1")

    def owner_cut(self):
        relay._inflight["o1"] = {"route": ["o1", relay.OWNER, relay.OWNER, "send me the cv", ""]}
        answer = "here it is\n@@ASK: tailor it? | yes | no\n@@FILE: cv.pdf"
        with mock.patch.object(relay, "ask", side_effect=relay.StopRun()):  # a crash after the body
            with self.assertRaises(relay.StopRun):
                relay.coordinator_send(answer, "send me the cv", refs={"#1": ("o1", relay.OWNER, "send me the cv")})
        rec = relay._inflight["o1"]
        self.assertEqual(rec["tail"], [["ask", "tailor it?", ["yes", "no"]], ["file", "cv.pdf"]])
        return rec

    def test_o2_an_owner_answer_tracks_its_questions_and_files(self):
        self.assertEqual(self.owner_cut()["tail_at"], 0)
        relay._inflight.pop("o1")

    def test_o2_a_cut_owner_answer_sends_the_files_that_never_started(self):
        rec = self.owner_cut()
        relay.write(relay.st("inflight.json"), json.dumps({"o1": rec}))
        relay._inflight.clear()
        with mock.patch.object(relay, "ask") as ask, mock.patch.object(relay, "send_owner_file") as sf, \
                mock.patch.object(relay, "route") as r:
            relay.resume_jobs()
        r.assert_not_called()  # never a blind re-run
        ask.assert_not_called()  # on its way at the crash: named, no copy
        sf.assert_called_once_with("cv.pdf")
        self.assertTrue(any("tailor it?" in t and "uncertain" in t for t in self.sent))
        self.assertNotIn("o1", relay._inflight)


    def test_o2_files_of_an_answer_whose_body_may_not_have_gone_are_named_not_sent(self):
        relay.write(relay.st("inflight.json"), json.dumps({"o1": {
            "route": ["o1", relay.OWNER, relay.OWNER, "cv?", ""], "sending": True, "after": 1, "text": None,
            "tail": [["file", "cv.pdf"]]}}))  # no tail_at: the crash came before or during the body
        relay._inflight.clear()
        with mock.patch.object(relay, "send_owner_file") as sf, mock.patch.object(relay, "route"):
            relay.resume_jobs()
        sf.assert_not_called()  # never a file without its answer
        self.assertTrue(any("cv.pdf" in t for t in self.sent))

    def test_o2_the_uncertain_note_comes_before_the_rest(self):
        relay.write(relay.st("inflight.json"), json.dumps({"o1": {
            "route": ["o1", relay.OWNER, relay.OWNER, "cv?", ""], "sending": True, "after": 1, "text": None,
            "tail": [["ask", "tailor it?", ["yes", "no"]], ["file", "cv.pdf"]], "tail_at": 0}}))
        relay._inflight.clear()
        order = []
        with mock.patch.object(relay, "send_owner_file", lambda f: order.append(f)), mock.patch.object(relay, "route"), \
                mock.patch.object(relay, "send_text", lambda c, t, **k: order.append(t)):
            relay.resume_jobs()
        self.assertIn("tailor it?", order[0])
        self.assertEqual(order[1], "cv.pdf")


class SixthReview(unittest.TestCase):
    """Instinct's re-review of 233ad89 (votes, drafts, owner proof, clock)."""
    setUp = ReviewFixes.setUp
    tearDown = ReviewFixes.tearDown

    def test_v1_a_vote_cut_by_a_restart_is_answered_after_it(self):
        relay.polls({"P1": {"chat": relay.OWNER, "question": "tailor it?", "kind": "ask"}})
        disk = []
        with mock.patch.object(relay, "owner_add", lambda *a: disk.append(relay.read(relay.st("inflight.json")))):
            relay.on_vote("V1", relay.OWNER, relay.OWNER, "P1", "yes")
        self.assertTrue(relay.polls()["P1"]["done"])
        self.assertIn("V1", json.loads(disk[0]))  # on disk while the answer waits in memory
        relay.write(relay.st("inflight.json"), disk[0])  # the process dies here: a restart reads this
        relay._inflight.clear()
        with mock.patch.object(relay, "owner_add") as oa:
            relay.resume_jobs()
        oa.assert_called_once_with("V1", 'ANSWER to "tailor it?": yes', None)

    def test_v1_a_real_kill_mid_vote_loses_nothing(self):  # a real process death: no finally block runs
        import subprocess
        relay.polls({"P3": {"chat": relay.OWNER, "question": "tailor it?", "kind": "ask"}})
        child = (
            "import os, sys; sys.path.insert(0, sys.argv[1]); import relay\n"
            "relay.CFG = {'paths': {'state_dir': sys.argv[2]}, 'ui': {'polls': True}}\n"
            "relay.OWNER = sys.argv[3]\n"
            "relay.owner_add = lambda *a: os._exit(9)  # killed while the answer waits in memory\n"
            "relay.on_vote('V3', relay.OWNER, relay.OWNER, 'P3', 'yes')\n")
        r = subprocess.run([sys.executable, "-c", child, os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            relay.CFG["paths"]["state_dir"], relay.OWNER], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertTrue(relay.polls()["P3"]["done"])  # re-tapping would be ignored now
        relay._inflight.clear()
        with mock.patch.object(relay, "owner_add") as oa:
            relay.resume_jobs()
        oa.assert_called_once_with("V3", 'ANSWER to "tailor it?": yes', None)

    def test_v1_a_vote_that_is_handled_at_once_leaves_no_record(self):
        relay.polls({"P2": {"chat": relay.OWNER, "question": "q", "kind": "ask"}})
        with mock.patch.object(relay, "owner_add"):  # owner_add claims the id in real life; here it does not
            relay.on_vote("V2", relay.OWNER, relay.OWNER, "P2", "yes")
        self.assertNotIn("V2", relay._inflight)

    def test_v1_a_vote_parked_on_the_limit_runs_as_a_vote(self):  # independent review: not as "(vote: yes)"
        rec = {"route": ["V4", relay.OWNER, relay.OWNER, "(vote: yes)", None],
               "vote": ["V4", relay.OWNER, relay.OWNER, "P4", "yes"]}
        relay.parked(lambda j: j.update({"route:V4": {"rec": rec, "at": 0, "since": 0, "tries": 0}}))
        with mock.patch.object(relay, "on_vote") as ov, mock.patch.object(relay, "route") as r:
            relay.resume_parked(now=1)
        r.assert_not_called()
        ov.assert_called_once_with("V4", relay.OWNER, relay.OWNER, "P4", "yes", resumed=True)

    def test_v1_a_vote_job_is_marked_sending_before_it_sends(self):  # a restart then says "uncertain", no copy
        rec = {"kind": "draft", "agent": "mom", "draft": "d", "chat": relay.OWNER, "question": "send?"}
        a = {"name": "mom"}
        relay._inflight["V5"] = {"route": ["V5", relay.OWNER, relay.OWNER, "(vote: Send)", None]}
        jobs = []
        with mock.patch.object(relay, "flag", return_value="x"), mock.patch.object(relay, "digest", return_value="d"), \
                mock.patch.object(relay, "enqueue_job", lambda k, fn, d: jobs.append(fn)), \
                mock.patch.object(relay, "owner_command", lambda *x: self.assertTrue(relay._inflight["V5"]["sending"])):
            relay.vote_job("V5", relay.OWNER, relay.OWNER, rec, "Send", "mom", "c@s", a)
            jobs[0]()
        relay._inflight.pop("V5")

    def test_v1_stop_finds_the_worker_of_an_owner_chat_vote(self):
        relay.polls({"P6": {"chat": relay.OWNER, "question": "q", "kind": "draft", "agent": "mom"}})
        self.assertEqual(relay.job_worker({"route": ["V6", relay.OWNER, relay.OWNER, "x", None],
                                           "vote": ["V6", relay.OWNER, relay.OWNER, "P6", "Send"]}), "mom")

    def test_v1_no_bridge_store_never_stops_a_vote_job(self):  # mark_sending now runs in vote jobs
        old = relay.CFG.pop("bridge", None)
        try:
            self.assertIsNone(relay.bridge_rowid())
        finally:
            if old is not None:
                relay.CFG["bridge"] = old

    def test_d1_the_bundle_is_saved_before_the_draft_goes_out(self):
        relay._inflight["o1"] = {"route": ["o1", relay.OWNER, relay.OWNER, "cv?", ""]}
        with mock.patch.object(relay, "send_text", side_effect=relay.StopRun()):  # a crash during the draft
            with self.assertRaises(relay.StopRun):
                relay.coordinator_send("@@DRAFT\nhi vendor\n@@END\nnotes\n@@FILE: cv.pdf", "cv?",
                                       refs={"#1": ("o1", relay.OWNER, "cv?")})
        self.assertEqual(relay._inflight["o1"]["tail"], [["file", "cv.pdf"]])
        relay._inflight.pop("o1")

    def test_p1_tail_at_proves_an_owner_body_went_out(self):
        relay.write(relay.st("inflight.json"), json.dumps({"o1": {
            "route": ["o1", relay.OWNER, relay.OWNER, "cv?", ""], "sending": True, "after": 1, "text": None,
            "tail": [["file", "a.pdf"], ["file", "b.pdf"]], "tail_at": 0}}))
        relay._inflight.clear()
        with mock.patch.object(relay, "send_owner_file"), mock.patch.object(relay, "route"):
            relay.resume_jobs()
        self.assertFalse(any('answer to "cv?"' in t for t in self.sent))  # only the file on its way is named
        self.assertTrue(any("a.pdf" in t for t in self.sent))

    def test_c1_repeated_clock_steps_back_never_postpone_a_sweep(self):
        relay._last_run.pop("z", None)
        self.assertFalse(relay.each("z", 1800, now=10_000.0, mono=0.0))
        for i in range(1, 6):  # the clock steps back every 5 min
            self.assertFalse(relay.each("z", 1800, now=10_000.0 - i * 100, mono=i * 300.0))
        self.assertTrue(relay.each("z", 1800, now=9_000.0, mono=1_800.0))  # 30 real minutes passed


class SeventhReview(unittest.TestCase):
    """Instinct's re-review of e97ba05: plain /stop must stop a vote from the owner chat."""
    setUp = ReviewFixes.setUp
    tearDown = ReviewFixes.tearDown

    def test_s1_stop_stops_a_send_vote_queued_behind_a_busy_agent(self):
        sent = []  # setUp's enqueue keeps jobs in self.jobs: a queue no /stop of the owner chat drains
        a = {"name": "busy7", "jid": "c7@s"}
        relay.polls({"P7": {"chat": relay.OWNER, "question": "send?", "kind": "draft", "agent": "busy7", "draft": "d"}})
        with mock.patch.object(relay, "by_name", return_value=("c7@s", a)), \
                mock.patch.object(relay, "flag", return_value="x"), mock.patch.object(relay, "digest", return_value="d"), \
                mock.patch.object(relay, "mark_sending"), \
                mock.patch.object(relay, "owner_command", lambda *x: sent.append(x)):
            relay.enqueue("busy7", lambda: None)  # the agent is busy with this one
            relay.on_vote("V7", relay.OWNER, relay.OWNER, "P7", "Send")  # Send tapped: queued behind it
            relay.stop_runs({"owner"})  # plain /stop in the owner chat
            for _, job in self.jobs:  # the agent's worker goes on
                job()
        self.assertEqual(sent, [])  # the draft never goes out after the stop
        self.assertNotIn("V7", relay._inflight)
        self.assertTrue(any("1 waiting" in t for t in self.sent))

    def test_s1_stop_during_the_agent_run_sends_nothing(self):
        a = {"name": "busy9", "jid": "c9@s"}
        relay.polls({"P9": {"chat": relay.OWNER, "question": "which day?", "kind": "ask", "agent": "busy9"}})

        def run(*x):  # the owner types /stop while the agent writes its answer
            relay.stop_runs({"owner"})
            return "monday then"
        with mock.patch.object(relay, "by_name", return_value=("c9@s", a)), mock.patch.object(relay, "run_agent", run), \
                mock.patch.object(relay, "mark_sending"), mock.patch.object(relay, "deliver") as d:
            relay.on_vote("V9", relay.OWNER, relay.OWNER, "P9", "Mon")
            for _, job in self.jobs:
                job()
        d.assert_not_called()

    def test_s1_stop_drops_a_vote_parked_on_the_limit(self):  # independent review: it sent after a restart
        rec = {"route": ["V10", relay.OWNER, relay.OWNER, "(vote: Mon)", None],
               "vote": ["V10", relay.OWNER, relay.OWNER, "P10", "Mon"], "worker": "busy10"}
        relay.parked(lambda j: j.update({relay.job_id(rec): {"rec": rec, "at": 0, "since": 0, "tries": 0}}))
        relay.stop_runs({"owner"})
        self.assertEqual(relay.parked(), {})
        with mock.patch.object(relay, "on_vote") as ov:
            relay.resume_parked(now=1)
        ov.assert_not_called()

    def test_s1_stop_kills_the_agent_run_of_a_vote_and_counts_right(self):
        relay._inflight["V11"] = {"route": ["V11", relay.OWNER, relay.OWNER, "(vote: Mon)", None],
                                  "vote": ["V11", relay.OWNER, relay.OWNER, "P11", "Mon"], "worker": "busy11"}
        p = mock.Mock(relay_key="busy11", vote="V11")
        with mock.patch.object(relay, "_procs", {p}):
            relay.stop_runs({"owner"})
        p.terminate.assert_called_once()
        self.assertTrue(any("1 running" in t for t in self.sent))

    def test_s1_a_vote_run_that_ends_during_the_stop_is_no_error(self):
        relay._inflight["V12"] = {"route": ["V12", relay.OWNER, relay.OWNER, "(vote: Mon)", None],
                                  "vote": ["V12", relay.OWNER, relay.OWNER, "P12", "Mon"], "worker": "busy12"}
        p = mock.Mock(relay_key="busy12", vote="V12")
        p.terminate.side_effect = lambda: relay._inflight.pop("V12", None)  # its job ends at once
        with mock.patch.object(relay, "_procs", {p}):
            relay.stop_runs({"owner"})
        self.assertTrue(any("Stopped" in t for t in self.sent))

    def test_s1_a_run_that_starts_after_the_stop_is_ended(self):
        relay._stopped_votes.add("V13")
        relay._wk.vote = "V13"
        fake = mock.Mock()
        fake.communicate.return_value = ("", "")
        fake.returncode = 0
        try:
            with mock.patch.object(relay.subprocess, "Popen", return_value=fake), \
                    mock.patch.object(relay, "hold_if_stopping"):  # an earlier test may leave STOPPING set
                try:
                    relay.run_claude(["claude"], "/tmp", "hi")
                except Exception:
                    pass
        finally:
            relay._wk.vote = None
        fake.terminate.assert_called()

    def test_s1_stop_waits_for_a_draft_send_on_its_way(self):  # never "Stopped" while it still sends
        import threading
        order, inside, go = [], threading.Event(), threading.Event()
        a = {"name": "busy14", "jid": "c14@s"}
        relay.polls({"P14": {"chat": relay.OWNER, "question": "send?", "kind": "draft", "agent": "busy14", "draft": "d"}})

        def command(*x):
            inside.set(), go.wait(5), order.append("sent")
        with mock.patch.object(relay, "by_name", return_value=("c14@s", a)), \
                mock.patch.object(relay, "flag", return_value="x"), mock.patch.object(relay, "digest", return_value="d"), \
                mock.patch.object(relay, "mark_sending"), mock.patch.object(relay, "owner_command", command), \
                mock.patch.object(relay, "send_text", lambda c, t, **k: order.append("waiting" if "on its way" in t else "stopped")):
            relay.on_vote("V14", relay.OWNER, relay.OWNER, "P14", "Send")
            job = threading.Thread(target=self.jobs[-1][1])
            job.start()
            inside.wait(5)
            stop = threading.Thread(target=relay.stop_runs, args=({"owner"},))
            stop.start()
            stop.join(0.3)
            self.assertTrue(stop.is_alive())  # it waits for the send on its way
            go.set(), job.join(5), stop.join(5)
        self.assertEqual(order, ["waiting", "sent", "stopped"])  # round 8: it says why it waits

    def test_s1_a_vote_after_the_stop_still_counts(self):
        sent = []
        a = {"name": "busy8", "jid": "c8@s"}
        relay.polls({"P8": {"chat": relay.OWNER, "question": "send?", "kind": "draft", "agent": "busy8", "draft": "d"}})
        with mock.patch.object(relay, "by_name", return_value=("c8@s", a)), \
                mock.patch.object(relay, "flag", return_value="x"), mock.patch.object(relay, "digest", return_value="d"), \
                mock.patch.object(relay, "mark_sending"), \
                mock.patch.object(relay, "owner_command", lambda *x: sent.append(x)):
            relay.stop_runs({"owner"})
            relay.on_vote("V8", relay.OWNER, relay.OWNER, "P8", "Send")
            for _, job in self.jobs:
                job()
        self.assertEqual(sent, [("busy8", "ok")])


class EighthReview(unittest.TestCase):
    """Instinct's re-review of 457505e: two narrow /stop races, and a clearer acknowledgement."""
    setUp = ReviewFixes.setUp
    tearDown = ReviewFixes.tearDown

    def vote(self, v, worker, choice="Mon"):
        return {"route": [v, relay.OWNER, relay.OWNER, f"(vote: {choice})", None],
                "vote": [v, relay.OWNER, relay.OWNER, "P" + v, choice], "worker": worker}

    def stop_at_register(self, vote=None, key=None, stops=None):
        """/stop lands after the run's process started, right before run_claude registers it."""
        fake = mock.Mock()
        fake.communicate.return_value = ("", "")
        fake.returncode = 0

        class Racy(set):
            def add(s, p):
                relay.stop_runs({"owner"})
                super().add(p)
        relay._wk.vote, relay._wk.key, relay._wk.stops = vote, key, stops
        try:
            with mock.patch.object(relay.subprocess, "Popen", return_value=fake), \
                    mock.patch.object(relay, "_procs", Racy()), mock.patch.object(relay, "hold_if_stopping"):
                with self.assertRaises(relay.StopRun):
                    relay.run_claude(["claude"], "/tmp", "hi")
        finally:
            relay._wk.vote = relay._wk.key = relay._wk.stops = None
        fake.terminate.assert_called()

    def test_r1_a_stop_before_the_vote_run_registers_still_ends_it(self):
        relay._inflight["V20"] = self.vote("V20", "busy20")
        self.stop_at_register(vote="V20", key="busy20")

    def test_r1_a_stop_before_an_owner_run_registers_still_ends_it(self):  # the same window without a vote
        self.stop_at_register(key="owner", stops=relay.stop_mark("owner"))

    def test_r1_a_stop_of_another_worker_leaves_the_run_alone(self):
        relay._wk.key, relay._wk.stops = "#7", relay.stop_mark("#7")
        fake = mock.Mock()
        fake.communicate.return_value = ("ok", "")
        fake.returncode = 0
        try:
            relay.stop_runs({"owner"})
            with mock.patch.object(relay.subprocess, "Popen", return_value=fake), \
                    mock.patch.object(relay, "hold_if_stopping"), mock.patch.object(relay, "limit_hit", return_value=None):
                relay.run_claude(["claude"], "/tmp", "hi")
        finally:
            relay._wk.key = relay._wk.stops = None
        fake.terminate.assert_not_called()

    def test_r2_a_crash_inside_the_stop_never_replays_the_vote(self):  # the disk changes before the memory mark
        relay.inflight("V21", self.vote("V21", "busy21"))
        rec = self.vote("V22", "busy22")
        relay.parked(lambda j: j.update({relay.job_id(rec): {"rec": rec, "at": 0, "since": 0, "tries": 0}}))

        class Crash(set):
            def update(s, *a):
                raise SystemExit("killed")
        with mock.patch.object(relay, "_stopped_votes", Crash()):
            with self.assertRaises(SystemExit):
                relay.stop_runs({"owner"})
        with open(relay.st("inflight.json")) as f:
            self.assertNotIn("V21", json.load(f))
        self.assertEqual(relay.parked(), {})

    def test_p_the_stop_line_says_a_stopped_draft_stays_held(self):
        relay._inflight["V23"] = self.vote("V23", "busy23", "Send")
        relay.stop_runs({"owner"})
        self.assertTrue(any("/busy23 ok" in t for t in self.sent))

    def test_p_a_send_already_out_is_not_called_held(self):
        relay._inflight["V24"] = dict(self.vote("V24", "busy24", "Send"), sending=True)
        relay.stop_runs({"owner"})
        self.assertFalse(any("busy24" in t for t in self.sent))


class NinthReview(unittest.TestCase):
    """Instinct's re-review of 9c70f41: ordinary runs, the stop count and the disk cleanup."""
    setUp = ReviewFixes.setUp

    def tearDown(self):
        relay._wk.stops = None
        relay.owner_buf.clear()
        relay.owner_sched[0] = False
        ReviewFixes.tearDown(self)

    def test_r9_a_stop_between_take_and_start_still_ends_an_ordinary_job(self):  # with a real worker thread
        import queue, threading
        taken, go, done, out = threading.Event(), threading.Event(), threading.Event(), []
        real = queue.Queue

        class Q(real):
            def get(s, *a, **k):
                item = super().get(*a, **k)
                taken.set(), go.wait(5)  # the /stop lands between q.get() and the job's start
                return item
        fake = mock.Mock()
        fake.communicate.return_value = ("ok", "")
        fake.returncode = 0

        def job():
            try:
                relay.run_claude(["claude"], "/tmp", "hi")
                out.append("ran")
            finally:
                done.set()
        relay.workers.pop("w9", None)
        with mock.patch.object(relay.subprocess, "Popen", return_value=fake), \
                mock.patch.object(relay, "hold_if_stopping"), mock.patch.object(relay, "limit_hit", return_value=None):
            with mock.patch.object(relay.queue, "Queue", Q):
                REAL_ENQUEUE("w9", job)
            self.assertTrue(taken.wait(5))
            relay.stop_runs({"w9"})
            go.set()
            self.assertTrue(done.wait(5))
            self.assertEqual(out, [])  # the stopped job never ran on
            done.clear()
            REAL_ENQUEUE("w9", job)  # new work after the /stop still runs
            self.assertTrue(done.wait(5))
        self.assertEqual(out, ["ran"])

    def test_r9_the_owner_chat_answers_again_after_a_stop_drops_its_queued_batch(self):
        relay.owner_sched[0] = True  # a batch waits in the owner queue
        relay.stop_runs({"owner"})
        relay.owner_add("m9", "hello?", "text")
        self.assertIn("owner", [k for k, _ in self.jobs])
        relay._claimed.discard("m9")

    def test_r9_a_batch_taken_before_the_stop_answers_what_came_after_it(self):
        relay.stop_runs({"owner"})
        relay._wk.stops = (-1, -1)  # this drain was taken from the queue before the /stop
        relay.owner_buf.append(("m10", "after the stop", "text", 0))
        seen = []
        with mock.patch.object(relay, "owner_batch", lambda items: seen.append(relay._wk.stops)), \
                mock.patch.object(relay, "done_job"):
            relay.owner_drain()
        self.assertEqual(seen, [relay.stop_mark("owner")])  # a new baseline: its run is not ended

    def test_r9_a_kill_that_fails_still_clears_the_disk(self):
        relay.inflight("o9", {"route": ["o9", relay.OWNER, relay.OWNER, "hi", ""]})
        p = mock.Mock(relay_key="owner", vote=None)
        p.terminate.side_effect = OSError("no such process")
        with mock.patch.object(relay, "_procs", {p}):
            try:
                relay.stop_runs({"owner"})
            except OSError:
                pass
        with open(relay.st("inflight.json")) as f:
            self.assertNotIn("o9", json.load(f))
        self.assertTrue(any("Stopped" in t for t in self.sent))


class TenthReview(unittest.TestCase):
    """Instinct's re-review of 232b288: a durable stopped-vote marker, and the kill scan spares newer work."""
    setUp = ReviewFixes.setUp
    tearDown = NinthReview.tearDown

    def vote(self, v, worker, choice="Send"):
        return EighthReview.vote(self, v, worker, choice)

    def test_t1_a_crash_inside_the_stop_never_replays_a_vote(self):  # a real kill: no finally block runs
        a = {"name": "busy30", "jid": "c30@s"}
        relay.polls({"PV30": {"chat": relay.OWNER, "question": "send?", "kind": "draft", "agent": "busy30", "draft": "d", "done": True},
                     "PV31": {"chat": relay.OWNER, "question": "send?", "kind": "draft", "agent": "busy30", "draft": "d", "done": True}})
        parked = self.vote("V30", "busy30")
        relay.parked(lambda j: j.update({relay.job_id(parked): {"rec": parked, "at": 0, "since": 0, "tries": 0}}))
        relay.inflight("V31", self.vote("V31", "busy30"))
        real = relay.inflight

        def crash(key, rec=None):
            if rec is None:
                raise SystemExit("killed")  # the process dies at its first removal
            return real(key, rec)
        with mock.patch.object(relay, "inflight", crash):
            with self.assertRaises(SystemExit):
                relay.stop_runs({"owner"})
        relay._stopped_votes.clear(), relay._inflight.clear()  # the restart: memory is gone, the disk stays
        with mock.patch.object(relay, "by_name", return_value=("c30@s", a)), \
                mock.patch.object(relay, "vote_job") as vj, mock.patch.object(relay, "route"):
            relay.resume_jobs()
            relay.resume_parked(now=time.time() + 10)
        vj.assert_not_called()

    def test_t2_the_kill_scan_spares_work_that_started_after_the_stop(self):
        old = mock.Mock(relay_key="owner", vote=None, stops=(-1, -1))
        new = mock.Mock(relay_key="owner", vote=None)
        real = relay.parked

        def during(change=None):  # inside the stop, after its count went up: an idle worker starts new work
            new.stops = relay.stop_mark("owner")
            procs.add(new)
            return real(change)
        procs = {old}
        with mock.patch.object(relay, "_procs", procs), mock.patch.object(relay, "parked", during):
            relay.stop_runs({"owner"})
        old.terminate.assert_called_once()
        new.terminate.assert_not_called()


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
