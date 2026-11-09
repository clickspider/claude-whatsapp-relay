"""Send a Gmail draft only after the owner's vote on the relay's poll for that exact draft (5 Nov 2026, #125;
poll 6 Nov 2026 14:27, #139). Against a fake Gmail:
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

ORIG_SEND_OWNER_FILE = relay.send_owner_file

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

    def slot(self):
        return gs.load(os.path.join(self.tmp, "mail-send", "pending.json"), {})

    def relay_asks(self, poll="P1"):
        """What relay.mail_gate_ask writes once its poll went out."""
        gs.save(os.path.join(self.tmp, "mail-send", "pending.json"),
                dict(self.slot(), status="asked", poll=poll, asked=time.time()))

    def owner_votes(self, choice, poll="P1", later=1):
        """What relay.mail_gate_vote writes for the owner's tap."""
        gs.save(os.path.join(self.tmp, "mail-send", "pending.json"),
                dict(self.slot(), vote={"poll": poll, "choice": choice, "time": time.time() + later}))

    def test_no_vote_means_no_send(self):
        self.assertIn("not shown", self.run_cmd("send"))  # never shown
        out = self.run_cmd("show")
        self.assertIn("Subject: Re: question", out)
        self.assertIn("A typed 'ok' never sends", out)
        self.assertEqual((self.slot()["status"], self.slot()["kind"]), ("requested", "send"))
        self.assertIn("Re: question", self.slot()["summary"])  # what the relay shows the owner
        self.assertIn("NOT SENT: the owner was not asked yet", self.run_cmd("send"))
        self.relay_asks()
        with open(os.path.join(self.tmp, "owner-last.json"), "w") as f:  # a typed "ok" counts for nothing
            json.dump({"time": time.time() + 1, "text": "ok שלח", "where": ""}, f)
        self.assertIn("has not voted", self.run_cmd("send"))
        self.owner_votes(gs.NO)
        self.assertIn("voted not to", self.run_cmd("send"))
        self.assertEqual(self.sent, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "mail-send", "sent.log")))

    def test_a_send_vote_sends_once_and_logs(self):
        self.run_cmd("show")
        self.relay_asks()
        self.owner_votes(gs.SEND)
        self.assertIn("SENT: draft", self.run_cmd("send"))
        self.assertIn("one vote = one action", self.run_cmd("send"))
        self.assertEqual(self.sent, [DRAFT])
        with open(os.path.join(self.tmp, "mail-send", "sent.log")) as f:
            line = json.loads(f.readline())
        self.assertEqual((line["draft"], line["to"], line["subject"], line["poll"]),
                         (DRAFT, "vendor@example.nl", "Re: question", "P1"))

    def test_only_the_vote_on_the_slots_own_poll_counts(self):
        self.run_cmd("show")
        self.relay_asks("P2")
        self.owner_votes(gs.SEND, poll="P1")  # a vote on an older poll
        self.assertIn("another poll", self.run_cmd("send"))
        self.owner_votes(gs.SEND, poll="P2")
        self.run_cmd("show")  # a new show replaces the slot: the vote it had is gone
        self.assertIn("not asked yet", self.run_cmd("send"))
        self.assertEqual(self.sent, [])

    def test_a_failed_send_is_outcome_unknown_never_sent(self):
        self.run_cmd("show")
        self.relay_asks()
        self.owner_votes(gs.SEND)
        with mock.patch.object(gs, "api", side_effect=[{"message": {"raw": raw(self.body)}}, TimeoutError("timed out")]):
            out = self.run_cmd("send")
        self.assertIn("OUTCOME UNKNOWN", out)
        self.assertNotIn("SENT:", out)
        with open(os.path.join(self.tmp, "mail-send", "sent.log")) as f:
            self.assertIn("UNKNOWN", json.loads(f.readline())["outcome"])
        self.assertIn("one vote = one action", self.run_cmd("send"))  # and never sent again on the same vote

    def test_the_config_comes_only_from_the_relay(self):
        # review #139 F1, N6: no config from argv; not started by the relay = nothing
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                gs.main(["send", DRAFT, self.conf])
        self.assertIn("no config argument", str(e.exception))
        with mock.patch.object(gs, "CONF", ""), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                gs.main(["send", DRAFT])
        self.assertIn("RELAY_CONFIG is not set", str(e.exception))
        self.assertEqual(self.sent, [])

    def test_the_window_runs_from_the_show_and_no_future_vote(self):
        self.run_cmd("show")
        self.relay_asks()
        self.owner_votes(gs.SEND, later=3600)
        self.assertIn("in the future", self.run_cmd("send"))
        self.owner_votes(gs.SEND, later=60)
        later = time.time() + gs.OK_HOURS * 3600 + 120  # a vote 1 min after the show, the send 2 h later
        with mock.patch.object(gs.time, "time", return_value=later):
            self.assertIn("too old", self.run_cmd("send"))
        self.assertEqual(self.sent, [])

    def test_an_attach_show_never_opens_a_send(self):
        gs.save(os.path.join(self.tmp, "mail-send", "pending.json"),
                {"kind": "attach", "draft": DRAFT, "key": "k", "time": time.time(), "poll": "P1",
                 "vote": {"poll": "P1", "choice": gs.SEND, "time": time.time()}})  # the shared slot
        self.assertIn("not shown to the owner first", self.run_cmd("send"))
        self.assertEqual(self.sent, [])

    def test_the_relay_hands_its_config_to_the_scripts(self):
        cfg = {"owner": {"jid": "x"}, "config_path": os.path.join(self.tmp, "relay.config.toml"),
               "paths": {"state_dir": self.tmp}}
        saved = (relay.CFG, relay.OWNER)
        self.addCleanup(lambda: (setattr(relay, "CFG", saved[0]), setattr(relay, "OWNER", saved[1])))
        with mock.patch.dict(os.environ, {"RELAY_CONFIG": ""}):
            relay.setup(cfg)
            self.assertEqual(os.environ["RELAY_CONFIG"], cfg["config_path"])  # claude runs inherit it

    def test_save_is_atomic(self):
        path = os.path.join(self.tmp, "mail-send", "pending.json")
        gs.save(path, {"draft": "a"})
        with mock.patch.object(gs.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                gs.save(path, {"draft": "b"})
        self.assertEqual(gs.load(path, {}), {"draft": "a"})  # the old version stays whole

    def test_a_send_show_puts_the_drafts_files_in_the_slot(self):  # review P2-5
        from email.message import EmailMessage
        m = EmailMessage()
        m["To"], m["Subject"] = "vendor@example.nl", "Re: question"
        m.set_content("Hi")
        m.add_attachment(b"%PDF x", maintype="application", subtype="pdf", filename="letter.pdf")
        self.body = None
        with mock.patch.object(gs, "draft", return_value=("h1", gs.email.message_from_bytes(
                m.as_bytes(), policy=gs.email.policy.default), "")):
            self.run_cmd("show")
        self.assertEqual(self.slot()["files"], [os.path.join(self.tmp, "mail-send", "files", "h1", "0", "letter.pdf")])
        with open(self.slot()["files"][0], "rb") as f:
            self.assertEqual(f.read(), b"%PDF x")

    def test_changed_draft_needs_a_new_show(self):
        self.run_cmd("show")
        self.relay_asks()
        self.owner_votes(gs.SEND)
        self.body = "Hi, other text"
        self.assertIn("draft changed", self.run_cmd("send"))
        self.assertEqual(self.sent, [])


class Describe(unittest.TestCase):
    def mail(self, plain, html):
        from email.message import EmailMessage
        m = EmailMessage()
        m["To"], m["Subject"] = "a@example.nl", "s"
        m.set_content(plain)
        m.add_alternative(html, subtype="html")
        return gs.email.message_from_bytes(m.as_bytes(), policy=gs.email.policy.default)

    def test_a_different_html_part_is_shown(self):  # review P2-6
        out = gs.describe(self.mail("I accept the offer.", "<p>I REJECT the offer.</p>"))
        self.assertIn("I accept", out)
        self.assertIn("(HTML version, what most mail apps show:)\n", out)
        self.assertIn("REJECT", out)

    def test_the_same_html_part_is_not_shown_twice(self):
        out = gs.describe(self.mail("I accept the offer.", "<p>I accept\nthe offer.</p>"))
        self.assertNotIn("HTML version", out)

    def test_the_drafts_files_are_saved_for_the_owner(self):  # review P2-5
        from email.message import EmailMessage
        m = EmailMessage()
        m["To"] = "a@example.nl"
        m.set_content("hi")
        m.add_attachment(b"%PDF one", maintype="application", subtype="pdf", filename="../../letter.pdf")
        m.add_attachment(b"%PDF two", maintype="application", subtype="pdf", filename=".hidden.pdf")
        msg = gs.email.message_from_bytes(m.as_bytes(), policy=gs.email.policy.default)
        folder = self.enterContext(tempfile.TemporaryDirectory())
        out = gs.save_files(msg, folder)
        self.assertEqual(out, [os.path.join(folder, "0", "letter.pdf"), os.path.join(folder, "1", "hidden.pdf")])
        with open(out[0], "rb") as f:
            self.assertEqual(f.read(), b"%PDF one")


class ShortRoundP2(unittest.TestCase):
    """Reviewer short round 6 Nov, P2-2/P2-3/P2-4 (gmail_send side)."""
    def test_a_forwarded_mail_is_saved_whole(self):  # P2-2: was a 0-byte .eml
        from email.message import EmailMessage
        inner = EmailMessage()
        inner["Subject"] = "the old mail"
        inner.set_content("inner text")
        m = EmailMessage()
        m["To"] = "a@example.nl"
        m.set_content("see below")
        m.add_attachment(inner, filename="fwd.eml")
        msg = gs.email.message_from_bytes(m.as_bytes(), policy=gs.email.policy.default)
        folder = self.enterContext(tempfile.TemporaryDirectory())
        [path] = gs.save_files(msg, folder)
        with open(path, "rb") as f:
            data = f.read()
        self.assertIn(b"inner text", data)
        self.assertIn(b"the old mail", data)

    def test_a_base64_forwarded_mail_is_saved_decoded(self):  # review #148 N6
        raw = (b"To: a@example.nl\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\n"
               b"Content-Type: text/plain\r\n\r\nsee below\r\n--B\r\nContent-Type: message/rfc822\r\n"
               b"Content-Disposition: attachment; filename=fwd.eml\r\nContent-Transfer-Encoding: base64\r\n\r\n"
               + base64.encodebytes(b"Subject: the old mail\r\n\r\ninner text\r\n") + b"--B--\r\n")
        msg = gs.email.message_from_bytes(raw, policy=gs.email.policy.default)
        [path] = gs.save_files(msg, self.enterContext(tempfile.TemporaryDirectory()))
        with open(path, "rb") as f:
            self.assertIn(b"inner text", f.read())

    def test_a_linked_files_folder_is_never_cleaned(self):  # review #148 N5
        t = SendGate("test_no_vote_means_no_send")
        t.setUp()
        self.addCleanup(t.doCleanups)
        victim = os.path.join(t.tmp, "victim", "keep")
        os.makedirs(victim)
        os.makedirs(os.path.join(t.tmp, "mail-send"))
        os.symlink(os.path.join(t.tmp, "victim"), os.path.join(t.tmp, "mail-send", "files"))
        self.assertIn("is a link", t.run_cmd("show"))
        self.assertTrue(os.path.isdir(victim))

    def test_html_entities_do_not_make_a_second_version(self):  # P2-3
        out = Describe.mail(None, "Tom's offer & more.", "<p>Tom&#39;s offer &amp; more.</p>")
        self.assertNotIn("HTML version", gs.describe(out))

    def test_a_new_show_removes_the_old_copies(self):  # P2-4
        t = SendGate("test_no_vote_means_no_send")
        t.setUp()
        self.addCleanup(t.doCleanups)
        old = os.path.join(t.tmp, "mail-send", "files", "oldhash", "0")
        os.makedirs(old)
        open(os.path.join(old, "x.pdf"), "w").close()
        t.run_cmd("show")
        self.assertFalse(os.path.exists(os.path.dirname(old)))  # this draft has no files, so nothing new


class RelaySide(unittest.TestCase):
    """relay.mail_gate_ask / mail_gate_vote / vote_job: the relay, not Claude, asks and records the tap."""
    def setUp(self):
        test_threads.ThreadTests.setUp(self)
        self.addCleanup(test_threads.ThreadTests.tearDown, self)
        self.path = relay.st(relay.MAIL_GATE)
        self.texts, self.files, self.polls_sent, self.added = [], [], [], []
        def api(path, body):
            self.assertEqual(path, "poll")
            self.polls_sent.append(body)
            return {"message_id": f"P{len(self.polls_sent)}"}
        for p in (mock.patch.object(relay, "api", api),
                  mock.patch.object(relay, "send_text", lambda chat, t, **k: self.texts.append(t)),
                  mock.patch.object(relay, "send_owner_file", lambda f, root=None: self.files.append(f) or True),
                  mock.patch.object(relay, "owner_add", lambda *a: self.added.append(a))):
            p.start()
            self.addCleanup(p.stop)

    def show(self, **entry):
        gs.request(self.path, dict({"kind": "attach", "draft": DRAFT, "key": "k", "subject": "Re: letter",
                                    "files": ["/d/letter.pdf"], "summary": "To: a@example.nl\n  letter.pdf"}, **entry))

    def test_the_relay_sends_summary_files_and_its_own_poll(self):
        self.show()
        relay.mail_gate_ask()
        self.assertEqual(self.texts, ["To: a@example.nl\n  letter.pdf"])
        self.assertEqual(self.files, ["/d/letter.pdf"])
        self.assertEqual(self.polls_sent[0]["options"], [relay.MAIL_SEND, relay.MAIL_NO])
        self.assertEqual([relay.MAIL_SEND, relay.MAIL_NO], [gs.SEND, gs.NO])
        self.assertIn("Re: letter", self.polls_sent[0]["question"])
        p = gs.load(self.path, {})
        self.assertEqual((p["status"], p["poll"]), ("asked", "P1"))
        self.assertEqual(relay.polls()["P1"]["kind"], "mailgate")
        relay.mail_gate_ask()  # asked once only
        self.assertEqual(len(self.polls_sent), 1)

    def test_no_poll_means_nothing_can_be_approved(self):
        self.show()
        with mock.patch.object(relay, "api", side_effect=OSError("bridge down")):
            relay.mail_gate_ask()
        self.assertIn("nothing will be sent", self.texts[-1])
        p = gs.load(self.path, {})
        self.assertEqual((p["status"], p.get("poll")), ("failed", None))
        relay.mail_gate_sweep()  # the start-up sweep does not tell it a second time
        self.assertEqual(sum("nothing will be sent" in t for t in self.texts), 1)
        self.assertIn("not asked yet", gs.vote_ok(p))

    def test_the_tap_is_recorded_and_goes_to_the_coordinator(self):
        self.show()
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        p = gs.load(self.path, {})
        self.assertEqual((p["vote"]["poll"], p["vote"]["choice"]), ("P1", gs.SEND))
        self.assertEqual(gs.vote_ok(p), "")
        self.assertIn(": Send", self.added[0][1])
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", relay.MAIL_NO)  # he changes his tap: that wins
        self.assertEqual(gs.load(self.path, {})["vote"]["choice"], gs.NO)
        self.assertNotEqual(gs.vote_ok(gs.load(self.path, {})), "")
        self.assertEqual(self.texts[-1], "Changed: not sent.")
        relay.on_vote("v3", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)  # back to Send never opens it again
        self.assertEqual(gs.load(self.path, {})["vote"]["choice"], gs.NO)
        self.assertEqual(len(self.added), 1)
        self.assertEqual(self.texts[-1], "That vote is closed: nothing will be sent. Ask me to show the mail again.")

    def test_one_tap_is_written_once(self):
        self.show()
        relay.mail_gate_ask()
        self.assertTrue(relay.mail_gate_vote("P1", relay.MAIL_NO))
        self.assertFalse(relay.mail_gate_vote("P1", relay.MAIL_SEND))  # a second tap never turns No into Send
        self.assertEqual(gs.load(self.path, {})["vote"]["choice"], gs.NO)
        self.assertFalse(relay.mail_gate_vote(None, relay.MAIL_SEND))

    def test_the_poll_comes_after_the_coordinator_answer(self):
        order = []
        with mock.patch.object(relay, "send_text", lambda chat, t, **k: order.append("answer")), \
                mock.patch.object(relay, "mail_gate_ask", lambda: order.append("poll")):
            relay.coordinator_send("Here is the mail.", "send the letter")
        self.assertEqual(order, ["answer", "poll"])

    def test_a_vote_after_a_newer_show_counts_for_nothing(self):
        self.show()
        relay.mail_gate_ask()
        self.show(draft="r1234567")  # the coordinator showed another mail before the tap
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        self.assertNotIn("vote", gs.load(self.path, {}))
        self.assertIn("not waiting for this vote", self.texts[-1])
        self.assertEqual(self.added, [])

    def test_a_vote_from_another_chat_never_counts(self):
        self.show()
        relay.mail_gate_ask()
        relay.on_vote("v1", "123@s.whatsapp.net", "123", "P1", relay.MAIL_SEND)
        self.assertNotIn("vote", gs.load(self.path, {}))

    def test_a_show_during_the_ask_voids_that_poll(self):
        self.show()
        def show_meanwhile(f, root=None):
            self.show(draft="r1234567")
            return True
        with mock.patch.object(relay, "send_owner_file", show_meanwhile):
            relay.mail_gate_ask()
        p = gs.load(self.path, {})
        self.assertEqual((p["draft"], p["status"], p.get("poll")), ("r1234567", "requested", None))

    def test_a_file_that_did_not_reach_the_owner_gets_no_poll(self):  # review P1-1
        self.show(files=["/d/a.pdf", "/d/b.pdf"])
        with mock.patch.object(relay, "send_owner_file", lambda f, root=None: f.endswith("a.pdf")):
            relay.mail_gate_ask()
        self.assertEqual(self.polls_sent, [])
        self.assertIn("a file did not reach you", self.texts[-1])
        self.assertEqual(gs.load(self.path, {})["status"], "failed")

    def test_the_real_file_sender_says_if_it_failed(self):  # review P1-1: True only when the bridge took it
        root = os.path.realpath(os.path.expanduser(relay.CFG["owner"]["folder"]))
        os.makedirs(root, exist_ok=True)
        f = os.path.join(root, "letter.pdf")
        with open(f, "wb") as h:
            h.write(b"%PDF x")
        with mock.patch.object(relay, "api", lambda path, body: {}):
            self.assertTrue(ORIG_SEND_OWNER_FILE(f))
        with mock.patch.object(relay, "api", side_effect=OSError("upload failed")):
            self.assertFalse(ORIG_SEND_OWNER_FILE(f))
        other = os.path.realpath(self.enterContext(tempfile.TemporaryDirectory()))
        with open(os.path.join(other, "x.pdf"), "wb") as h:
            h.write(b"%PDF x")
        with mock.patch.object(relay, "api", lambda path, body: {}):
            self.assertFalse(ORIG_SEND_OWNER_FILE(os.path.join(other, "x.pdf")))  # outside the owner's folder
            self.assertTrue(ORIG_SEND_OWNER_FILE(os.path.join(other, "x.pdf"), root=other))  # the send files root

    def test_a_send_shows_the_drafts_own_files_from_the_state_folder(self):  # review P2-5
        self.show(kind="send", files=["/s/mail-send/files/h/0/letter.pdf"])
        roots = []
        with mock.patch.object(relay, "send_owner_file", lambda f, root=None: roots.append(root) or True):
            relay.mail_gate_ask()
        self.assertEqual(roots, [relay.st("mail-send/files")])  # the literal: review #148 N1 (MAIL_FILES was redefined)
        roots.clear()
        self.show()
        with mock.patch.object(relay, "send_owner_file", lambda f, root=None: roots.append(root) or True):
            relay.mail_gate_ask()
        self.assertEqual(roots, [None])  # an attach: only the owner's folder

    def test_a_cut_ask_is_told_once_after_a_restart(self):  # review P1-2, with a real process death
        self.show()
        pid = os.fork()
        if pid == 0:
            with mock.patch.object(relay, "send_text", lambda *a, **k: os._exit(9)):
                relay.mail_gate_ask()
            os._exit(0)
        _, code = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(code), 9)
        self.assertEqual(gs.load(self.path, {})["status"], "asking")
        relay.mail_gate_sweep()
        relay.mail_gate_ask()
        relay.mail_gate_sweep()
        self.assertEqual(self.polls_sent, [])
        self.assertEqual(self.texts, ["(a mail approval was cut by a restart: nothing will be sent. Ask me to show it again.)"])
        self.assertIn("not asked yet", gs.vote_ok(gs.load(self.path, {})))

    def test_a_resumed_tap_still_reaches_the_coordinator(self):  # review P1-4
        self.show()
        relay.mail_gate_ask()
        rec = relay.polls()["P1"]
        self.assertTrue(relay.mail_gate_vote("P1", relay.MAIL_SEND))  # written, then the relay died
        relay.vote_job("v1", relay.OWNER, relay.OWNER, rec, relay.MAIL_SEND, "", None, None)
        self.assertTrue(self.added)
        self.assertEqual(gs.vote_ok(gs.load(self.path, {})), "")

    def test_the_same_send_tap_again_changes_nothing(self):  # review P1-3: only a change away from Send retracts
        self.show()
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        self.assertEqual(gs.vote_ok(gs.load(self.path, {})), "")
        self.assertNotIn("Changed: not sent.", self.texts)
        self.assertFalse(any("vote is closed" in t for t in self.texts))  # review #148 N4

    def test_a_change_on_an_old_poll_never_touches_the_new_mail(self):  # review P1-3
        self.show()
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        self.show(draft="r1234567")
        relay.mail_gate_ask()  # P2 for the new mail
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", relay.MAIL_NO)
        self.assertNotIn("vote", gs.load(self.path, {}))
        self.assertEqual(self.texts[-1], "That poll is closed: a newer mail replaced it. Your tap changes nothing.")

    def test_a_change_on_an_old_poll_says_the_old_mail_went(self):  # review #148 N3
        self.show(kind="send")
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        os.makedirs(os.path.dirname(relay.st("mail-send/sent.log")), exist_ok=True)
        with open(relay.st("mail-send/sent.log"), "a") as f:
            f.write(json.dumps({"poll": "P1", "message": "m1"}) + "\n")
        self.show(draft="r1234567")
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", relay.MAIL_NO)
        self.assertEqual(self.texts[-1], "That poll is closed: that mail was already sent; that cannot be taken back.")

    def test_a_late_change_on_an_attach_never_says_sent(self):  # review #148 N2
        self.show()
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        gs.save(self.path, dict(gs.load(self.path, {}), used=True))
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", relay.MAIL_NO)
        self.assertEqual(self.texts[-1], "Too late to change: the files were already being put on the draft. Nothing was sent.")

    def test_send_again_on_a_replaced_poll_is_silent(self):  # review #148 N4
        self.show()
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", relay.MAIL_NO)
        self.show(draft="r1234567")
        n = len(self.texts)
        relay.on_vote("v3", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        self.assertEqual(len(self.texts), n)

    def test_the_start_up_sweeps_before_it_resumes(self):  # review P1-2: the sweep must run at all
        import inspect
        src = inspect.getsource(relay.main_loop)
        self.assertLess(src.index("mail_gate_sweep()"), src.index("resume_jobs()"))

    def test_an_untick_takes_the_send_back(self):  # review P1-3: "(none)" counts as a change too
        self.show()
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", "(none)")
        self.assertEqual(gs.load(self.path, {})["vote"]["choice"], gs.NO)

    def test_a_change_after_the_mail_went_is_too_late(self):  # review P1-3
        self.show(kind="send")
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        gs.save(self.path, dict(gs.load(self.path, {}), used=True))
        relay.on_vote("v2", relay.OWNER, relay.OWNER, "P1", relay.MAIL_NO)
        self.assertEqual(self.texts[-1], "Too late to change: that mail was already being sent.")
        self.assertEqual(gs.load(self.path, {})["vote"]["choice"], gs.SEND)

    def test_an_old_show_gets_no_poll(self):  # review P2-7
        self.show()
        gs.save(self.path, dict(gs.load(self.path, {}), time=time.time() - 3 * 3600))
        relay.mail_gate_ask()
        self.assertEqual(self.polls_sent, [])
        self.assertIn("waited too long", self.texts[-1])
        self.assertEqual(relay.MAIL_OK_HOURS, gs.OK_HOURS)

    def test_a_resumed_dont_send_reaches_the_coordinator_not_the_retract(self):  # short round M2
        self.show()
        relay.mail_gate_ask()
        relay.polls({"P1": dict(relay.polls()["P1"], done=True)})  # done on disk, then the relay died
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_NO, resumed=True)
        self.assertEqual([a[1] for a in self.added], [f'ANSWER to "{relay.polls()["P1"]["question"]}": {relay.MAIL_NO}'])
        self.assertNotIn("Changed: not sent.", self.texts)

    def test_a_change_from_another_chat_never_retracts(self):  # short round M3
        self.show()
        relay.mail_gate_ask()
        relay.on_vote("v1", relay.OWNER, relay.OWNER, "P1", relay.MAIL_SEND)
        n = len(self.texts)
        relay.on_vote("v2", "123@g.us", "123", "P1", relay.MAIL_NO)
        self.assertEqual(gs.load(self.path, {})["vote"]["choice"], gs.SEND)
        self.assertEqual(len(self.texts), n)

    def test_a_refused_file_names_the_folder_really_checked(self):  # short round P2-4
        other = os.path.realpath(self.enterContext(tempfile.TemporaryDirectory()))
        self.assertFalse(ORIG_SEND_OWNER_FILE("nope.pdf", root=other))
        self.assertEqual(self.texts[-1], f"(file not sent: nope.pdf is not a file in {other})")

    def test_the_coordinator_is_told_how(self):
        for want in ("gmail_send.py show", "gmail_draft.py show", "A typed 'ok' never sends",
                     "A thread run never runs gmail_send.py or gmail_draft.py"):
            self.assertIn(want, relay.MAIL_RULES)


if __name__ == "__main__":
    unittest.main()


class PreviewNewOnly(unittest.TestCase):
    """Daniel 9 Nov 2026 17:15 (#180): the owner sees only what this mail adds. A quote or a file is hidden
    only when the thread proves it old; anything not proven is shown. The hash still covers the whole mail."""
    OLD_HTML = '<div dir="auto">Hi Daniel,<br><br>The price stays <a href="x">€650</a>.<br>Warm regards,<br>Joyce</div>'
    PHOTO = b"\xff\xd8 joyce photo"

    def old(self):
        from email.message import EmailMessage
        m = EmailMessage()
        m.set_content("Hi Daniel,\nThe price stays €650.\nWarm regards,\nJoyce")
        m.add_alternative(self.OLD_HTML, subtype="html")
        m.add_attachment(self.PHOTO, maintype="image", subtype="jpeg", filename="photo.jpg")
        return {"text": "\0".join(gs.bare(gs.strip_html(p.get_content())) for p in m.walk()
                                  if p.get_content_type() in ("text/plain", "text/html")),
                "files": {gs.hashlib.sha256(self.PHOTO).hexdigest()}}

    def reply(self, quote_extra="", files=()):
        from email.message import EmailMessage
        m = EmailMessage()
        m["To"], m["Subject"] = "joyce@example.nl", "Re: violin"
        m.set_content("Hi Joyce,\nPlease send the contract.\n\nOn Mon, Nov 09, 2026 04:35 PM, Joyce <joyce@example.nl> wrote:\n"
                      "> Hi Daniel,\n> The price stays €650.\n" + (f"> {quote_extra}\n" if quote_extra else "") +
                      "> Warm regards,\n> Joyce\n")
        m.add_alternative('<div dir="auto">Hi Joyce,<br><br>Please send the contract.</div><div class="gmail_extra">'
                          'On Mon, Nov 09, 2026 04:35 PM, Joyce &lt;joyce@example.nl&gt; wrote:<br>'
                          f'<blockquote class="gmail_quote">{self.OLD_HTML}'
                          + (f"<div>{quote_extra}</div>" if quote_extra else "") + '</blockquote></div>', subtype="html")
        for name, data in files:
            m.add_attachment(data, maintype="image", subtype="jpeg", filename=name)
        return gs.email.message_from_bytes(m.as_bytes(), policy=gs.email.policy.default)

    def test_only_the_new_text_and_one_line_for_the_quote(self):
        out = gs.describe(self.reply(), self.old())
        self.assertIn("Hi Joyce,\nPlease send the contract.", out)
        self.assertNotIn("price stays", out)
        self.assertNotIn("HTML version", out)
        self.assertIn("(under it, the earlier mails are quoted: On Mon, Nov 09, 2026 04:35 PM, Joyce", out)

    def test_a_quote_with_a_line_from_no_earlier_mail_is_shown_whole(self):
        out = gs.describe(self.reply("Please pay to NL00BANK0123456789 instead."), self.old())
        self.assertIn("NL00BANK0123456789", out)
        self.assertIn("price stays", out)
        self.assertNotIn("earlier mails are quoted", out)

    def test_a_thread_that_cannot_be_read_hides_nothing(self):
        out = gs.describe(self.reply(), None)
        self.assertIn("price stays", out)
        self.assertNotIn("earlier mails are quoted", out)

    def test_an_html_quote_not_proven_shows_the_html_whole(self):
        msg = self.reply()
        html = msg.get_body(preferencelist=("html",))
        html.set_content(html.get_content().replace("</blockquote>", "<div>Pay NL00BANK0123456789.</div></blockquote>"),
                         subtype="html")
        out = gs.describe(msg, self.old())
        self.assertIn("(HTML version, what most mail apps show:)", out)
        self.assertIn("NL00BANK0123456789", out)

    def test_a_file_from_an_earlier_mail_is_named_but_not_sent_to_the_owner(self):
        msg = self.reply(files=[("photo.jpg", self.PHOTO), ("contract.jpg", b"\xff\xd8 new")])
        out = gs.describe(msg, self.old())
        self.assertIn("Attachments: contract.jpg\n", out)
        self.assertIn("from an earlier mail of this thread, not sent to you again: photo.jpg", out)
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.assertEqual([os.path.basename(p) for p in gs.save_files(msg, folder, self.old())], ["contract.jpg"])
        self.assertEqual(len(gs.save_files(msg, self.enterContext(tempfile.TemporaryDirectory()))), 2)  # unproven: all

    def test_a_same_name_with_other_bytes_is_new(self):
        out = gs.describe(self.reply(files=[("photo.jpg", b"other bytes")]), self.old())
        self.assertIn("Attachments: photo.jpg", out)

    def test_gmail_wraps_a_plain_attribution_over_two_lines(self):
        text, quoted = gs.split_quote("Hi Joyce,\n\nOn Mon, Nov 9, 2026 at 4:35 PM Joyce <\njoyce@example.nl> wrote:\n"
                                      "> Hi Daniel,\n> The price stays €650.\n", False, self.old())
        self.assertEqual(text, "Hi Joyce,")
        self.assertIn("Joyce < joyce@example.nl> wrote:", quoted)

    def test_the_vote_still_covers_the_hidden_quote(self):
        t = SendGate("test_no_vote_means_no_send")
        t.setUp()
        self.addCleanup(t.doCleanups)
        t.body = "Hi\r\n\r\nOn Mon, Joyce <j@example.nl> wrote:\r\n> old text"
        with mock.patch.object(gs, "thread_old", return_value={"text": "oldtext", "files": set()}):
            out = t.run_cmd("show")
        self.assertNotIn("old text", t.slot()["summary"])
        self.assertIn("earlier mails are quoted", out)
        t.relay_asks()
        t.owner_votes(gs.SEND)
        t.body = "Hi\r\n\r\nOn Mon, Joyce <j@example.nl> wrote:\r\n> old text, now pay me"
        self.assertIn("draft changed", t.run_cmd("send"))
        self.assertEqual(t.sent, [])
