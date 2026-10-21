import datetime, os, sys, unittest
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay

TZ = "Europe/Amsterdam"


class RemindTests(unittest.TestCase):
    def test_parse_marker_lines(self):
        relay.CFG = {"prompts": {"owner_note_marker": "OWNER"}}
        body, marks = relay.parse("Done\n@@REMIND: 2026-10-22 10:00 | DJ call\n@@REMIND: 2026-10-23 09:30 | pay venue")
        self.assertEqual(body, "Done")
        self.assertEqual(marks["REMIND"], ["2026-10-22 10:00 | DJ call", "2026-10-23 09:30 | pay venue"])

    def test_bad_lines_rejected(self):
        for bad in ("2026-10-22 10:00", "tomorrow | x", "2026-10-22 10:00 |  ", "22-10-2026 10:00 | x"):
            self.assertIsNone(relay.parse_reminder(bad, TZ), bad)
        due, note = relay.parse_reminder("2026-10-22 10:00 | DJ call", TZ)
        self.assertEqual((due.hour, due.tzinfo.key, note), (10, TZ, "DJ call"))

    def test_due_logic(self):
        lines = ["2026-10-22 10:00 | a", "2026-10-22 10:01 | b", "2026-10-01 08:00 | very late", "junk"]
        now = datetime.datetime(2026, 10, 22, 10, 0, tzinfo=ZoneInfo(TZ))
        due, keep = relay.split_due(lines, now, TZ)
        self.assertEqual(due, ["2026-10-22 10:00 | a", "2026-10-01 08:00 | very late"])  # late ones fire once
        self.assertEqual(keep, ["2026-10-22 10:01 | b"])  # junk dropped
        # same instant seen from Israel (11:00 there) is still due: times are absolute
        self.assertEqual(relay.split_due(lines[:1], now.astimezone(ZoneInfo("Asia/Jerusalem")), TZ)[0], lines[:1])

    def test_self_per_day_cap(self):
        ok, d = relay.self_start_ok, "2026-10-22"
        self.assertTrue(ok(0, ["2026-10-22 09:00"] * 5, "", d))  # 0 = no cap
        self.assertTrue(ok(1, [], "", d))
        self.assertTrue(ok(1, ["2026-10-21 18:00"], "", d))  # yesterday does not count
        self.assertFalse(ok(1, ["2026-10-22 09:00"], "2026-10-22 08:59", d))  # wrote before it
        self.assertTrue(ok(1, ["2026-10-22 09:00"], "2026-10-22 11:30", d))  # wrote after: open


if __name__ == "__main__":
    unittest.main()
