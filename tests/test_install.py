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
                            store, sys.executable, "me.example.lore.relay", "http://127.0.0.1:18080/api",
                            "me.example.lore.bridge"], check=True)
            self.assertEqual(os.stat(cfg_path).st_mode & 0o777, 0o600)
            cfg = relay.load_config(cfg_path)
            self.assertEqual(relay.validate(cfg)[0], [])
            self.assertEqual(cfg["owner"]["name"], 'Sam "Q"')
            self.assertEqual(cfg["launchd"]["label"], "me.example.lore.relay")
            self.assertEqual(cfg["install"]["bridge_label"], "me.example.lore.bridge")
            out = subprocess.run([sys.executable, "-c", READ_CONFIG, cfg_path],
                                 check=True, capture_output=True, text=True).stdout
            s = dict(line.split("=", 1) for line in out.splitlines())
            self.assertEqual(s["STORE"], os.path.join(s["WA_DIR"], "whatsapp-bridge", "store"))
            self.assertEqual((s["PORT"], s["HOST"], s["MANAGE_BRIDGE"]), ("18080", "127.0.0.1", "true"))
            # never overwrites an existing config
            r = subprocess.run([sys.executable, "-c", WRITE_CONFIG, os.path.join(ROOT, "relay.config.example.toml"),
                                cfg_path, "x", "x", "x", "x", "x", "x", "x", "x", "x"], capture_output=True)
            self.assertNotEqual(r.returncode, 0)

    def test_example_matches_installer_defaults(self):
        out = subprocess.run([sys.executable, "-c", READ_CONFIG, os.path.join(ROOT, "relay.config.example.toml")],
                             check=True, capture_output=True, text=True).stdout
        s = dict(line.split("=", 1) for line in out.splitlines())
        self.assertEqual(s["STORE"], os.path.join(s["WA_DIR"], "whatsapp-bridge", "store"))

    def test_yes_dry_run_never_asks_and_reports_steps(self):
        """--yes --dry-run with no terminal at all (new session, stdin closed): every step prints a LORE-STEP line."""
        if sys.platform != "darwin":
            self.skipTest("install.sh is macOS only")
        with tempfile.TemporaryDirectory() as tmp:
            stubs = os.path.join(tmp, "bin")
            os.mkdir(stubs)
            for name, body in {"brew": "exit 0", "go": "echo go version go1.22 darwin/arm64", "ffmpeg": "exit 0",
                               "whisper-cli": "exit 0", "claude": "echo 2.0.0 Claude Code"}.items():
                with open(os.path.join(stubs, name), "w") as f:
                    f.write("#!/bin/sh\n" + body + "\n")
                os.chmod(os.path.join(stubs, name), 0o755)
            env = {"HOME": tmp, "PATH": f"{stubs}:{os.path.dirname(sys.executable)}:/usr/bin:/bin:/usr/sbin",
                   "RELAY_NUMBER": "31612345678", "RELAY_LABEL": "me.example.lore.relay"}
            run = lambda *a: subprocess.run(["bash", os.path.join(ROOT, "install.sh"), "--dry-run", "--yes", *a], env=env,
                                            stdin=subprocess.DEVNULL, capture_output=True, text=True, start_new_session=True)
            r = run()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            steps = [line.split()[1:3] for line in r.stdout.splitlines() if line.startswith("LORE-STEP ")]
            for want in (["tools", "ok"], ["config", "ok"], ["qr", "wait"], ["bridge", "ok"], ["check", "ok"], ["done", "ok"]):
                self.assertIn(want, steps)
            self.assertEqual(os.listdir(tmp), ["bin"])   # dry run: nothing written
            r = run("--tools-only")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("LORE-STEP done ok", r.stdout)
            self.assertNotIn("2/6", r.stdout)
            # a bad number stops with a LORE-STEP fail line instead of asking again
            env["RELAY_NUMBER"] = "12"
            r = subprocess.run(["bash", os.path.join(ROOT, "install.sh"), "--yes"], env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, start_new_session=True, cwd=tmp)
            self.assertEqual(r.returncode, 1)
            self.assertIn("LORE-STEP fail RELAY_NUMBER", r.stdout)


if __name__ == "__main__":
    unittest.main()
