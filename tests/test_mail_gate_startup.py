"""Mail gate start-up and the send/retract race (reviewer short round on 3d4bac9, 7 Nov 2026).
Every outside effect is stubbed: no bridge, no Gmail."""
import email, email.policy, json, os, sys, time, unittest, urllib.error
from email.message import EmailMessage
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import relay, test_threads, gmail_send as gs  # noqa: E402


class Stop(Exception):
    pass


class StartUp(unittest.TestCase):
    def setUp(self):
        test_threads.ThreadTests.setUp(self)
        self.addCleanup(test_threads.ThreadTests.tearDown, self)
        self.addCleanup(relay._inflight.clear)

    def test_bridge_down_at_start_skips_resume_and_the_next_message_wipes_inflight(self):
        old_rec = {"route": ["m-old", relay.OWNER, relay.OWNER, "please book the dentist", None], "worker": "owner"}
        relay.write(relay.st("inflight.json"), json.dumps({"m-old": old_rec}))
        gs.save(relay.st(relay.MAIL_GATE), {"kind": "send", "draft": "r1234567", "time": time.time(), "status": "asking"})
        resumed = []
        def down(*a, **k):
            raise urllib.error.URLError("bridge not up yet")
        with mock.patch.object(relay, "send_text", down), \
                mock.patch.object(relay, "resume_jobs", lambda: resumed.append(1) or relay._inflight.update(relay.load_state("inflight.json"))), \
                mock.patch.object(relay, "heartbeat", lambda: None), \
                mock.patch.object(relay, "watchdog_back", lambda: None), \
                mock.patch.object(relay, "db", lambda n: None), \
                mock.patch.object(relay, "new_rows", mock.Mock(side_effect=Stop)):
            relay.write(relay.st("last-rowid"), "0")
            with self.assertRaises(Stop):
                relay.main_loop()
        relay.inflight("m-new", {"route": ["m-new", relay.OWNER, relay.OWNER, "hi", None]})  # the next message
        on_disk = json.loads(relay.read(relay.st("inflight.json")))
        self.assertTrue(resumed, "a failed sweep line skipped resume_jobs")
        self.assertIn("m-old", on_disk, "the unfinished job from before the restart is gone")
        self.assertEqual(gs.load(relay.st(relay.MAIL_GATE), {}).get("status"), "asking")  # told at the next start


import base64, io, threading
from contextlib import redirect_stdout
import test_gmail_send as tg


class Race(unittest.TestCase):
    """Real threads + the real flock: Don't send while gmail_send.py send is between its lock and drafts.send."""
    setUp, show = tg.RelaySide.setUp, tg.RelaySide.show  # not a subclass: its tests run once, in test_gmail_send
    def gmail(self, inside=None, go=None):
        sent = []
        raw = base64.urlsafe_b64encode(b"To: v@example.nl\r\nSubject: q\r\n\r\nhello\r\n").decode()
        def api(tok, path, body=None):
            if body is not None:
                if inside:
                    inside.set(); go.wait(5)
                sent.append(body["id"]); return {"id": "m1"}
            return {"message": {"raw": raw}}
        return api, sent

    def run_gs(self, cmd, api, out):
        conf = os.path.join(relay.CFG["paths"]["state_dir"], "rv.toml")
        with open(conf, "w") as f:
            f.write(f'[paths]\nstate_dir = "{relay.CFG["paths"]["state_dir"]}"\n[mail]\nuser = "me@gmail.com"\n')
        with mock.patch.object(gs, "access", return_value="tok"), mock.patch.object(gs, "api", api), \
                mock.patch.object(gs, "CONF", conf), redirect_stdout(io.StringIO()) as o:
            try:
                gs.main([cmd, tg.DRAFT])
            except SystemExit as e:
                out.append(str(e)); return
        out.append(o.getvalue())

    def ready(self):
        api, _ = self.gmail()
        self.run_gs("show", api, [])
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)

    def test_dont_send_while_send_is_past_the_lock(self):
        self.ready()
        inside, go, out = threading.Event(), threading.Event(), []
        api, sent = self.gmail(inside, go)
        t = threading.Thread(target=self.run_gs, args=("send", api, out)); t.start()
        self.assertTrue(inside.wait(5))
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", relay.MAIL_NO)  # while drafts.send is in flight
        go.set(); t.join(5)
        self.assertEqual(sent, [tg.DRAFT])
        self.assertIn("Too late", self.texts[-1])

    def test_dont_send_before_send_takes_the_lock(self):
        self.ready()
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", relay.MAIL_NO)
        out = []
        api, sent = self.gmail()
        self.run_gs("send", api, out)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
