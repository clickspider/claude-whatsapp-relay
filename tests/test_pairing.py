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


class SameNumber(unittest.TestCase):
    def store(self, tmp, device_jid):
        c = sqlite3.connect(os.path.join(tmp, "whatsapp.db"))
        c.execute("CREATE TABLE whatsmeow_device (jid TEXT, lid TEXT, registration_id INTEGER)")
        c.execute("INSERT INTO whatsmeow_device VALUES (?, ?, 1)", (device_jid, "99999@lid"))
        c.commit(), c.close()

    def test_device_equal_to_owner_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"bridge": {"store": tmp}, "owner": {"jid": "31634133574@s.whatsapp.net"}}
            with Patch(CFG=cfg):
                self.assertFalse(relay.bot_is_owner())  # no store yet: not an error here
                self.store(tmp, "31634133574:73@s.whatsapp.net")
                self.assertTrue(relay.bot_is_owner())
                cfg["owner"]["jid"] = "31611111111@s.whatsapp.net"
                self.assertFalse(relay.bot_is_owner())

    def test_check_and_run_report_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.store(tmp, "15551234567:5@s.whatsapp.net")
            from test_config import write_cfg
            path = write_cfg(tmp)
            relay.CFG = relay.load_config(path)
            with Patch(token=lambda: ""):
                errs, _ = relay.check_deps(relay.CFG)
            self.assertIn(relay.SAME_NUMBER, errs)
            with Patch(validate=lambda cfg: ([], []), main_loop=lambda: self.fail("must not start")):
                self.assertEqual(relay.main(["run", "--config", path]), 1)


if __name__ == "__main__":
    unittest.main()
