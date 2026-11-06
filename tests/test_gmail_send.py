"""Send a Gmail draft only after the owner's OK on that exact draft (5 Nov 2026, #125). Against a fake Gmail:
no real mail is ever sent here. Run: python3 -m unittest discover tests"""
import base64, io, json, os, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import relay  # noqa: E402
import test_threads  # noqa: E402
import gmail_send as gs  # noqa: E402

DRAFT = "r9222184318451542730"


def raw(body):
    m = f"To: vendor@example.nl\r\nCc: partner@example.nl\r\nSubject: Re: question\r\n\r\n{body}\r\n"
    return base64.urlsafe_b64encode(m.encode()).decode()


class SendGate(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.conf = os.path.join(self.tmp, "relay.config.toml")
        with open(self.conf, "w") as f:
            f.write('[paths]\nstate_dir = "."\n[mail]\nuser = "me@gmail.com"\n')
        self.body, self.sent = "Hi, one question", []

        def api(tok, path, body=None):
            if body is not None:  # the only POST: drafts.send
                self.assertEqual(path, "drafts/send")
                self.sent.append(body["id"])
                return {"id": "m1"}
            self.assertEqual(path, f"drafts/{DRAFT}?format=raw")
            return {"message": {"raw": raw(self.body)}}
        for p in (mock.patch.object(gs, "access", return_value="tok"), mock.patch.object(gs, "api", api)):
            p.start()
            self.addCleanup(p.stop)

    def run_cmd(self, cmd):
        with redirect_stdout(io.StringIO()) as out:
            try:
                with mock.patch.object(gs, "CONF", self.conf):
                    gs.main([cmd, DRAFT])
            except SystemExit as e:
                return str(e)
        return out.getvalue()

    def owner_says(self, text, later=1, where=""):
        with open(os.path.join(self.tmp, "owner-last.json"), "w") as f:
            json.dump({"time": time.time() + later, "text": text, "where": where}, f)

    def test_no_ok_means_no_send(self):
        self.assertIn("not shown", self.run_cmd("send"))  # never shown
        self.owner_says("שלח", later=-60)  # an OK from before the show does not count
        self.assertIn("Subject: Re: question", self.run_cmd("show"))
        self.assertIn("NOT SENT: the owner wrote nothing after the show", self.run_cmd("send"))
        for text in ("אל תשלח", "ok but change the date", "wait", "don’t send", "what about the date?"):
            self.owner_says(text)
            self.assertIn("NOT SENT", self.run_cmd("send"), text)
        self.assertEqual(self.sent, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "mail-send", "sent.log")))

    def test_ok_sends_once_and_logs(self):
        self.run_cmd("show")
        self.owner_says("ok שלח")
        self.assertIn("SENT: draft", self.run_cmd("send"))
        self.assertIn("one OK = one mail", self.run_cmd("send"))
        self.assertEqual(self.sent, [DRAFT])
        with open(os.path.join(self.tmp, "mail-send", "sent.log")) as f:
            line = json.loads(f.readline())
        self.assertEqual((line["draft"], line["to"], line["subject"]), (DRAFT, "vendor@example.nl", "Re: question"))

    def test_a_failed_send_is_outcome_unknown_never_sent(self):
        self.run_cmd("show")
        self.owner_says("שלח")
        with mock.patch.object(gs, "api", side_effect=[{"message": {"raw": raw(self.body)}}, TimeoutError("timed out")]):
            out = self.run_cmd("send")
        self.assertIn("OUTCOME UNKNOWN", out)
        self.assertNotIn("SENT:", out)
        with open(os.path.join(self.tmp, "mail-send", "sent.log")) as f:
            self.assertIn("UNKNOWN", json.loads(f.readline())["outcome"])
        self.assertIn("one OK = one mail", self.run_cmd("send"))  # and never sent again on the same OK

    def test_only_a_plain_owner_chat_ok_counts(self):
        # review #139 F1 + F3: no config from argv; a thread OK, a poll answer or an old record never opens it
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                gs.main(["send", DRAFT, self.conf])
        self.assertIn("no config argument", str(e.exception))
        self.run_cmd("show")
        for where in ("#134", "poll", None):
            self.owner_says("ok", where=where)
            self.assertIn("for a thread or a poll", self.run_cmd("send"), where)
        self.assertEqual(self.sent, [])

    def test_changed_draft_needs_a_new_show(self):
        self.run_cmd("show")
        self.body = "Hi, other text"
        self.owner_says("שלח")
        self.assertIn("draft changed", self.run_cmd("send"))
        self.assertEqual(self.sent, [])

    def test_relay_records_the_owner_words(self):
        test_threads.ThreadTests.setUp(self)
        self.addCleanup(test_threads.ThreadTests.tearDown, self)
        with mock.patch.object(relay, "quoted_text", return_value='[Replying to your message: "send it"]\n'), \
                mock.patch.object(relay, "resume_note", return_value=""):
            relay.owner_prep("m1", "שלח", None)
        last = json.loads(relay.read(relay.st("owner-last.json")))
        self.assertEqual((last["text"], last["where"]), ("שלח", ""))  # his own words, in the owner chat
        with mock.patch.object(relay, "quoted_text", return_value=""), mock.patch.object(relay, "resume_note", return_value=""):
            relay.owner_prep("m2", "ok", None, where="#134")  # "#134 ok": thread_route strips the tag
            self.assertEqual(json.loads(relay.read(relay.st("owner-last.json")))["where"], "#134")
            relay.owner_prep("m3", 'ANSWER to "Send?": Yes', None)
            self.assertEqual(json.loads(relay.read(relay.st("owner-last.json")))["where"], "poll")
        self.assertIn("gmail_send.py show", relay.MAIL_RULES)
        self.assertIn("only after the owner's explicit OK on that exact draft", relay.MAIL_RULES)


if __name__ == "__main__":
    unittest.main()
