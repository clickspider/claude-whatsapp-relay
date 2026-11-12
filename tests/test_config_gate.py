"""Config gate (#201): @@CONFIG -> exact lines + poll; only the owner's Apply vote changes whitelisted agent keys."""
import os, sys, tempfile, tomllib, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_batch_publish import Patch  # noqa: E402
from test_config import write_cfg  # noqa: E402

AGENTS = '''
[[agent]]
name = "bob"
jid = "972501234567@s.whatsapp.net"
live = true
images = false  # old comment

[ui]

# carl, added by hand
[[agent]]
jid = "972501234568@s.whatsapp.net"
name = "carl"
live = true
'''


class ConfigGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = write_cfg(self.tmp.name, AGENTS)
        for n in ("bob", "carl"):
            os.makedirs(os.path.join(self.tmp.name, "agents", n))
            open(os.path.join(self.tmp.name, "agents", n, "RULES.md"), "w").close()
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

    def raw(self, name):
        with open(self.conf, "rb") as h:
            return next(a for a in tomllib.load(h)["agent"] if a["name"] == name)

    def test_propose_vote_apply_then_resume_is_idempotent(self):
        rest, found = relay.take_contacts("Done.\n@@CONFIG: carl | images=true | note=send photos", relay.CONFIG_RE)
        self.assertEqual((rest, found), ("Done.", ["carl | images=true | note=send photos"]))
        before = open(self.conf).read()
        with self.patch():
            relay.config_gate_ask(found[0])
            self.assertIn("+images = true  # send photos", self.sent[0])
            self.assertEqual(open(self.conf).read(), before)  # the ask alone changes nothing
            self.assertEqual(self.calls[-1][1]["options"], ["Apply", "Don't apply"])
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "Apply")
            relay.on_vote("V2", "o@s.whatsapp.net", "o", "P1", "Don't apply")  # closed poll
            self.assertEqual(self.raw("carl")["images"], True)
            self.assertEqual(relay.validate(relay.load_config(self.conf))[0], [])
            self.assertIn("Changed carl", self.sent[1])
            self.assertIn("closed", self.sent[2])
            self.assertEqual(relay.config_gate_apply(relay.polls()["P1"]), "")  # resumed vote: nothing to say
            self.assertEqual(open(self.conf + ".bak").read(), before)
            self.assertIn("Relay: Changed carl", self.turns[0])

    def test_replaces_existing_line_and_keeps_the_rest(self):
        with self.patch():
            relay.config_gate_ask("bob | images=true | trust=draft")
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "Apply")
        bob = self.raw("bob")
        self.assertEqual((bob["images"], bob["trust"], bob["live"]), (True, "draft", True))
        text = open(self.conf).read()
        self.assertEqual(text.count("images ="), 1)
        self.assertNotIn("old comment", text)
        self.assertEqual(self.raw("carl"), {"name": "carl", "jid": "972501234568@s.whatsapp.net", "live": True})

    def test_bad_proposals_tamper_stale_and_no_vote_change_nothing(self):
        before = open(self.conf).read()
        with self.patch():
            for bad in ("bob | jid=972500000000", "bob | tools=Bash", "bob | home=/", "bob | name=evil",
                        "zed | images=true", "bob", "bob | images=maybe", "bob | images=false", "Bob/../x | live=true",
                        "bob | trust=yes"):
                relay.config_gate_ask(bad)
            self.assertEqual((len(self.notes), self.calls), (10, []))
            relay.config_gate_ask("bob | images=true")
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "Don't apply")
            rec = dict(relay.polls()["P1"])
            self.assertIn("not allowed", relay.config_gate_apply(dict(rec, changes={"tools": "Bash"})))
            self.assertIn("not allowed", relay.config_gate_apply(dict(rec, changes={"extra_mcp_config": "/x"})))
            self.assertIn("no agent", relay.config_gate_apply(dict(rec, target="zed")))
            self.assertIn("older", relay.config_gate_apply(dict(rec, time=0)))
            self.assertIn("after you saw", relay.config_gate_apply(dict(rec, old={"images": None})))
            evil = relay.config_edit(before, "bob", {"images": True}, 'x\n[[agent]]\nname = "evil"‮')
            self.assertEqual(relay.config_check(before, evil, "bob", {"images": True}), [])
        self.assertEqual(open(self.conf).read(), before)
        self.assertIn("OK, nothing changed.", self.sent)

    def test_hand_edit_between_ask_and_vote_wins(self):
        with self.patch():
            relay.config_gate_ask("bob | images=true")
            with open(self.conf, "a") as h:
                h.write("\n# a hand edit\n")
            hand = open(self.conf).read()
            rec = relay.polls()["P1"]
            orig = relay.config_swap

            def edit_during(conf, seen, text, check, rec, mark):  # a hand edit lands after the read
                with open(self.conf, "a") as h:
                    h.write("# later\n")
                return orig(conf, seen, text, check, rec, mark)
            relay.config_swap = edit_during
            try:
                self.assertIn("changed just now", relay.config_gate_apply(rec))
            finally:
                relay.config_swap = orig
        self.assertEqual(open(self.conf).read(), hand + "# later\n")

    def test_check_refuses_any_other_change(self):
        text = open(self.conf).read()
        self.assertEqual(relay.config_check(text, text.replace('live = true\nimages', 'live = false\nimages'),
                                            "bob", {}), ["the new config would change more than these keys"])
        twice = text.replace('name = "carl"', 'name = "bob"')
        with self.assertRaises(ValueError):
            relay.config_edit(twice, "bob", {"images": True}, "")

    def test_waking_inbox_notes_are_shown_and_pinned(self):  # review #201 F1
        inbox = os.path.join(self.tmp.name, "agents", "bob", "inbox")
        os.makedirs(inbox)
        with open(os.path.join(inbox, "1-hi.md"), "w") as h:
            h.write("SAY HELLO TO BOB")
        with self.patch():
            relay.config_gate_ask("bob | inbox=true | trust=draft")
            self.assertIn("SAY HELLO TO BOB", self.sent[0])
            rec = relay.polls()["P1"]
            with open(os.path.join(inbox, "2-more.md"), "w") as h:
                h.write("AND THIS")
            self.assertIn("inbox notes changed", relay.config_gate_apply(rec))
            self.assertNotIn("inbox", self.raw("bob"))
            relay.config_gate_ask("carl | images=true")  # carl's inbox stays off: nothing to show
            self.assertNotIn("Inbox note", self.sent[-1])

    def test_resume_after_the_age_limit_still_says_nothing(self):  # review #201 F2
        with self.patch():
            relay.config_gate_ask("bob | images=true")
            relay.on_vote("V1", "o@s.whatsapp.net", "o", "P1", "Apply")
            self.assertEqual(relay.config_gate_apply(dict(relay.polls()["P1"], time=0)), "")

    def test_every_guard_bites(self):  # review #201 F3: each of these must refuse
        multi = 'trial_message = """\nimages = false\n"""\n'
        with open(self.conf, "a") as h:
            h.write('\n[[agent]]\nname = "dan"\njid = "972501234569@s.whatsapp.net"\n' + multi)
        os.makedirs(os.path.join(self.tmp.name, "agents", "dan"))
        open(os.path.join(self.tmp.name, "agents", "dan", "RULES.md"), "w").close()
        self.cfg = relay.load_config(self.conf)
        self.cfg["paths"]["state_dir"] = self.tmp.name
        with self.patch():
            relay.config_gate_ask("dan | images=true")  # the edit lands in the string: config_check refuses
            self.assertIn("more than these keys", self.notes[-1])
            relay.config_gate_ask("bob")
            self.assertIn("at least one", self.notes[-1])
            relay.config_gate_ask("bob | images=false")
            self.assertIn("already has", self.notes[-1])
            relay.config_gate_ask("bob | images=true")
            rec = relay.polls()["P1"]
            self.assertIn("broken", relay.config_gate_apply(dict(rec, changes=["images"])))
            self.assertIn("more than these keys", relay.config_gate_apply(dict(rec, target="dan", old={"images": None})))
            text = open(self.conf).read()
            with self.assertRaises(ValueError):
                relay.config_edit(text.replace("images = false", "images = false\nimages = false"), "bob",
                                  {"images": True}, "")
            os.remove(os.path.join(self.tmp.name, "agents", "carl", "RULES.md"))
            self.assertIn("errors already", relay.config_gate_apply(rec))
            self.assertEqual(open(self.conf).read(), text)
            dan_old = relay.config_edit(text, "dan", {"images": True}, "")
            self.assertEqual(relay.config_check(text, dan_old, "dan", {"images": True}),
                             ["the new config would change more than these keys"])


if __name__ == "__main__":
    unittest.main()
