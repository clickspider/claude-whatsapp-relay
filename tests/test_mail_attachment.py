"""Mail attachments (5 Nov 2026, #124): GET only, a thread id works too, names cleaned, files 600 in their own
folder, a .docx gets its text. Against a fake Gmail. Run: python3 -m unittest discover tests"""
import base64, io, os, sys, tempfile, unittest, urllib.error, zipfile
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mail_attachment as ma  # noqa: E402

b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")


def docx(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:p><w:t>{text}</w:t></w:p></w:document>")
    return buf.getvalue()


class AttachmentTest(unittest.TestCase):
    def test_thread_id_download(self):
        msg = {"id": "aaaabbbbccccdddd", "payload": {"parts": [
            {"mimeType": "text/plain", "filename": "", "body": {"data": b64(b"hi")}},
            {"mimeType": "application/pdf", "filename": "../../evil/Offer 1.pdf", "body": {"attachmentId": "A1"}},
            {"mimeType": "application/msword", "filename": "brief.docx", "body": {"data": b64(docx("Total EUR 860"))}},
        ]}}
        calls = []

        def get(tok, path):
            calls.append(path)
            if path.startswith("messages/aaaabbbbccccdddd/attachments/A1"):
                return {"data": b64(b"%PDF-1.4 fake")}
            if path.startswith("messages/"):
                raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
            return {"messages": [msg]}

        with tempfile.TemporaryDirectory() as root, mock.patch.object(ma, "access", return_value=("t", root)), \
                mock.patch.object(ma, "get", get), redirect_stdout(io.StringIO()) as out:
            ma.main(["1234567890abcdef"])
            folder = os.path.join(root, "aaaabbbbccccdddd")
            self.assertEqual(sorted(os.listdir(folder)), ["Offer 1.pdf", "brief.docx", "brief.docx.txt"])
            pdf = os.path.join(folder, "Offer 1.pdf")
            self.assertEqual(open(pdf, "rb").read(), b"%PDF-1.4 fake")
            self.assertEqual(os.stat(pdf).st_mode & 0o777, 0o600)
            self.assertIn("Total EUR 860", open(os.path.join(folder, "brief.docx.txt")).read())
        self.assertEqual(calls[1], "threads/1234567890abcdef?format=full")  # message 404 -> thread
        self.assertIn("UNTRUSTED", out.getvalue())

    def test_bad_id_refused(self):
        with self.assertRaises(SystemExit):
            ma.main(["../x"])


if __name__ == "__main__":
    unittest.main()
