"""owner.voice_file + per-agent use_owner_voice. Run: python3 -m unittest discover tests"""
import os, sys, tempfile, unittest, wave
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_config import write_cfg  # noqa: E402

AGENT = """
[[agent]]
name = "alex"
jid = "15550100002@s.whatsapp.net"
"""


def voice_arg(args):
    return next((args[i + 1] for i, x in enumerate(args) if x == "--append-system-prompt"
                 and "Write like the owner" in args[i + 1]), None)


class TranscribeTests(unittest.TestCase):
    """Fake 20 s note with one pause at 10 s. lang(span_seconds, index) gives the detected language."""
    def run_it(self, langs, he_model=True, fixes=None, prompt="", fail_detect=False):
        calls = []

        def fake_run(args, **kw):
            if args[0] == "ffmpeg" and "-af" in args:
                return SimpleNamespace(stderr="silence_end: 10.0 | silence_duration: 0.5\n", stdout="")
            if args[0] == "ffmpeg":
                with wave.open(args[-1], "wb") as w:
                    w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
                    w.writeframes(b"\0\0" * 16000 * 20)
                return None
            calls.append(args)
            if "-dl" in args:
                if fail_detect:
                    return SimpleNamespace(stdout="", stderr="")
                n = args.count("-f")
                answers = langs[:n]
                del langs[:n]
                return SimpleNamespace(stdout="", stderr="".join(
                    f"auto-detected language: {x} (p = 0.95)\n" for x in answers))
            return SimpleNamespace(stdout=f" {'HE' if args[args.index('-m') + 1] == he else 'MAIN'} VNs\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            he = os.path.join(tmp, "he.bin")
            open(he, "w").close()
            paths = {"ffmpeg": "ffmpeg", "whisper_cli": "whisper-cli", "whisper_model": "main.bin", "whisper_language": "auto",
                     "whisper_model_he": he if he_model else "", "whisper_prompt_he": prompt, "whisper_fixes": fixes or {}}
            with mock.patch.dict(relay.CFG, {"paths": paths}), mock.patch.object(relay.subprocess, "run", fake_run):
                text = relay.transcribe("in.ogg")
        return text, [c for c in calls if "-dl" not in c]

    def test_hebrew_note_runs_once_on_hebrew_model(self):
        text, runs = self.run_it(["he", "he"], prompt="Kubernetes")
        self.assertEqual(text, "HE VNs")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][runs[0].index("-l") + 1], "he")
        self.assertIn("Kubernetes", runs[0])
        self.assertIn("--carry-initial-prompt", runs[0])

    def test_other_language_stays_on_main_model(self):
        text, runs = self.run_it(["en", "en"], prompt="Kubernetes")
        self.assertEqual(text, "MAIN VNs")
        self.assertEqual(runs[0][runs[0].index("-l") + 1], "auto")
        self.assertNotIn("--prompt", runs[0])

    def test_mixed_note_keeps_both_languages_in_order(self):
        # first pass he, en; second pass (2 halves on each side) puts the switch at the pause
        text, runs = self.run_it(["he", "en", "he", "he", "en", "en"])
        self.assertEqual(text, "HE VNs MAIN VNs")
        self.assertEqual(len(runs), 2)

    def test_failed_detection_still_transcribes(self):
        text, runs = self.run_it([], fail_detect=True)
        self.assertEqual(text, "MAIN VNs")

    def test_no_hebrew_model_no_detection(self):
        text, runs = self.run_it(["he"], he_model=False)
        self.assertEqual(text, "MAIN VNs")
        self.assertEqual(len(runs), 1)

    def test_fix_whole_word_only(self):
        text, _ = self.run_it(["he", "he"], fixes={"VNs": "Kubernetes", "V": "X"})
        self.assertEqual(text, "HE Kubernetes")


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.own = relay.OWN[0]

    def tearDown(self):
        relay.OWN[0] = self.own

    def cfg(self, tmp, extra=AGENT, text="Short. Lowercase. Ends with 👍"):
        path = write_cfg(tmp, extra)
        if text is not None:
            with open(os.path.join(tmp, "dossier", "voice.md"), "w") as f:
                f.write(text)
        return relay.load_config(path)

    def test_voice_injected_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay.OWN[0] = True  # own number: the agent speaks as the owner -> default on
            cfg = self.cfg(tmp)
            self.assertIn("Ends with 👍", relay.owner_system(cfg))
            self.assertIn("Ends with 👍", voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))

    def test_not_injected_when_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay.OWN[0] = False  # second number: default off
            cfg = self.cfg(tmp)
            self.assertIsNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))
            relay.OWN[0] = True
            cfg = self.cfg(tmp, AGENT + "use_owner_voice = false\n")
            self.assertIsNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))
            cfg = self.cfg(tmp)
            cfg["owner"]["voice_file"] = ""  # owner.voice_file = "" turns it off everywhere
            self.assertNotIn("OWNER'S VOICE", relay.owner_system(cfg))
            self.assertIsNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))

    def test_explicit_on_with_second_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay.OWN[0] = False
            cfg = self.cfg(tmp, AGENT + "use_owner_voice = true\n")
            self.assertIsNotNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))

    def test_missing_file_no_crash_and_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay.OWN[0] = True
            cfg = self.cfg(tmp, text=None)
            self.assertNotIn("OWNER'S VOICE", relay.owner_system(cfg))
            self.assertIsNone(voice_arg(relay.agent_args(cfg, relay.agents(cfg)[0], "s", True)))
            cfg = self.cfg(tmp, text="x" * 20_000)
            self.assertEqual(len(relay.owner_voice(cfg)), relay.VOICE_MAX)


if __name__ == "__main__":
    unittest.main()
