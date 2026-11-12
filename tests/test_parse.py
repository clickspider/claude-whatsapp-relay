import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay


class ParseTests(unittest.TestCase):
    def test_owner_note_never_reaches_contact(self):
        relay.CFG = {"prompts": {"owner_note_marker": "DANIEL"}}
        for marker in ("DANIEL", "OWNER"):
            body, marks = relay.parse(f"Hi Lara\n\n@@{marker}: private note\n@@EN: gist")
            self.assertEqual(body, "Hi Lara")
            self.assertEqual(marks["NOTE"], ["private note"])
    def test_multi_line_owner_note_never_reaches_contact(self):
        # 27 Oct 2026: a numbered @@DANIEL note leaked lines 2+ to mom in English
        relay.CFG = {"prompts": {"owner_note_marker": "DANIEL"}}
        body, marks = relay.parse("Понимаю ♥️\n\n@@DANIEL: She is upset:\n1) light broke\n2) boiler\n@@EN: I understand.\nSecond line.")
        self.assertEqual(body, "Понимаю ♥️")
        self.assertEqual(marks["NOTE"], ["She is upset:\n1) light broke\n2) boiler"])
        self.assertEqual(marks["EN"], "I understand.\nSecond line.")

    def test_gif_and_sticker_markers_are_stripped(self):
        relay.CFG = {"prompts": {"owner_note_marker": "OWNER"}}
        body, marks = relay.parse("Haha\n@@GIF: happy dance\n@@STICKER: cute dog")
        self.assertEqual(body, "Haha")
        self.assertEqual((marks["GIF"], marks["STICKER"]), ("happy dance", "cute dog"))


    def test_mention_becomes_a_real_tag(self):
        relay.CFG = {"prompts": {"owner_note_marker": "OWNER"}}
        body, marks = relay.parse("Lara, can you check the DJ?\n@@MENTION: Lara")
        real_db, relay.db = relay.db, lambda name: (_ for _ in ()).throw(OSError())  # no bridge DB: all keys count as phones
        self.addCleanup(setattr, relay, "db", real_db)
        a = {"members": {"31600000001": "Lara", "31600000002": "Daniel"}}
        self.assertEqual(relay.add_mentions(a, body, marks["MENTION"]),
                         ("@31600000001, can you check the DJ?", ["31600000001"]))
        self.assertEqual(relay.add_mentions(a, "Hi both", ["Daniel", "Nobody"]), ("@31600000002 Hi both", ["31600000002"]))

    def test_image_marker_sends_only_images_in_home(self):
        import tempfile
        relay.CFG = {"prompts": {"owner_note_marker": "OWNER"}}
        body, marks = relay.parse("A or B?\n@@IMAGE: media/out/bed.jpg | NEIDEN https://x")
        self.assertEqual((body, marks["IMAGE"]), ("A or B?", ["media/out/bed.jpg | NEIDEN https://x"]))
        home, out = tempfile.mkdtemp(), tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "media", "out"))
        open(os.path.join(home, "media", "out", "bed.jpg"), "w").close()
        sent, texts = [], []
        relay.CFG = {"bridge": {"outbox": out}}
        for name, fake in (("api", lambda kind, payload: sent.append(payload)),
                           ("send_text", lambda chat, text, **kw: texts.append(text)),
                           ("log_thread", lambda *a, **kw: None)):
            self.addCleanup(setattr, relay, name, getattr(relay, name))
            setattr(relay, name, fake)
        a = {"name": "mom", "home": home}
        relay.send_agent_image(a, "mom@s", marks["IMAGE"][0])
        relay.send_agent_image(a, "mom@s", "../../etc/passwd")
        self.assertEqual([(p["recipient"], p["message"]) for p in sent], [("mom@s", "NEIDEN https://x")])
        self.assertEqual(len(texts), 1)  # the bad path: a note to the owner, nothing to mom

    def test_file_marker_sends_only_pdfs_in_home(self):
        import tempfile
        relay.CFG = {"prompts": {"owner_note_marker": "OWNER"}}
        body, marks = relay.parse("Here it is\n@@FILE: cv/cv.pdf")
        self.assertEqual((body, marks["FILE"]), ("Here it is", ["cv/cv.pdf"]))
        home, out = tempfile.mkdtemp(), tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "cv"))
        for f in ("cv.pdf", "notes.md"):
            open(os.path.join(home, "cv", f), "w").close()
        sent, texts = [], []
        relay.CFG = {"bridge": {"outbox": out}, "prompts": {"owner_note_marker": "OWNER"}, "owner": {"name": "O"},
                     "hooks": {"after_log": []}, "threads": {"enabled": False}, "ui": {"polls": False}}
        for name, fake in (("api", lambda kind, payload: sent.append(payload)),
                           ("send_text", lambda chat, text, **kw: texts.append((chat, text))),
                           ("log_thread", lambda *a, **kw: None), ("pick_react", lambda *a: None)):
            self.addCleanup(setattr, relay, name, getattr(relay, name))
            setattr(relay, name, fake)
        a = {"name": "ran", "home": home, "trust": "reply", "checkins": False, "files": True, "images": False,
             "mentions": False, "gifs": False, "ask_contact": False}
        relay.deliver("ran@s", a, "Here it is\n@@FILE: cv/cv.pdf\n@@FILE: cv/notes.md\n@@FILE: ../../../etc/passwd", False)
        self.assertEqual([os.path.basename(p["media_path"]) for p in sent], ["cv.pdf"])
        self.assertEqual(sent[0]["recipient"], "ran@s")
        self.assertEqual(texts[-1], ("ran@s", "Here it is"))
        self.assertEqual(len(texts), 3)  # 2 refused paths: notes to the owner
        sent.clear()
        relay.deliver("x@s", dict(a, files=False), "Hi\n@@FILE: cv/cv.pdf", False)  # off = nothing goes out
        self.assertEqual(sent, [])

    def test_video_marker_sends_only_small_videos_in_home(self):
        # #203: a contact agent sends a video from its home
        import tempfile
        home, out = tempfile.mkdtemp(), tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "media", "out"))
        for f, size in (("wall.mp4", 10), ("big.mov", relay.VIDEO_MAX_MB * 1024 * 1024 + 1), ("bed.jpg", 10)):
            with open(os.path.join(home, "media", "out", f), "wb") as fh:
                fh.truncate(size)
        sent, texts = [], []
        relay.CFG = {"bridge": {"outbox": out}, "prompts": {"owner_note_marker": "OWNER"}, "owner": {"name": "O"},
                     "hooks": {"after_log": []}, "threads": {"enabled": False}, "ui": {"polls": False}}
        for name, fake in (("api", lambda kind, payload: sent.append(payload)),
                           ("send_text", lambda chat, text, **kw: texts.append((chat, text))),
                           ("log_thread", lambda *a, **kw: None), ("pick_react", lambda *a: None)):
            self.addCleanup(setattr, relay, name, getattr(relay, name))
            setattr(relay, name, fake)
        a = {"name": "carl", "home": home, "trust": "reply", "checkins": False, "videos": True, "images": False,
             "files": False, "mentions": False, "gifs": False, "ask_contact": False}
        out_text = ("הנה הסרטון\n@@VIDEO: media/out/wall.mp4 | הקיר\n@@VIDEO: media/out/big.mov\n"
                    "@@VIDEO: media/out/bed.jpg\n@@VIDEO: ../../../etc/passwd")
        relay.deliver("carl@s", a, out_text, False)
        self.assertEqual([(os.path.basename(p["media_path"]), p["message"]) for p in sent], [("wall.mp4", "הקיר")])
        self.assertEqual(texts[-1], ("carl@s", "הנה הסרטון"))
        self.assertEqual(len(texts), 4)  # 3 refused (too big, not a video, outside home): notes to the owner
        receipts = []  # review F2: a video makes the receipt "?" so a restart never sends it twice
        self.addCleanup(setattr, relay, "sending_text", relay.sending_text)
        relay.sending_text = lambda refs, text: receipts.append(text)
        relay.deliver("carl@s", a, "@@VIDEO: media/out/wall.mp4", False)
        self.assertEqual(receipts, ["?", ""])
        receipts.clear()
        relay.deliver("carl@s", a, "Hi\n@@VIDEO: media/out/wall.mp4", False)
        self.assertEqual(receipts, ["?", "?"])
        sent.clear()
        relay.deliver("x@s", dict(a, videos=False), "Hi\n@@VIDEO: media/out/wall.mp4", False)  # off = nothing goes out
        relay.deliver("x@s", dict(a, trust="draft", dir=tempfile.mkdtemp()), "Hi\n@@VIDEO: media/out/wall.mp4", False)  # held draft: no video
        self.assertEqual(sent, [])
        self.assertIn("videos", relay.CONTACT_BOOL)  # @@CONFIG / @@CONTACT may set it
        self.assertFalse(relay.AGENT_DEFAULTS["videos"])


if __name__ == "__main__":
    unittest.main()
