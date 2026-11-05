"""An agent's report to the owner's side never reaches the chat (Yakov got 9 on 2-5 Nov 2026, mom 2 on 27 Oct)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay

# Real messages that went out, as the agent wrote them (no @@ marker in front of the report).
YAKOV = ("I sent Yakov the sofa update as one message, with no delivery date. The note for Sali "
         "(floor 2, no elevator) is still waiting to go next.\n\n"
         "אחי, הספה הוזמנה ושולמה 🎉 דורי הום, כולל משלוח והרכבה בשמואל הנביא.\n"
         "אמא בחופש מהעבודה משלישי 10.11 עד סוף אותו שבוע.")
YAKOV_MATH = ("The math checks out: 2,950 + 2,235.50 = 5,185.50, so a third is 1,728.50. I sent Yakov the split.\n\n"
              "אחי, סגרנו את החשבון 🙏")
MOM = ("Понимаю, это очень неприятно ♥️\nНа крышу лезть не нужно.\n\n"
       "1) Last evening at the old flat the light didn't work.\n"
       "I calmed her, told her not to climb or fix anything herself.")


class ReportLeak(unittest.TestCase):
    def setUp(self):
        self.sent, self.notes = [], []
        self.old = {k: getattr(relay, k) for k in
                    ("CFG", "send_text", "log_thread", "agent_note", "pick_react", "pick_quote", "apply_fixes")}
        relay.CFG = {"prompts": {"owner_note_marker": "DANIEL"}, "owner": {"name": "Daniel"}, "ui": {"polls": False}}
        relay.send_text = lambda chat, text, **kw: self.sent.append(text)
        relay.log_thread = lambda *a, **kw: None
        relay.agent_note = lambda name, note, dry=False: self.notes.append(note)
        relay.pick_react = lambda *a: None
        relay.pick_quote = lambda *a, **kw: None
        relay.apply_fixes = lambda chat, text: text

    def tearDown(self):
        for k, v in self.old.items():
            setattr(relay, k, v)

    def send(self, out):
        a = {"name": "x", "trust": "reply", "checkins": False, "ask_contact": False, "mentions": False, "gifs": False}
        relay.deliver("jid", a, out, False, allow_hold=False)
        return self.sent[-1] if self.sent else ""

    def test_hebrew_and_russian_reports_stay_private(self):
        for out, keep in ((YAKOV, "אחי, הספה"), (YAKOV_MATH, "אחי, סגרנו"), (MOM, "Понимаю")):
            self.sent, self.notes = [], []
            text = self.send(out)
            self.assertTrue(text.startswith(keep), text)
            self.assertNotRegex(text, r"[A-Za-z]{3,}")  # no English sentence left in the chat
            self.assertEqual(len(self.notes), 1)  # the report goes to the owner's side instead

    def test_normal_messages_are_untouched(self):
        for out in ("Hi Lara, I've told Daniel 👍\n\nAnything else?",              # English chat: all of it
                    "אחי, הלינק לספה:\n\nhttps://www.dorihome.co.il/product/eliza-new",  # a link is no report
                    "אמא בחרה \"Eliza NEW\" בצבע קרם"):                             # names inside Hebrew
            self.sent, self.notes = [], []
            self.assertEqual(self.send(out), out)
            self.assertEqual(self.notes, [])

    def test_inbox_prompt_says_the_reply_is_sent(self):
        self.assertIn("@@OWNER", relay.INBOX_REPLY_RULE)


if __name__ == "__main__":
    unittest.main()
