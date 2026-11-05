import os, sqlite3, tempfile, unittest
from unittest import mock

import relay


class AutoQuote(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        c = sqlite3.connect(os.path.join(self.d, "messages.db"))
        c.execute("CREATE TABLE messages (id TEXT, chat_jid TEXT, timestamp TIMESTAMP, media_type TEXT)")
        c.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", [
            ("a", "o", "2026-11-02 12:00:00", None), ("b", "o", "2026-11-02 12:00:05", "reaction"),
            ("c", "o", "2026-11-02 12:01:00", None), ("x", "g", "2026-11-02 12:02:00", None)])
        c.commit(), c.close()
        cfg = dict(relay.CFG or relay.DEFAULTS)
        cfg.update(bridge={"store": self.d}, ui={**relay.DEFAULTS["ui"], "auto_quote": True})
        self.p = mock.patch.object(relay, "CFG", cfg)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_newer_in_chat(self):
        self.assertTrue(relay.newer_in_chat("o", "a"))   # c came after it
        self.assertFalse(relay.newer_in_chat("o", "c"))  # newest; other chat does not count
        self.assertFalse(relay.newer_in_chat("o", "zz"))

    def test_pick(self):
        r1, r2 = ("a", "o", "first"), ("c", "o", "second")
        self.assertEqual(relay.pick_quote("o", "#1", {"#1": r1, "#2": r2}), r1)
        self.assertEqual(relay.pick_quote("o", "", {"#1": r1, "#2": r2}), r2)       # bare @@QUOTE
        self.assertEqual(relay.pick_quote("o", None, {"#1": r1}), r1)              # newer came in
        self.assertIsNone(relay.pick_quote("o", None, {"#1": r2}))                 # nothing newer
        self.assertEqual(relay.pick_quote("o", None, {"#1": r1, "#2": r2}, auto_last=True), r2)
        self.assertIsNone(relay.pick_quote("o", None, None))
        relay.CFG["ui"]["auto_quote"] = False
        self.assertIsNone(relay.pick_quote("o", None, {"#1": r1}))
        self.assertEqual(relay.pick_quote("o", "#1", {"#1": r1}), r1)


if __name__ == "__main__":
    unittest.main()
