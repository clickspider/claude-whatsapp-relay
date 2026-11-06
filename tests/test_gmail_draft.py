"""Attach files to a Gmail draft, never send (6 Nov 2026, #139). Against a fake Gmail: nothing goes out.
Run: python3 -m unittest discover tests"""
import base64, email, email.policy, io, json, os, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import gmail_draft as gd  # noqa: E402
import gmail_send as gs  # noqa: E402

DRAFT = "r-4486766997841415913"
MAIL = ("To: a@example.nl, b@example.nl\r\nSubject: Re: letter\r\nIn-Reply-To: <x@example.nl>\r\n"
        "References: <x@example.nl>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHi, see the file.\r\n")


def b64(data):
    return base64.urlsafe_b64encode(data).decode()


class Attach(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = os.path.join(self.tmp, "dossier")
        os.makedirs(self.root)
        self.pdf = os.path.join(self.root, "letter.pdf")
        with open(self.pdf, "wb") as f:
            f.write(b"%PDF-1.7 fake")
        self.conf = os.path.join(self.tmp, "relay.config.toml")
        self.write_conf(f'attach_dir = "{self.root}"\n')
        self.raw, self.puts, self.drop_on_save = MAIL.encode(), [], False

        def api(tok, path, body=None):
            self.assertIsNone(body)  # GET only: never drafts/send
            self.assertEqual(path, f"drafts/{DRAFT}?format=raw")
            return {"id": DRAFT, "message": {"raw": b64(self.raw), "threadId": "t1"}}

        def put(tok, did, body):
            self.puts.append(body)
            if not self.drop_on_save:
                self.raw = base64.urlsafe_b64decode(body["message"]["raw"])
            return {"id": did}
        for p in (mock.patch.object(gs, "access", return_value="tok"), mock.patch.object(gs, "api", api),
                  mock.patch.object(gd, "put", put)):
            p.start()
            self.addCleanup(p.stop)

    def write_conf(self, mail):
        with open(self.conf, "w") as f:
            f.write('[paths]\nstate_dir = "."\n[mail]\nuser = "me@gmail.com"\n' + mail)

    def cmd(self, name, *files):
        with redirect_stdout(io.StringIO()) as out:
            try:
                with mock.patch.object(gs, "CONF", self.conf):
                    gd.main([name, DRAFT, *files])
            except SystemExit as e:
                return str(e)
        return out.getvalue()

    def approve(self, choice=gs.SEND, poll="P1", later=1, asked=True):
        """What the relay writes: its poll went out (mail_gate_ask), then the owner's tap (mail_gate_vote)."""
        path = os.path.join(self.tmp, "mail-send", "pending.json")
        p = gs.load(path, {})
        if asked:
            p.update(status="asked", poll=poll, asked=time.time())
        if choice:
            p["vote"] = {"poll": poll, "choice": choice, "time": time.time() + later}
        gs.save(path, p)

    def run_cmd(self, *files):
        """The happy path: show, the owner's Send vote on the relay's poll, attach. A refusal at show stops there."""
        out = self.cmd("show", *files)
        if "Files to attach" not in out:
            return out
        self.approve()
        return self.cmd("attach", *files)

    def test_attach_keeps_the_mail_and_adds_the_file(self):
        out = self.run_cmd(self.pdf)
        self.assertIn("ATTACHED to draft", out)
        self.assertIn("Not sent", out)
        body = self.puts[0]
        self.assertEqual((body["id"], body["message"]["threadId"]), (DRAFT, "t1"))
        msg = email.message_from_bytes(self.raw, policy=email.policy.default)
        self.assertEqual((msg["To"], msg["Subject"], msg["In-Reply-To"]),
                         ("a@example.nl, b@example.nl", "Re: letter", "<x@example.nl>"))
        self.assertIn("Hi, see the file.", msg.get_body(preferencelist=("plain",)).get_content())
        part = next(p for p in msg.walk() if p.get_filename() == "letter.pdf")
        self.assertEqual((part.get_content_type(), part.get_content()), ("application/pdf", b"%PDF-1.7 fake"))

    def test_files_outside_the_folder_are_refused(self):
        outside = os.path.join(self.tmp, "secret.txt")
        with open(outside, "w") as f:
            f.write("x")
        link = os.path.join(self.root, "link.txt")
        os.symlink(outside, link)
        for p in (outside, link, os.path.join(self.root, "..", "secret.txt"), os.path.join(self.root, "nope.pdf"),
                  self.root):
            self.assertIn("NOT ATTACHED", self.run_cmd(p), p)
        self.assertIn("NOT ATTACHED", self.run_cmd(self.pdf, outside))  # one bad file = nothing attached
        self.assertEqual(self.puts, [])

    def test_hidden_folders_and_denied_files_are_refused(self):
        # 6 Nov 2026 (Daniel + coordinator): never .git or other hidden folders, never the files in attach_deny
        for rel in (".git/config", ".env", "a/.claude/settings.json", "money/pay-secret.txt", "money/x/pay-secret.txt"):
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("x")
        self.write_conf(f'attach_dir = "{self.root}"\nattach_deny = ["money/*secret*", "**/pay-secret.txt"]\n')
        for rel in (".git/config", ".env", "a/.claude/settings.json", "money/pay-secret.txt", "money/x/pay-secret.txt"):
            self.assertIn("NOT ATTACHED", self.run_cmd(os.path.join(self.root, rel)), rel)
        self.assertIn("ATTACHED to draft", self.run_cmd(self.pdf))  # a normal file still goes

    def test_a_passwords_file_is_refused_by_name(self):
        # the live attach_deny (6 Nov 2026): any file or folder whose name says password, in en/nl/he
        deny = '["*password*", "*Password*", "*PASSWORD*", "*wachtwoord*", "*סיסמ*"]'
        self.write_conf(f'attach_dir = "{self.root}"\nattach_deny = {deny}\n')
        for rel in ("money/payslip-password.md", "Passwords/list.txt", "nl/loonstrook-wachtwoord.txt", "he/סיסמאות.txt"):
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("x")
            self.assertIn("attach_deny", self.run_cmd(path), rel)
        self.assertEqual(self.puts, [])

    def test_a_password_file_is_refused_by_name_in_any_case(self):
        # Daniel 6 Nov 2026 11:52: never the passwords file. A Hebrew name and an upper-case copy, anywhere.
        for rel in ("cases/work/סיסמת_תלושים_והכנסה.md", "x/Payslip-PASSWORD.txt"):
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("x")
        self.write_conf(f'attach_dir = "{self.root}"\nattach_deny = ["*סיסמ*", "*password*"]\n')
        for rel in ("cases/work/סיסמת_תלושים_והכנסה.md", "x/Payslip-PASSWORD.txt"):
            self.assertIn("attach_deny", self.run_cmd(os.path.join(self.root, rel)), rel)
        self.assertEqual(self.puts, [])

    def test_no_attach_dir_means_nothing_attached(self):
        self.write_conf("")
        self.assertIn("attach_dir is not set", self.run_cmd(self.pdf))
        self.assertEqual(self.puts, [])

    def test_too_big_and_twice_are_refused(self):
        with mock.patch.object(gd, "MAX_MB", 0):
            self.assertIn("bigger than", self.run_cmd(self.pdf))
        self.run_cmd(self.pdf)
        self.assertIn("already has letter.pdf", self.run_cmd(self.pdf))
        self.assertEqual(len(self.puts), 1)

    def test_a_gmail_html_draft_becomes_mixed(self):
        self.raw = ("To: a@example.nl\r\nSubject: Re: letter\r\nMIME-Version: 1.0\r\n"
                    'Content-Type: multipart/alternative; boundary="b"\r\n\r\n--b\r\n'
                    "Content-Type: text/plain; charset=utf-8\r\n\r\nHi\r\n--b\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n\r\n<div>Hi</div>\r\n--b--\r\n").encode()
        self.assertIn("ATTACHED", self.run_cmd(self.pdf))
        msg = email.message_from_bytes(self.raw, policy=email.policy.default)
        self.assertEqual(msg.get_content_type(), "multipart/mixed")
        self.assertIn("<div>Hi</div>", msg.get_body(preferencelist=("html",)).get_content())

    # The gate (Daniel + Instinct 12:25, poll 14:27, 6 Nov 2026): the vote covers these recipients and files, BEFORE upload.
    def test_show_lists_recipients_and_files_and_uploads_nothing(self):
        out = self.cmd("show", self.pdf)
        for want in ("To: a@example.nl, b@example.nl", "letter.pdf", "Nothing is uploaded", "A typed 'ok' never"):
            self.assertIn(want, out)
        slot = gs.load(os.path.join(self.tmp, "mail-send", "pending.json"), {})
        self.assertEqual((slot["kind"], slot["status"], slot["files"]), ("attach", "requested", [os.path.realpath(self.pdf)]))
        self.assertIn("letter.pdf (13 bytes)", slot["summary"])  # what the relay shows the owner
        self.assertIn("To: a@example.nl, b@example.nl", slot["summary"])
        self.assertEqual(self.puts, [])

    def test_a_pdf_shows_its_pages(self):
        self.assertEqual(gd.pages(b"%PDF-1.7 1 0 obj <</Type /Pages>> 2 0 obj <</Type /Page>> 3 0 obj <</Type/Page>>"),
                         ", 2 pages")
        self.assertEqual(gd.pages(b"not a pdf /Type /Page"), "")

    def test_no_vote_means_no_upload(self):
        self.assertIn("not shown", self.cmd("attach", self.pdf))  # never shown
        self.cmd("show", self.pdf)
        self.assertIn("not asked yet", self.cmd("attach", self.pdf))
        with open(os.path.join(self.tmp, "owner-last.json"), "w") as f:  # a typed "ok" counts for nothing
            json.dump({"time": time.time() + 1, "text": "ok", "where": ""}, f)
        self.approve(choice=None)
        self.assertIn("has not voted", self.cmd("attach", self.pdf))
        self.approve(choice=gs.NO)
        self.assertIn("voted not to", self.cmd("attach", self.pdf))
        self.approve(later=3600)
        self.assertIn("in the future", self.cmd("attach", self.pdf))  # a time ahead of the clock never counts
        self.approve()
        later = time.time() + gd.gs.OK_HOURS * 3600 + 120
        with mock.patch.object(gd.time, "time", return_value=later), mock.patch.object(gs.time, "time", return_value=later):
            self.assertIn("too old", self.cmd("attach", self.pdf))  # the window ends 2 h after the show
        self.assertEqual(self.puts, [])

    def test_any_change_after_the_show_needs_a_new_show(self):
        other = os.path.join(self.root, "other.pdf")
        with open(other, "wb") as f:
            f.write(b"%PDF other")
        self.cmd("show", self.pdf)
        self.approve()
        self.assertIn("changed after the show", self.cmd("attach", other))  # another file
        self.assertIn("changed after the show", self.cmd("attach", self.pdf, other))  # one more file
        with open(self.pdf, "ab") as f:
            f.write(b" edited")
        self.assertIn("changed after the show", self.cmd("attach", self.pdf))  # same name, new content
        self.cmd("show", self.pdf)
        self.approve()
        self.raw = self.raw.replace(b"b@example.nl", b"stranger@example.com")
        self.assertIn("changed after the show", self.cmd("attach", self.pdf))  # another recipient
        self.assertEqual(self.puts, [])

    def test_a_draft_without_recipient_is_refused(self):
        self.raw = MAIL.replace("To: a@example.nl, b@example.nl\r\n", "").encode()
        self.assertIn("no recipient", self.cmd("show", self.pdf))

    def test_one_vote_uploads_once_and_never_unlocks_a_send(self):
        self.drop_on_save = True
        self.assertIn("CHECK FAILED", self.run_cmd(self.pdf))
        self.assertIn("one vote = one action", self.cmd("attach", self.pdf))
        self.assertEqual(len(self.puts), 1)
        with open(os.path.join(self.tmp, "mail-attach", "attach.log")) as f:
            line = json.loads(f.readline())
        self.assertEqual((line["draft"], line["to"], line["poll"]), (DRAFT, ["a@example.nl", "b@example.nl"], "P1"))
        with redirect_stdout(io.StringIO()):  # the send gate: its show replaces the slot, the attach vote is gone
            with mock.patch.object(gs, "CONF", self.conf):
                gs.main(["show", DRAFT])
                with self.assertRaises(SystemExit) as e:
                    gs.main(["send", DRAFT])
        self.assertIn("NOT SENT: the owner was not asked yet", str(e.exception))

    def test_one_vote_covers_only_the_newest_show_of_either_gate(self):
        # review #139 F3 repro E: attach show, then a send show, then a Send vote: it never uploads a file
        self.cmd("show", self.pdf)
        with redirect_stdout(io.StringIO()), mock.patch.object(gs, "CONF", self.conf):
            gs.main(["show", DRAFT])
        self.approve()
        self.assertIn("not shown to the owner first", self.cmd("attach", self.pdf))
        self.assertEqual(self.puts, [])

    def test_a_config_argument_is_never_read(self):
        # review #139 F1/N2: a caller-written config could point attach_dir at "/" and state at its own OK
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        with open(os.path.join(other, "relay.config.toml"), "w") as f:
            f.write('[paths]\nstate_dir = "."\n[mail]\nuser = "me@gmail.com"\nattach_dir = "/"\n')
        self.assertIn("NOT ATTACHED", self.cmd("show", self.pdf, "--config", os.path.join(other, "relay.config.toml")))
        with mock.patch.object(gs, "CONF", ""), self.assertRaises(SystemExit) as e:  # not started by the relay
            gd.main(["show", DRAFT, self.pdf])
        self.assertIn("RELAY_CONFIG is not set", str(e.exception))
        self.assertEqual(self.puts, [])

    def test_a_vote_on_another_poll_never_attaches(self):
        self.cmd("show", self.pdf)
        self.approve(poll="P2")
        self.approve(poll="P1", asked=False)  # the tap belongs to an older poll than the slot's
        self.assertIn("another poll", self.cmd("attach", self.pdf))
        self.assertEqual(self.puts, [])

    def test_a_name_without_address_is_no_recipient(self):
        self.raw = MAIL.replace("To: a@example.nl, b@example.nl", "To: Lara").encode()
        self.assertIn("no recipient", self.cmd("show", self.pdf))

    def test_the_uploaded_bytes_are_the_shown_bytes(self):
        # review #139 F5: the file changes between the hash and the upload
        real_open = open
        def change_after_read(path, *a, **k):
            f = real_open(path, *a, **k)
            if isinstance(path, str) and os.path.realpath(path) == os.path.realpath(self.pdf) and "b" in (a[0] if a else k.get("mode", "")):
                data = f.read()
                f.close()
                with real_open(self.pdf, "wb") as w:
                    w.write(b"%PDF swapped")
                return io.BytesIO(data)
            return f
        self.cmd("show", self.pdf)
        with open(self.pdf, "wb") as f:
            f.write(b"%PDF-1.7 fake")
        self.approve()
        with mock.patch("builtins.open", change_after_read):
            self.assertIn("ATTACHED", self.cmd("attach", self.pdf))
        msg = email.message_from_bytes(self.raw, policy=email.policy.default)
        part = next(p for p in msg.walk() if p.get_filename() == "letter.pdf")
        self.assertEqual(part.get_content(), b"%PDF-1.7 fake")

    def test_a_failed_read_back_is_logged_as_unknown(self):
        self.cmd("show", self.pdf)
        self.approve()
        with mock.patch.object(gs, "draft", side_effect=TimeoutError("timed out")):
            out = self.cmd("attach", self.pdf)
        self.assertIn("OUTCOME UNKNOWN", out)
        with open(os.path.join(self.tmp, "mail-attach", "attach.log")) as f:
            self.assertIn("OUTCOME UNKNOWN", json.loads(f.readline())["outcome"])

    def test_a_failed_upload_is_outcome_unknown(self):
        self.cmd("show", self.pdf)
        self.approve()
        with mock.patch.object(gd, "put", side_effect=TimeoutError("timed out")):
            out = self.cmd("attach", self.pdf)
        self.assertIn("OUTCOME UNKNOWN", out)
        self.assertNotIn("ATTACHED to", out)
        self.assertIn("one vote = one action", self.cmd("attach", self.pdf))

    def test_a_save_that_lost_the_file_is_reported(self):
        self.drop_on_save = True
        self.assertIn("CHECK FAILED", self.run_cmd(self.pdf))


if __name__ == "__main__":
    unittest.main()
