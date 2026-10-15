"""Fresh pairing: history sync, unknown chats, rate-limit notes, bot number == owner number."""
import os, sqlite3, sys, tempfile, time, unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_batch_publish import Patch  # noqa: E402

OWNER = "31600000001@s.whatsapp.net"


class HistorySync(unittest.TestCase):
    def test_old_rows_are_dropped_at_the_source(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,"
                  " is_from_me BOOLEAN, media_type TEXT, filename TEXT)")
        c.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", [
            ("old", "1@g.us", "x", "from last year", "2025-01-01 10:00:00.123456789+02:00", 0, "", ""),
            ("new", OWNER, "o", "hi", "2026-10-15 12:00:05+00:00", 0, "", ""),
            ("bad", OWNER, "o", "odd stamp", "not a time", 0, "", ""),  # unreadable: kept
            ("mine", OWNER, "o", "own", "2026-10-15 12:00:06+00:00", 1, "", ""),
        ])
        cutoff = relay.msg_epoch("2026-10-15 12:00:00+00:00")
        rows, last = relay.new_rows(c, 0, cutoff)
        self.assertEqual([r[1] for r in rows], ["new", "bad"])
        self.assertEqual(last, 3)  # the old row is consumed, never seen again
        self.assertEqual(len(relay.new_rows(c, 0, 0)[0]), 3)  # 0 = filter off


class UnknownChats(unittest.TestCase):
    def setUp(self):
        self.sent, self.queued = [], []
        self.cfg = {"limits": {"per_minute": 1, "per_day": 0}, "agents": []}
        relay._minute.clear(), relay._day.clear(), relay._limit_noted.clear()

    def run_route(self, chats, now):
        t = [now]
        with Patch(CFG=self.cfg, OWNER=OWNER, agents=lambda cfg: [],
                   send_text=lambda c, text, quote=None: self.sent.append((c, text)),
                   owner_add=lambda *a: self.queued.append(a),
                   time=SimpleNamespace(time=lambda: t[0], strftime=time.strftime, localtime=time.localtime)):
            for chat in chats:
                relay.route("m", chat, "s", "hi", "")

    def test_unknown_group_is_silent_and_not_counted(self):
        self.run_route(["9@g.us"] * 5, 1000)
        self.assertEqual(self.sent, [])
        self.assertNotIn("9@g.us", relay._minute)

    def test_owner_limit_note_once_per_window(self):
        self.run_route([OWNER] * 4, 1000)
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(len(self.sent), 1)  # one note, not one per message
        self.run_route([OWNER, OWNER, OWNER], 1100)  # new window: allowed, then limited again
        self.assertEqual(len(self.queued), 2)
        self.assertEqual(len(self.sent), 2)


class OwnNumber(unittest.TestCase):
    def store(self, tmp, device_jid):
        c = sqlite3.connect(os.path.join(tmp, "whatsapp.db"))
        c.execute("CREATE TABLE whatsmeow_device (jid TEXT, lid TEXT, registration_id INTEGER)")
        c.execute("INSERT INTO whatsmeow_device VALUES (?, ?, 1)", (device_jid, "99999@lid"))
        c.commit(), c.close()

    def test_device_equal_to_owner_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"bridge": {"store": tmp}, "owner": {"jid": "31634133574@s.whatsapp.net"}}
            with Patch(CFG=cfg):
                self.assertFalse(relay.bot_is_owner())  # no store yet
                self.store(tmp, "31634133574:73@s.whatsapp.net")
                self.assertTrue(relay.bot_is_owner())
                cfg["owner"]["jid"] = "31611111111@s.whatsapp.net"
                self.assertFalse(relay.bot_is_owner())

    def test_check_shows_info_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, "15551234567:5@s.whatsapp.net")
            from test_config import write_cfg
            relay.CFG = relay.load_config(write_cfg(tmp))
            with Patch(token=lambda: ""):
                errs, notes = relay.check_deps(relay.CFG)
            self.assertIn(relay.OWN_NOTE, notes)
            self.assertFalse(any("own number" in e for e in errs))

    MSG = ("CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,"
           " is_from_me BOOLEAN, media_type TEXT, filename TEXT)")
    TS = "2026-10-15 12:00:05+00:00"

    def own_mode(self, tmp, bridge_row=None):
        """Own-number mode with a fake bridge: /api/send stores the row (like the real bridge) and returns no ID."""
        db = os.path.join(tmp, "messages.db")

        def urlopen(req, timeout=None):
            if bridge_row:
                c = sqlite3.connect(db)
                c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", bridge_row)
                c.commit(), c.close()
            return SimpleNamespace(read=lambda: b'{"success": true}')
        return Patch(OWNER=OWNER, OWN=[True], _sent={}, token=lambda: "t",
                     CFG={"bridge": {"api": "x", "store": tmp}, "paths": {"state_dir": tmp}},
                     urllib=SimpleNamespace(request=SimpleNamespace(Request=lambda *a: None, urlopen=urlopen)))

    def rows(self, tmp, rows):
        c = sqlite3.connect(os.path.join(tmp, "messages.db"))
        c.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", rows)
        c.commit()
        return c

    def test_typed_runs_reply_never_and_restart_keeps_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = sqlite3.connect(os.path.join(tmp, "messages.db"))
            c.execute(self.MSG), c.commit(), c.close()
            with self.own_mode(tmp, ("R1", OWNER, "o", "answer from Claude", self.TS, 1, "", "")):
                relay.send_text(OWNER, "answer from Claude")
            self.assertIn("R1", open(os.path.join(tmp, "sent.json")).read())  # the ID, persisted
            with self.own_mode(tmp):  # a restart: empty memory, loaded from state/sent.json
                c = self.rows(tmp, [
                    ("T1", OWNER, "o", "hi", self.TS, 1, "", ""),                  # owner types in Message yourself
                    ("O1", "4@s.whatsapp.net", "o", "hey", self.TS, 1, "", ""),     # owner typing to someone else
                    ("I1", "4@s.whatsapp.net", "4", "yo", self.TS, 0, "", ""),
                    ("T2", OWNER, "o", "answer from Claude", self.TS, 1, "", ""),  # owner repeats the text later
                ])
                got = [r[1] for r in relay.new_rows(c, 0)[0]]
            self.assertEqual(got, ["T1", "I1", "T2"])  # R1 skipped by ID; T2 not, its text entry was used up
            with Patch(OWNER=OWNER, OWN=[False]):  # second-number mode: own rows never count
                self.assertEqual([r[1] for r in relay.new_rows(c, 0)[0]], ["I1"])

    def test_text_guard_covers_a_reply_whose_id_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.own_mode(tmp):  # bridge store not readable: no ID, only the text guard
                relay.send_text(OWNER, "done")
                c = sqlite3.connect(":memory:")
                c.execute(self.MSG)
                c.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", [
                    ("X", OWNER, "o", "done", self.TS, 1, "", ""), ("Y", OWNER, "o", "done", self.TS, 1, "", "")])
                self.assertEqual([r[1] for r in relay.new_rows(c, 0)[0]], ["Y"])


class AgentHistory(unittest.TestCase):
    def test_bounded_history_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = sqlite3.connect(os.path.join(tmp, "messages.db"))
            c.execute("CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT,"
                      " timestamp TIMESTAMP, is_from_me BOOLEAN, media_type TEXT)")
            c.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                          [(str(i), "g@g.us", "5", "x" * 900 if i == 9 else f"m{i}",
                            f"2020-01-01 10:00:{i:02d}+00:00", 0, "") for i in range(10)]
                          + [("o", "other@g.us", "5", "secret", "2020-01-01 11:00:00+00:00", 0, "")])
            c.commit(), c.close()
            a = {"jid": "g@g.us", "home": tmp, "members": {"5": "Ann"}}
            with Patch(CFG={"bridge": {"store": tmp}, "limits": {"history_messages": 3}}):
                text = open(relay.write_history(a)).read()
            self.assertIn("Last 3 messages", text)
            self.assertIn("Ann: m7", text)
            self.assertNotIn("m6", text)
            self.assertNotIn("secret", text)
            self.assertIn("x" * 500, text)
            self.assertNotIn("x" * 501, text)
            with Patch(CFG={"limits": {"history_messages": 0}}):
                self.assertIsNone(relay.write_history(a))


if __name__ == "__main__":
    unittest.main()
