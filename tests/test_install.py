"""install.sh's two embedded Python helpers: the config it writes must load, and must agree with itself."""
import os, re, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ_CONFIG, WRITE_CONFIG = re.findall(r"<<'EOF'\n(.*?)\nEOF\n", open(os.path.join(ROOT, "install.sh")).read(), re.S)


class InstallTests(unittest.TestCase):
    def test_written_config_loads_and_matches_bridge_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "relay.config.toml")
            store = os.path.expanduser("~/.claude-whatsapp-relay/whatsapp-mcp/whatsapp-bridge/store")
            subprocess.run([sys.executable, "-c", WRITE_CONFIG, os.path.join(ROOT, "relay.config.example.toml"),
                            cfg_path, "31612345678@s.whatsapp.net", 'Sam "Q"', tmp, "Europe/Amsterdam",
                            store, sys.executable], check=True)
            self.assertEqual(os.stat(cfg_path).st_mode & 0o777, 0o600)
            cfg = relay.load_config(cfg_path)
            self.assertEqual(relay.validate(cfg)[0], [])
            self.assertEqual(cfg["owner"]["name"], 'Sam "Q"')
            self.assertEqual(cfg["launchd"]["label"], "com.claude-whatsapp-relay.relay")
            out = subprocess.run([sys.executable, "-c", READ_CONFIG, cfg_path],
                                 check=True, capture_output=True, text=True).stdout
            s = dict(line.split("=", 1) for line in out.splitlines())
            self.assertEqual(s["STORE"], os.path.join(s["WA_DIR"], "whatsapp-bridge", "store"))
            self.assertEqual((s["PORT"], s["HOST"], s["MANAGE_BRIDGE"]), ("8080", "127.0.0.1", "true"))
            # never overwrites an existing config
            r = subprocess.run([sys.executable, "-c", WRITE_CONFIG, os.path.join(ROOT, "relay.config.example.toml"),
                                cfg_path, "x", "x", "x", "x", "x", "x"], capture_output=True)
            self.assertNotEqual(r.returncode, 0)

    def test_example_matches_installer_defaults(self):
        out = subprocess.run([sys.executable, "-c", READ_CONFIG, os.path.join(ROOT, "relay.config.example.toml")],
                             check=True, capture_output=True, text=True).stdout
        s = dict(line.split("=", 1) for line in out.splitlines())
        self.assertEqual(s["STORE"], os.path.join(s["WA_DIR"], "whatsapp-bridge", "store"))


if __name__ == "__main__":
    unittest.main()
