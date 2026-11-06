"""/stop keeps the order of kept jobs, and leaves other workers queues alone (reviewer, 6 Nov 2026)."""
import os, queue, sys, threading, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import relay, test_threads  # noqa: E402


class StopOrder(unittest.TestCase):
    setUp, tearDown = test_threads.ThreadTests.setUp, test_threads.ThreadTests.tearDown

    def test_kept_jobs_keep_their_order_and_other_queues_stay(self):
        n4, star = relay.stop_mark("#4")
        new = (n4 + 1, star)                      # the mark a job queued after this /stop carries
        K3 = ("K3", new)
        injected = []

        class Q(queue.Queue):                     # old code: K3 lands in the Empty -> re-put gap
            def get_nowait(self):
                try:
                    return super().get_nowait()
                except queue.Empty:
                    if not injected:
                        injected.append("gap"); self.put(K3)
                    raise
        q4, qo = Q(), queue.Queue()
        for j in [("OLD", (n4, star)), ("K1", new), ("K2", new)]:
            q4.put(j)
        owner_jobs = [("O1", relay.stop_mark("owner")), ("O2", (relay.stop_mark("owner")[0] - 1, star))]
        for j in owner_jobs:
            qo.put(j)
        real, th = relay.stop_mark, []

        def mark(k):                              # new code: a put from another thread while the filter runs
            if k == "#4" and q4.mutex.locked() and not injected:
                injected.append("thread")
                t = threading.Thread(target=q4.put, args=(K3,)); t.start(); th.append(t)
                time.sleep(0.2)                   # the thread is now blocked on q4.mutex
            return real(k)
        sent = []
        with mock.patch.object(relay, "workers", {"#4": q4, "owner": qo}), \
                mock.patch.object(relay, "stop_mark", mark), \
                mock.patch.object(relay, "send_text", lambda c, t, **k: sent.append(t)):
            relay.stop_runs({"#4"})
        for t in th:
            t.join(2)
        self.assertEqual([j[0] for j in q4.queue], ["K1", "K2", "K3"], injected)
        self.assertEqual(list(qo.queue), owner_jobs)          # a worker the /stop does not name: untouched
        self.assertIn("0 running, 1 waiting", sent[-1])


if __name__ == "__main__":
    unittest.main()
