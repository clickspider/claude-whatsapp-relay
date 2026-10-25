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


if __name__ == "__main__":
    unittest.main()
