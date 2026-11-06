"""Attach files to a Gmail draft, never send (6 Nov 2026, #139). Against a fake Gmail: nothing goes out.
Run: python3 -m unittest discover tests"""
import base64, email, email.policy, io, json, os, sys, tempfile, unittest
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

    def run_cmd(self, *files):
        with redirect_stdout(io.StringIO()) as out:
            try:
                gd.main(["attach", DRAFT, *files, "--config", self.conf])
            except SystemExit as e:
                return str(e)
        return out.getvalue()

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

    def test_a_save_that_lost_the_file_is_reported(self):
        self.drop_on_save = True
        self.assertIn("CHECK FAILED", self.run_cmd(self.pdf))


if __name__ == "__main__":
    unittest.main()
