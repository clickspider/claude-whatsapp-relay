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


if __name__ == "__main__":
    unittest.main()
