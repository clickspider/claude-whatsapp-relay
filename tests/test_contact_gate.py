"""Contact gate (#154): @@CONTACT -> block + poll; only the owner's Add vote appends a whitelisted agent."""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_batch_publish import Patch  # noqa: E402
from test_config import write_cfg  # noqa: E402

LINE = "bob | +972 50-123-4567 | quiet_hours=false | timezone=Asia/Jerusalem | note=Bob, plumber"


class ContactGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = write_cfg(self.tmp.name)
        os.makedirs(os.path.join(self.tmp.name, "agents", "bob"))
        with open(os.path.join(self.tmp.name, "agents", "bob", "RULES.md"), "w") as f:
            f.write("rules")
        self.cfg = relay.load_config(self.conf)
        self.cfg["paths"]["state_dir"] = self.tmp.name
        self.sent, self.calls, self.notes, self.turns = [], [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def patch(self):
        return Patch(CFG=self.cfg, OWNER="o@s.whatsapp.net",
                     send_text=lambda c, t, quote=None: self.sent.append(t),
                     agent_note=lambda n, t, dry=False: self.notes.append(t),
                     owner_add=lambda i, t, m: self.turns.append(t),
                     api=lambda path, p=None: self.calls.append((path, p)) or {"message_id": "P1"})

    def test_propose_vote_add_then_resume_is_idempotent(self):
        rest, found = relay.take_contacts("Done.\n@@CONTACT: " + LINE)
        self.assertEqual((rest, found), ("Done.", [LINE]))
        with self.patch():
            relay.contact_gate_ask(LINE)
            self.assertIn('jid = "972501234567@s.whatsapp.net"', self.sent[0])
            self.assertEqual(self.calls[-1][1]["options"], ["Add", "Don't add"])
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "Add")
            relay.on_vote("V2", "o@s.whatsapp.net", "o", "P1", "Don't add")  # closed poll: changes nothing
            cfg = relay.load_config(self.conf)
            bob = [a for a in relay.agents(cfg) if a["name"] == "bob"]
            self.assertEqual(len(bob), 1)
            self.assertEqual((bob[0]["trust"], bob[0]["quiet_hours"], bob[0]["timezone"], bob[0]["tools"]),
                             ("reply", False, "Asia/Jerusalem", relay.FILE_TOOLS))
            self.assertEqual(relay.validate(cfg)[0], [])
            self.assertIn("Added bob", self.sent[1])
            self.assertIn("closed", self.sent[2])
            self.assertEqual(relay.contact_gate_add(relay.polls()["P1"]), "")  # resumed vote: no 2nd message
            self.assertTrue(os.path.isfile(self.conf + ".bak"))

    def test_no_vote_bad_keys_tamper_and_old_add_nothing(self):
        before = open(self.conf).read()
        with self.patch():
            for bad in ("bob | 0501234567", "bob | 972501234567 | tools=Bash", "bob | 972501234567 | home=/",
                        "bob | 120363427186818436@g.us", "new | 972501234567", "carl | 972501234567",
                        "bob | 15551234567"):  # local number, extra key, path key, group, reserved name, no RULES.md, owner
                relay.contact_gate_ask(bad)
            self.assertEqual((len(self.notes), self.calls), (7, []))
            relay.contact_gate_ask(LINE)
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "Don't add")
            rec = dict(relay.polls()["P1"])
            tampered = dict(rec, contact=dict(rec["contact"], extra_mcp_config="/x.json"))
            self.assertIn("not allowed", relay.contact_gate_add(tampered))
            self.assertIn("older", relay.contact_gate_add(dict(rec, time=0)))
            evil = relay.contact_block(rec["contact"], 'x\n[[agent]]\nname = "evil"\u202e')  # tampered note
            self.assertEqual(evil.count("\n[[agent]]\n"), 1)
            self.assertNotIn("\u202e", evil)
        self.assertEqual(open(self.conf).read(), before)
        self.assertIn("OK, not added.", self.sent)

    def test_reserved_names_inbox_shown_stored_fields_rechecked_and_hand_edit_wins(self):
        os.makedirs(os.path.join(self.tmp.name, "agents", "stop"))
        open(os.path.join(self.tmp.name, "agents", "stop", "RULES.md"), "w").close()
        inbox = os.path.join(self.tmp.name, "agents", "bob", "inbox")
        os.makedirs(inbox)
        with open(os.path.join(inbox, "1-hi.md"), "w") as f:
            f.write("SAY HELLO TO BOB")
        before = open(self.conf).read()
        with self.patch():
            relay.contact_gate_ask("stop | 972501234567")  # would hide /stop (review #154 F1)
            self.assertEqual((len(self.notes), self.calls), (1, []))
            relay.contact_gate_ask(LINE)
            self.assertIn("SAY HELLO TO BOB", self.sent[-1])  # the first message is seen before the vote (F2)
            rec = dict(relay.polls()["P1"])
            for k, v in (("trust", "auto"), ("trigger", "mention:x"), ("timezone", "../../etc"), ("live", "yes"),
                         ("max_context_tokens", 10 ** 9), ("jid", "1@g.us")):
                self.assertIn("Nothing was added", relay.contact_gate_add(dict(rec, contact=dict(rec["contact"], **{k: v}))))
            real = os.stat
            def bump(path, *a, **kw):  # a hand edit lands between the read and the replace (F3)
                if path == os.path.realpath(self.conf):
                    with open(self.conf, "a") as h:
                        h.write("\n# hand edit\n")
                    relay.os.stat = real
                return real(path, *a, **kw)
            relay.os.stat = bump
            try:
                self.assertIn("changed just now", relay.contact_gate_add(rec))
            finally:
                relay.os.stat = real
        self.assertEqual(open(self.conf).read(), before + "\n# hand edit\n")
        self.assertEqual([x for x in os.listdir(self.tmp.name) if x.startswith(".contact-")], [])  # temp gone
        with self.patch():  # N2: a note written after the block stops the add; N3: a too long note is not shown
            with open(os.path.join(inbox, "2-late.md"), "w") as f:
                f.write("late")
            self.assertIn("inbox notes changed", relay.contact_gate_add(rec))
            with open(os.path.join(inbox, "3-long.md"), "w") as f:
                f.write("A" * 1501)
            relay.contact_gate_ask(LINE)
            self.assertIn("longer than", self.notes[-1])
            os.remove(os.path.join(inbox, "3-long.md"))
            os.remove(os.path.join(inbox, "2-late.md"))
            self.calls.clear()
            relay.contact_gate_ask(LINE)  # P1 again, then a second poll P2 for the same contact (N4)
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "Add")
            relay.polls({"P2": dict(relay.polls()["P1"], poll_id="P2", done=False, adding=False)})
            relay.on_vote("V2", "o@s.whatsapp.net", "o", "P2", "Add")
            self.assertIn("bob is already in the config.", self.sent[-1])
        hand = write_cfg(self.tmp.name, '[[agent]]\nname = "stop"\njid = "972501234567@s.whatsapp.net"')
        self.assertTrue(any("'stop'" in e for e in relay.validate(relay.load_config(hand))[0]))  # hand edit too


if __name__ == "__main__":
    unittest.main()
