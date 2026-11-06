"""6 Nov 2026: a saved relay.py with a missing import passed py_compile, the relay crash-looped for 35 minutes
and nobody was told. The update now checks that the new code loads, and a watchdog outside the relay tells
the owner when it is down. Run: python3 -m unittest discover tests"""
import importlib.util, json, os, shutil, sys, tempfile, unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402

spec = importlib.util.spec_from_file_location("relay_watchdog", os.path.join(ROOT, "scripts", "relay_watchdog.py"))
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class CrashGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = relay.load_config(write_cfg(self.tmp.name, "[threads]\nenabled = true\n"))
        os.makedirs(cfg["paths"]["state_dir"], exist_ok=True)
        self.old = dict(relay.CFG), relay.OWNER
        relay.CFG.clear(), relay.CFG.update(cfg)
        relay.OWNER = cfg["owner"]["jid"]
        self.sent, self.notes = [], []
        self.p = [mock.patch.object(relay, "send_text", lambda c, t, quote=None, mentions=None: self.sent.append(t)),
                  mock.patch.object(relay, "agent_note", lambda n, t, dry=False: self.notes.append((n, t)))]
        for p in self.p:
            p.start()
        relay._sent.clear()
        # the 09:42 change: a name used at the top level without its import
        self.broken = os.path.join(self.tmp.name, "relay.py")
        shutil.copy(relay.__file__, self.broken)
        with open(self.broken, "a") as f:
            f.write("\n_stop_n = collections.Counter()\n")

    def tearDown(self):
        for p in self.p:
            p.stop()
        relay.CFG.clear(), relay.CFG.update(self.old[0])
        relay.OWNER = self.old[1]
        relay._sent.clear()
        self.tmp.cleanup()

    def test_load_check(self):
        self.assertEqual(relay.load_check(relay.__file__, relay.CFG["config_path"]), "")
        self.assertIn("NameError: name 'collections' is not defined", relay.load_check(self.broken))

    def test_update_that_does_not_load_keeps_the_old_code(self):
        with mock.patch.object(relay, "__file__", self.broken), mock.patch.object(relay, "START_MTIME", 0), \
                mock.patch.object(relay.os, "_exit", side_effect=AssertionError("restarted on broken code")):
            relay.restart_if_changed()
            relay.restart_if_changed()  # reported once, not every 3 ticks
        self.assertEqual(len(self.notes), 1)
        self.assertIn("did NOT restart", self.notes[0][1])
        self.assertIn("NameError", self.notes[0][1])

    def test_watchdog_demo(self):
        watchdog.demo()

    def test_watchdog_settings_from_the_relay_config(self):
        s = watchdog.settings(relay.CFG["config_path"])
        self.assertEqual(s["state"], relay.CFG["paths"]["state_dir"])
        self.assertEqual(s["owner"], relay.OWNER)
        self.assertEqual(s["minutes"], relay.DEFAULTS["watchdog"]["down_minutes"])
        self.assertEqual(s["text"], relay.DEFAULTS["watchdog"]["down_text"])

    def test_relay_back_after_watchdog_alert(self):
        relay.heartbeat()
        self.assertTrue(os.path.exists(relay.st("heartbeat")))
        down = {"since": 1_700_000_000, "at": 1_700_000_300, "id": "MID9", "text": "down since 09:42"}
        relay.write(relay.st("watchdog-down.json"), json.dumps(down))
        relay.watchdog_back()
        self.assertFalse(os.path.exists(relay.st("watchdog-down.json")))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("back", self.sent[0])
        # the watchdog's own message, read from the backlog after the restart, is never an owner message
        self.assertTrue(relay.is_own_send("OTHER", relay.OWNER, "down since 09:42"))  # by text
        self.assertTrue(relay.is_own_send("MID9", relay.OWNER, "edited"))  # by id
        self.assertFalse(relay.is_own_send("OWNER1", relay.OWNER, "hi"))
        relay.watchdog_back()
        self.assertEqual(len(self.sent), 1)  # nothing to say without the file


if __name__ == "__main__":
    unittest.main()


class WatchdogNight(unittest.TestCase):
    def test_watchdog_night_overrides_the_owner_night(self):  # 6 Nov 2026: the owner wants the alarm at night too
        d = tempfile.mkdtemp()
        try:
            conf = os.path.join(d, "c.toml")
            with open(conf, "w") as f:
                f.write("[owner]\nnight = [23, 8]\n[watchdog]\nnight = []\n")
            self.assertEqual(watchdog.settings(conf)["night"], [])
            with open(conf, "w") as f:
                f.write("[owner]\nnight = [23, 8]\n")
            self.assertEqual(watchdog.settings(conf)["night"], [23, 8])
        finally:
            shutil.rmtree(d)

