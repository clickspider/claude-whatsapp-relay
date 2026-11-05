"""Mail watch (5 Nov 2026): Gmail API read only, consequence-based sorting, night rules, reply watch, the 08:00
digest, and the one-time Allow script, against a fake Google. Run: python3 -m unittest discover tests"""
import base64, datetime, io, json, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import relay  # noqa: E402
import test_threads  # noqa: E402
import gmail_auth  # noqa: E402

AT = lambda h: datetime.datetime(2026, 11, 5, h, 1)
b64 = lambda t: base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")


def msg(mid, thr, frm, subject, body, labels=("INBOX", "UNREAD"), bulk=False, date=1000, html=False):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    if bulk:
        headers.append({"name": "List-Unsubscribe", "value": "<mailto:off@shop.example>"})
    part = {"mimeType": "text/html" if html else "text/plain", "body": {"data": b64(body)}}
    return {"id": mid, "threadId": thr, "labelIds": list(labels), "internalDate": str(date), "snippet": body[:20],
            "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": [part]}}


class FakeGmail:
    """messages by id; history = ids added since history 100. Records every call: only GETs exist here."""
    def __init__(self, mails=(), threads=None, newest="200"):
        self.mails, self.threads, self.newest, self.calls = {m["id"]: m for m in mails}, threads or {}, newest, []

    def __call__(self, path, **params):
        self.calls.append((path, params))
        if path == "profile":
            return {"historyId": "150"}
        if path == "history":
            if params["startHistoryId"] == "old":
                raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
            assert params["labelId"] == "INBOX" and params["historyTypes"] == "messageAdded"
            return {"historyId": self.newest,
                    "history": [{"messagesAdded": [{"message": {"id": i}}]} for i in self.mails]}
        if path.startswith("messages/"):
            return self.mails[path.split("/")[1]]
        if path.startswith("threads/"):
            if path.split("/")[1] not in self.threads:
                raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
            return {"messages": self.threads[path.split("/")[1]]}
        raise AssertionError(f"unexpected Gmail call {path}")


class MailWatch(unittest.TestCase):
    def setUp(self):
        test_threads.ThreadTests.setUp(self)
        relay.CFG["mail"].update(enabled=True, user="me@gmail.com",
                                 watch={"lease": ["law.example", "חוזה"], "dj": ["dj.example"]})
        relay.CFG["owner"]["night"] = [23, 8]
        relay.threads_db("5", title="lease defence", state="review", topic="lease")
        relay.threads_db("9", title="old", state="closed")
        relay._mail.update(busy=False, next=0.0, told="", token=("tok", 9e12))
        self.notes = []
        self.p2 = [mock.patch.object(relay, "agent_note", lambda name, note, dry=False: self.notes.append(note)),
                   mock.patch.object(relay, "mail_refresh_token", return_value="refresh")]
        for p in self.p2:
            p.start()

    def tearDown(self):
        for p in self.p2:
            p.stop()
        test_threads.ThreadTests.tearDown(self)

    def check(self, g, sort=None, night=False):
        with mock.patch.object(relay, "gmail", g), mock.patch.object(relay, "owner_night", return_value=night), \
                mock.patch.object(relay, "mail_sort", sort or (lambda ask: self.fail("no sort expected"))):
            relay.mail_check()

    def test_config_checks(self):
        bad = dict(relay.CFG, mail=dict(relay.CFG["mail"], user="", check_minutes=0, digest_hour=24,
                                        watch={"x": "not a list"}))
        errs = " ".join(relay.validate(bad)[0])
        for k in ("mail.user", "mail.check_minutes", "mail.digest_hour", "mail.watch"):
            self.assertIn(k, errs)
        self.assertIn("@@WATCHMAIL", relay.coordinator_rules(relay.CFG))
        self.assertNotIn("@@WATCHMAIL", relay.coordinator_rules(dict(relay.CFG, mail=dict(relay.CFG["mail"], enabled=False))))
        self.assertTrue(os.path.isabs(relay.CFG["mail"]["client_file"]))

    def test_first_run_and_old_history_start_from_now(self):
        g = FakeGmail()
        self.check(g)
        self.assertEqual(relay.mail_state()["history"], "150")
        relay.mail_state(lambda s: s.update(history="old"))
        self.check(g)
        self.assertEqual(relay.mail_state()["history"], "150")
        self.assertEqual(self.notes, [])

    def test_sorting_by_consequence(self):
        relay.mail_state(lambda s: s.update(history="100", threads={"777": "5"}))
        g = FakeGmail([
            msg("a1", "555", "Alex <alex@law.example>", "Defence draft", "Please sign by Friday"),
            msg("a2", "556", "Shop <deals@shop.example>", "50% off", "Sale!", bulk=True,
                labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS")),
            msg("a3", "557", "Bank <info@bank.example>", "Statement", "read already", labels=("INBOX",)),
            msg("a4", "777", "Court <filing@justice.gov.il>", "Re: 57427-08-26", "received"),
            msg("a5", "558", "Me <me@gmail.com>", "note to self", "x"),
            msg("a6", "559", "Promo <hi@app.example>", "URGENT: last chance", "<p>buy <b>now</b></p>", html=True),
        ])
        asked = []

        def sort(ask):
            asked.extend((m["id"], m["text"]) for m in ask)
            return relay.mail_verdicts("<0> | carry=#5 | decide=yes | risk=sign by Fri 6 Nov | wait_hurts=no | "
                                       "Draft defence to sign\n<1> | carry=no | decide=no | risk=no | "
                                       "wait_hurts=yes | ad")
        self.check(g, sort)
        self.assertEqual(asked, [("a1", "Please sign by Friday"), ("a6", "buy now")])  # no haiku for the rest
        self.assertTrue(all(p in ("history",) or p.startswith("messages/") for p, _ in g.calls))
        self.assertEqual(len(self.notes), 1)
        note = self.notes[0]
        for want in ("Defence draft", "For #5.", "Risk: sign by Fri 6 Nov", "New mail in the email thread of #5",
                     "thread-id 555"):
            self.assertIn(want, note)
        self.assertNotIn("URGENT", note)
        s = relay.mail_state()
        self.assertEqual((s["history"], s["threads"]["555"]), ("200", "5"))
        self.assertEqual(len(s["digest"]), 2)

    def test_night_sends_only_what_cannot_wait(self):
        relay.mail_state(lambda s: s.update(history="100"))
        g = FakeGmail([msg("b1", "1", "a@dj.example", "Quote", "x"), msg("b2", "2", "b@bank.example", "Card blocked", "fraud")])
        sort = lambda ask: relay.mail_verdicts("<0> | carry=dj | decide=yes | risk=no | wait_hurts=no | quote\n"
                                               "<1> | carry=no | decide=yes | risk=security | wait_hurts=yes | card")
        self.check(g, sort, night=True)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("Card blocked", self.notes[0])
        self.assertNotIn("Quote", self.notes[0])
        self.assertEqual(len(relay.mail_state()["digest"]), 2)  # both in the 08:00 digest

    def test_reply_watch(self):
        text = relay.take_watchmail("ok, I will tell you\n@@WATCHMAIL #5: law.example\n@@WATCHMAIL 18c2f0a1b2c3d4e5\n")
        self.assertEqual(text, "ok, I will tell you")
        self.assertEqual(relay.take_watchmail("no marker  "), "no marker  ")
        relay.mail_state(lambda s: s.update(history="100"))
        self.check(FakeGmail([msg("c1", "42", "office@law.example", "Re: question", "answer")]))  # no haiku
        self.assertIn("The reply you asked to watch (#5)", self.notes[0])
        self.assertEqual([w["key"] for w in relay.mail_state()["watch"]], ["18c2f0a1b2c3d4e5"])

    def test_sort_failure_keeps_the_mail_and_tells_once(self):
        relay.mail_state(lambda s: s.update(history="100"))
        g = FakeGmail([msg("d1", "1", "x@y.example", "Hi", "x")])

        def broken(ask):
            raise RuntimeError("the mail sort gave no answer (exit 1)")
        for _ in range(2):
            with mock.patch.object(relay, "gmail", g), mock.patch.object(relay, "mail_sort", broken):
                relay._mail["busy"] = True
                relay.mail_run(relay.mail_check)
        self.assertEqual(relay.mail_state()["history"], "100")
        self.assertEqual(len(self.notes), 1)
        self.assertIn("mail check failed", self.notes[0])
        self.assertFalse(relay._mail["busy"])

    def test_no_access_yet_means_off_quietly(self):
        g = FakeGmail()
        with mock.patch.object(relay, "mail_refresh_token", return_value=""), \
                mock.patch.object(relay, "gmail", g), mock.patch("builtins.print") as out:
            relay.mail_check()
            relay.mail_check()
        self.assertEqual((g.calls, self.notes, out.call_count), ([], [], 1))

    def test_access_token_cached_and_revoked_access_named(self):
        relay._mail["token"] = ("", 0.0)
        with open(relay.CFG["mail"]["client_file"], "w") as f:
            json.dump({"installed": {"client_id": "cid", "client_secret": "sec"}}, f)
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req.data.decode())
            return io.BytesIO(b'{"access_token": "A1", "expires_in": 3600}')
        with mock.patch.object(relay.urllib.request, "urlopen", urlopen):
            self.assertEqual(relay.mail_access("R"), "A1")
            self.assertEqual(relay.mail_access("R"), "A1")
        self.assertEqual(len(seen), 1)
        self.assertIn("grant_type=refresh_token", seen[0])
        relay._mail["token"] = ("", 0.0)
        no = mock.Mock(side_effect=urllib.error.HTTPError("u", 400, "Bad Request", {}, None))
        with mock.patch.object(relay.urllib.request, "urlopen", no), self.assertRaises(RuntimeError) as e:
            relay.mail_access("R")
        self.assertIn("gmail_auth.py", str(e.exception))

    def test_morning_digest(self):
        items = [{"id": "m1", "thrid": "1", "date": 10, "line": "From a: \"read it\""},
                 {"id": "m2", "thrid": "2", "date": 10, "line": "From b: \"answered it\""},
                 {"id": "m3", "thrid": "3", "date": 10, "line": "From c: \"still open\""},
                 {"id": "m4", "thrid": "4", "date": 10, "line": "From d: \"deleted\""}]
        relay.mail_state(lambda s: s.update(history="100", digest=items))
        g = FakeGmail(threads={"1": [{"id": "m1", "labelIds": ["INBOX"]}],
                               "2": [{"id": "m2", "labelIds": ["UNREAD"]}, {"id": "x", "labelIds": ["SENT"], "internalDate": "20"}],
                               "3": [{"id": "m3", "labelIds": ["UNREAD"]}, {"id": "y", "labelIds": ["SENT"], "internalDate": "5"}]})
        started = []
        with mock.patch.object(relay.threading, "Thread",
                               lambda target, args, daemon: mock.Mock(start=lambda: started.append(args[0]))):
            relay.mail_tick(AT(7))
            self.assertEqual(started, [relay.mail_check])  # before 08:00: a normal check
            relay._mail.update(busy=False, next=0.0)
            relay.mail_tick(AT(8))
            self.assertEqual(started[-1], relay.mail_morning)
        with mock.patch.object(relay, "gmail", g), mock.patch.object(relay, "owner_now", lambda: AT(8)):
            relay.mail_morning()
        self.assertEqual(len(self.notes), 1)
        self.assertIn("still open", self.notes[0])
        for gone in ("read it", "answered it", "deleted"):
            self.assertNotIn(gone, self.notes[0])
        s = relay.mail_state()
        self.assertEqual((s["digest"], s["day"]), ([], "2026-11-05"))
        relay._mail.update(busy=False, next=9e12)
        with mock.patch.object(relay.threading, "Thread", lambda **k: self.fail("no second digest today")):
            relay.mail_tick(AT(9))

    def test_coordinator_prepares_the_next_step(self):
        """Owner 5 Nov 2026: the coordinator reads the mail and prepares the next step; drafts and calendar
        only after the owner's yes; a 'reply to start' email is the owner's call; nothing is ever sent."""
        r = relay.MAIL_RULES
        for part in ("get_thread", "@@TO #<n>", "@@DRAFT", "only after the owner says yes",
                     "is the owner's call", "Never send, delete, archive, label or mark mail", "@@WATCHMAIL"):
            self.assertIn(part, r)

    def test_verdict_lines(self):
        v = relay.mail_verdicts("<0> | carry=no | decide=no | risk=no | wait_hurts=yes | URGENT sale\n"
                                "1 | carry=#5. | decide=No | risk=None | wait_hurts=no | update")
        self.assertEqual((v[0]["important"], v[0]["urgent"]), (False, False))
        self.assertEqual((v[1]["important"], v[1]["carry"], v[1]["line"]), (True, "#5", "update"))


class AllowScript(unittest.TestCase):
    """scripts/gmail_auth.py end to end: client file out of Downloads, Allow in the browser, token in the Keychain."""
    def run_auth(self, scope, email="me@gmail.com"):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        conf = os.path.join(tmp, "relay.config.toml")
        with open(conf, "w") as f:
            f.write('[mail]\nuser = "me@gmail.com"\n')
        down = os.path.join(tmp, "client_secret_123.json")
        with open(down, "w") as f:
            json.dump({"installed": {"client_id": "cid", "client_secret": "sec"}}, f)
        saved, real_open = [], urllib.request.urlopen

        def browser(url):  # Google sends the browser back to the local server with a code
            q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
            self.assertEqual(urllib.parse.unquote(q["scope"]), gmail_auth.SCOPE)
            back = f"{urllib.parse.unquote(q['redirect_uri'])}/?code=C1&state={q['state']}"
            threading.Thread(target=lambda: real_open(back, timeout=5).read(), daemon=True).start()
            return True

        def urlopen(req, timeout=None):
            if getattr(req, "data", None):
                self.assertIn("code_verifier=", req.data.decode())
                return io.BytesIO(json.dumps({"access_token": "A", "refresh_token": "R", "scope": scope}).encode())
            return io.BytesIO(json.dumps({"emailAddress": email}).encode())
        with mock.patch.object(gmail_auth.webbrowser, "open", browser), \
                mock.patch.object(gmail_auth.urllib.request, "urlopen", urlopen), \
                mock.patch.object(gmail_auth.subprocess, "run", lambda a, check: saved.append(a)), \
                mock.patch("builtins.print"):
            try:
                gmail_auth.main([conf, down])
            except SystemExit as e:
                return saved, str(e), tmp, down
        return saved, "", tmp, down

    def test_allow_saves_the_token(self):
        saved, err, tmp, down = self.run_auth(gmail_auth.SCOPE)
        self.assertEqual(err, "")
        self.assertFalse(os.path.exists(down))
        dest = os.path.join(tmp, "gmail-client.json")
        self.assertEqual(oct(os.stat(dest).st_mode & 0o777), "0o600")
        self.assertEqual(saved[0][:7], ["security", "add-generic-password", "-U", "-s", "relay-gmail-oauth", "-a",
                                        "me@gmail.com"])

    def test_more_rights_or_wrong_account_saves_nothing(self):
        saved, err, _, _ = self.run_auth(gmail_auth.SCOPE + " https://www.googleapis.com/auth/gmail.send")
        self.assertEqual(saved, [])
        self.assertIn("gmail.readonly", err)
        saved, err, _, _ = self.run_auth(gmail_auth.SCOPE, email="other@gmail.com")
        self.assertEqual(saved, [])
        self.assertIn("other@gmail.com", err)


if __name__ == "__main__":
    unittest.main()
