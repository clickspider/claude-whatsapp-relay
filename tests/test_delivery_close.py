"""Finished work never vanishes in a @@SKIP; threads close when the result was delivered (5 Nov 2026, #134)."""
import os, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
import test_threads  # noqa: E402

H, D = 3600, 86400


class DeliveryCheck(unittest.TestCase):
    setUp = test_threads.ThreadTests.setUp

    def tearDown(self):
        relay.inflight("digest")  # in the relay the owner queue clears it (done_job)
        test_threads.ThreadTests.tearDown(self)

    def digest(self, *answers):
        relay._digest["due"] = True
        with mock.patch.object(relay, "owner_night", return_value=False), \
                mock.patch.object(relay, "ask_owner_claude", side_effect=list(answers)) as ask, \
                mock.patch.object(relay, "coordinator_send") as cs:
            relay.coordinator_digest()
        return ask, cs

    def test_finished_items_are_thread_results_and_job_ends(self):
        relay.main_log("#3 film (review)", "v4 ready")
        relay.main_log("🔔 job", "#3 render ended, exit code 0 (OK)")
        relay.main_log("🔔 contact", "she says hi")  # a contact note is not finished work
        news = relay.main_since("coordinator", lambda w: False, peek=True)
        self.assertEqual(relay.finished_items(news), ["#3 film (review)", "🔔 job"])

    def test_a_skipped_result_is_asked_again(self):
        relay.main_log("#3 film (review)", "v4 ready")
        ask, cs = self.digest("@@SKIP", "#3: v4 of the film is ready.")
        self.assertEqual(ask.call_count, 2)
        self.assertIn("DELIVERY CHECK", ask.call_args[0][0])
        self.assertIn("#3: v4", cs.call_args[0][0])

    def test_silence_is_a_logged_choice(self):
        relay.main_log("#3 film (review)", "v4 ready")
        ask, cs = self.digest("@@SKIP", "@@SKIP")
        self.assertIn("Chose silence", relay.read(relay.st("threads/main.md")))

    def test_pure_fyi_needs_no_second_ask(self):
        relay.main_log("🔔 contact", "she says hi")
        ask, _ = self.digest("@@SKIP")
        self.assertEqual(ask.call_count, 1)


class AutoClose(unittest.TestCase):
    setUp, tearDown = test_threads.ThreadTests.setUp, test_threads.ThreadTests.tearDown

    def run_at(self, t):
        with mock.patch.object(relay, "agent_note") as note:
            relay.thread_auto_close(now=t)
        return [c[0][1] for c in note.call_args_list]

    def test_blocked_thread_is_asked_once_then_closed_with_its_question(self):
        now = time.time()
        relay.threads_db("4", title="vendor call", state="blocked", last="Which date: 12 or 14?", reported=now - 8 * D)
        notes = self.run_at(now)
        self.assertIn("still needed?", notes[0])
        self.assertIn("Which date: 12 or 14?", notes[0])
        self.assertEqual(self.run_at(now + H), [])  # asked once only
        self.assertEqual(relay.threads_db()["4"]["state"], "blocked")
        notes = self.run_at(now + 49 * H)
        self.assertEqual(relay.threads_db()["4"]["state"], "closed")
        self.assertIn("Open question kept: Which date: 12 or 14?", notes[0])

    def test_new_activity_after_the_question_keeps_it_open(self):
        now = time.time()
        relay.threads_db("4", title="vendor call", state="blocked", last="Which date?", reported=now - 8 * D)
        self.run_at(now)
        relay.threads_db("4", reported=now + H)  # the thread reported again (still blocked)
        self.run_at(now + 49 * H)
        self.assertEqual(relay.threads_db()["4"]["state"], "blocked")

    def test_old_blocked_thread_without_report_time_closes_too(self):
        now = time.time()
        relay.threads_db("5", title="old", state="blocked", last="Go on?")
        with mock.patch.object(relay.time, "time", return_value=now + 8 * D):
            self.run_at(now + 8 * D)
        self.run_at(now + 8 * D + 49 * H)
        self.assertEqual(relay.threads_db()["5"]["state"], "closed")

    def test_review_closes_24_h_after_delivery_not_before(self):
        now = time.time()
        relay.threads_db("6", title="film", state="review", last="v4", reported=now - 30 * H)
        self.run_at(now)
        self.assertEqual(relay.threads_db()["6"]["state"], "review")  # never delivered: stays open
        relay.mark_delivered(now)
        self.run_at(time.time() + 23 * H)
        self.assertEqual(relay.threads_db()["6"]["state"], "review")
        notes = self.run_at(time.time() + 25 * H)
        self.assertEqual(relay.threads_db()["6"]["state"], "closed")
        self.assertIn("FYI only", notes[0])


if __name__ == "__main__":
    unittest.main()
