"""feedback.incoming_reactions: an emoji on a message reaches the agent. Run: python3 -m unittest discover tests"""
import json, copy, os, sqlite3, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402
from test_batch_publish import Patch  # noqa: E402
from test_context_emoji import write_cfg  # noqa: E402

OWNER, CONTACT, GROUP, MENTION = "o@s.whatsapp.net", "m@s.whatsapp.net", "g@g.us", "t@s.whatsapp.net"
AGENTS = {CONTACT: {"name": "sam", "jid": CONTACT, "trigger": "all"},
          GROUP: {"name": "family", "jid": GROUP, "trigger": "listen"},
          MENTION: {"name": "tagged", "jid": MENTION, "trigger": "mention:bot"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = sqlite3.connect(os.path.join(self.dir.name, "messages.db"))
        self.con.execute("CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TEXT, "
                         "is_from_me BOOLEAN, media_type TEXT, filename TEXT, quoted_message_id TEXT)")
        self.cfg = copy.deepcopy(relay.DEFAULTS)
        self.cfg["bridge"] = {"store": self.dir.name}

    def add(self, id, chat, sender, content, media_type=None, filename=None, me=0):
        self.con.execute("INSERT INTO messages VALUES (?,?,?,?,'2026-11-01 10:00:00+01:00',?,?,?,NULL)",
                         (id, chat, sender, content, me, media_type, filename))
        self.con.commit()
        return self.con.execute("SELECT MAX(rowid) FROM messages").fetchone()[0]

    def react(self, chat, target, emoji, sender="p", id="r1"):
        """Store a reaction row and run on_reaction: -> what reached route."""
        rowid = self.add(id, chat, sender, emoji, "reaction", target)
        routed = []
        with Patch(CFG=self.cfg, OWNER=OWNER, by_jid=AGENTS.get, route=lambda *x: routed.append(x)):
            relay.on_reaction(rowid, id, chat, sender, target, emoji)
        return routed


class WhichReactionsAct(Base):
    def test_owner_reaction_on_our_message_carries_the_text(self):
        self.add("t", OWNER, "bot", "shall I book it?", me=1)
        got = self.react(OWNER, "t", "✅")
        self.assertEqual(got, [("r1", OWNER, "p", 'REACTION ✅ to your message: "shall I book it?"', "reaction")])

    def test_any_emoji_counts(self):
        self.add("t", CONTACT, "bot", "hi", me=1)
        skin_tone = "\U0001F44D\U0001F3FD"
        family = "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466"
        kiss_tones = "\U0001F468\U0001F3FB\u200D\u2764\uFE0F\u200D\U0001F48B\u200D\U0001F468\U0001F3FF"
        england = "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"
        keycap = "1\uFE0F\u20E3"
        for i, e in enumerate(["🦄", "🇮🇱", "❤️", "💩", skin_tone, family, kiss_tones, england, keycap]):
            self.assertEqual(self.react(CONTACT, "t", e, id=f"r{i}")[0][3], f'REACTION {e} to your message: "hi"')

    def test_their_own_message_and_media_and_unknown(self):
        self.add("t", CONTACT, "p", "", media_type="image")
        self.assertEqual(self.react(CONTACT, "t", "😂")[0][3], 'REACTION 😂 to their own message: "(image)"')
        self.assertEqual(self.react(CONTACT, "gone", "😂", id="r2")[0][3], "REACTION 😂 to a message that is not on file")

    def test_removal_does_nothing(self):
        self.add("t", CONTACT, "bot", "hi", me=1)
        self.assertEqual(self.react(CONTACT, "t", ""), [])

    def test_change_is_new_but_same_emoji_again_is_not(self):
        self.add("t", CONTACT, "bot", "hi", me=1)
        self.assertEqual(len(self.react(CONTACT, "t", "👍", id="a")), 1)
        self.assertEqual(self.react(CONTACT, "t", "", id="b"), [])
        self.assertEqual(self.react(CONTACT, "t", "👍", id="c"), [])  # took it away and put it back
        self.assertEqual(len(self.react(CONTACT, "t", "❤️", id="d")), 1)  # changed
        self.assertEqual(len(self.react(CONTACT, "t", "👍", id="e")), 1)  # changed back

    def test_other_skin_tone_is_new_but_heart_with_or_without_selector_is_not(self):
        self.add("t", CONTACT, "bot", "hi", me=1)
        self.assertEqual(len(self.react(CONTACT, "t", "\U0001F44D", id="a")), 1)
        self.assertEqual(len(self.react(CONTACT, "t", "\U0001F44D\U0001F3FD", id="b")), 1)
        self.assertEqual(len(self.react(CONTACT, "t", "\u2764\uFE0F", id="c")), 1)
        self.assertEqual(self.react(CONTACT, "t", "\u2764", id="d"), [])

    def test_prompt_gives_no_fixed_meaning(self):
        self.assertIn("read the emoji in the context", relay.DEFAULTS["prompts"]["reaction"])
        self.assertNotIn("means", relay.DEFAULTS["prompts"]["reaction"])

    def test_another_person_same_emoji_is_new(self):
        self.add("t", GROUP, "bot", "hi", me=1)
        self.assertEqual(len(self.react(GROUP, "t", "👍", sender="a", id="a")), 1)
        self.assertEqual(len(self.react(GROUP, "t", "👍", sender="b", id="b")), 1)

    def test_group_only_on_the_agents_own_message(self):
        self.add("ours", GROUP, "bot", "hi", me=1)
        self.add("theirs", GROUP, "x", "love you")
        self.assertEqual(self.react(GROUP, "theirs", "❤️"), [])
        self.assertEqual(self.react(GROUP, "gone", "❤️", id="r2"), [])
        self.assertEqual(len(self.react(GROUP, "ours", "❤️", id="r3")), 1)

    def test_mention_chat_only_on_the_agents_own_message(self):
        self.add("ours", MENTION, "bot", "hi", me=1)
        self.add("theirs", MENTION, "x", "hello")
        self.assertEqual(self.react(MENTION, "theirs", "👍"), [])
        self.assertEqual(len(self.react(MENTION, "ours", "👍", id="r2")), 1)

    def test_unknown_chat_and_switch_off(self):
        self.assertEqual(self.react("x@s.whatsapp.net", "t", "👍"), [])
        self.add("t", CONTACT, "bot", "hi", me=1)
        self.cfg["feedback"]["incoming_reactions"] = False
        self.assertEqual(self.react(CONTACT, "t", "👍"), [])

    def test_long_text_from_a_modified_client_is_cut(self):
        self.add("t", CONTACT, "bot", "hi", me=1)
        self.assertEqual(self.react(CONTACT, "t", "x" * 500)[0][3], 'REACTION ' + "x" * 16 + ' to your message: "hi"')


class Rows(Base):
    def rows(self, cutoff=0, own=False):
        with Patch(CFG=self.cfg, OWNER=OWNER, db=lambda n: sqlite3.connect(os.path.join(self.dir.name, n)),
                   OWN=[own]):
            return relay.new_rows(self.con, 0, cutoff)[0]

    def test_old_history_never_triggers(self):
        self.add("r", CONTACT, "p", "👍", "reaction", "t")  # 1 Nov 2026 10:00 in the row
        late = relay.msg_epoch("2026-11-01 10:00:00+01:00") + 60
        self.assertEqual(self.rows(cutoff=late), [])

    def test_loop_guard_our_own_reaction_in_another_chat_is_not_read(self):
        self.add("r", CONTACT, "bot", "✅", "reaction", "t", me=1)
        self.assertEqual(self.rows(), [])

    def test_loop_guard_own_number_our_status_mark_is_not_read(self):
        with Patch(CFG=self.cfg, OWNER=OWNER, OWN=[True], api=lambda *a, **k: {}, st=lambda f: os.path.join(self.dir.name, f)):
            relay._sent.clear()
            self.addCleanup(relay._sent.clear)
            relay.react(OWNER, "m1", OWNER, "✅")  # the relay's mark, as the echo comes back
            self.add("echo", OWNER, OWNER, "✅", "reaction", "m1", me=1)
            self.assertEqual(relay.new_rows(self.con, 0, 0)[0], [])
            last = self.add("mine", OWNER, OWNER, "✅", "reaction", "m1", me=1) - 1  # the owner's own tap counts
            self.assertEqual([r[1] for r in relay.new_rows(self.con, last, 0)[0]], ["mine"])


class ReachesTheAgent(Base):
    def test_owner_prompt_needs_no_download(self):
        with Patch(CFG=self.cfg, download=lambda *a: self.fail("download"), RESUMED=set(),
                   st=lambda f: os.path.join(self.dir.name, f)):
            text, voice = relay.owner_prep("r", 'REACTION ✅ to your message: "x"', "reaction")
            self.assertEqual(json.load(open(relay.st("owner-last.json")))["where"], "reaction")  # never an OK (#139)
        self.assertIn('REACTION ✅ to your message: "x"', text)
        self.assertIn("@@SKIP", text)
        self.assertFalse(voice)

    def test_owner_message_goes_to_the_coordinator_as_a_batch_item(self):
        added = []
        with Patch(CFG=self.cfg, OWNER=OWNER, owner_add=lambda *a: added.append(a), thread_route=lambda *a: False):
            relay.dispatch("r", OWNER, OWNER, "REACTION ✅ to your message: \"x\"", "reaction", None)
        self.assertEqual(added, [("r", 'REACTION ✅ to your message: "x"', "reaction")])

    def test_contact_gets_no_marks_or_typing_and_the_hint_stays_outside_the_data(self):
        seen, sent, a = [], [], dict(AGENTS[CONTACT], dir=self.dir.name, members={}, trust="reply", ask_contact=False)
        with tempfile.TemporaryDirectory() as home:
            a["home"] = home
            with Patch(CFG=self.cfg, OWNER=OWNER, enqueue=lambda k, fn: fn(), done_job=lambda k: None,
                       agent_now=lambda a: relay.datetime.datetime(2026, 11, 1),
                       with_feedback=lambda *x: self.fail("with_feedback"), log_thread=lambda *x, **k: None,
                       download=lambda *x: self.fail("download"), member=lambda *x: "",
                       run_agent_told=lambda j, a, p, t: seen.append(p) or "@@REACT: 👍",
                       send_text=lambda *x, **k: sent.append(x), react=lambda *x: self.fail("react"),
                       flag=lambda *x: None, RESUMED=set()):
                relay.dispatch("r", CONTACT, "p", 'REACTION ✅ to your message: "x"', "reaction", a)
        self.assertEqual(sent, [])  # nothing to say: the contact sees nothing
        self.assertLess(seen[0].index("@@SKIP"), seen[0].index(relay.UNTRUSTED_OPEN))
        self.assertIn('REACTION ✅ to your message: "x"', seen[0])

    def test_mention_chat_lets_a_reaction_through(self):
        jobs = []
        with Patch(enqueue_job=lambda *x: jobs.append(x), flag=lambda *x: None,
                   agent_now=lambda a: relay.datetime.datetime(2026, 11, 1)):
            relay.dispatch("r", MENTION, "p", "REACTION 👍 to your message: \"x\"", "reaction", AGENTS[MENTION])
        self.assertEqual(len(jobs), 1)

    def test_group_reaction_wakes_the_agent_without_marks_or_smart_check(self):
        relay.buffers.clear()
        a = dict(AGENTS[GROUP], members={}, self_tags=[], auto_reply="smart", group_label="GROUP.")
        prompts, delivered = [], []
        with Patch(CFG=self.cfg, OWNER=OWNER, enqueue=lambda k, fn: fn(), log_thread=lambda *x: None,
                   member=lambda *x: "Sam", download=lambda *x: self.fail("download"), quoted_text=lambda *x: "",
                   with_feedback=lambda *x: self.fail("with_feedback"), smart_check=lambda *x: self.fail("smart"),
                   run_agent_told=lambda j, a, p, t: prompts.append(p) or "",
                   deliver=lambda *x, **k: delivered.append(k)):
            relay.listen(GROUP, a, "r", "p", 'REACTION ❤️ to your message: "x"', "reaction")
        self.assertIn("reaction to your own message", prompts[0])
        self.assertIn('[Sam]', prompts[0])
        self.assertEqual(delivered[0]["refs"], {})  # no bubble to quote


class NoMarksOnAReaction(Base):
    def test_status_marks_and_read_receipts_skip_the_reaction_row(self):
        self.add("r", OWNER, OWNER, "✅", "reaction", "t")
        self.add("v", OWNER, OWNER, "Yes", "poll_vote", "t")
        self.add("m", OWNER, OWNER, "hello")
        self.cfg["feedback"].update(typing=False)
        calls = []
        with Patch(CFG=self.cfg, OWNER=OWNER, api=lambda path, body=None: calls.append((path, body["message_ids"][0] if path == "mark-read" else body["message_id"]))):
            for m in ("r", "v", "m"):
                relay.with_feedback(OWNER, m, OWNER, lambda: None)
        self.assertEqual({c[1] for c in calls}, {"m"})
        self.assertIn(("mark-read", "m"), calls)


class Config(unittest.TestCase):
    def test_on_by_default_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = relay.load_config(write_cfg(tmp, ""))
            self.assertIs(cfg["feedback"]["incoming_reactions"], True)
            self.assertEqual(relay.validate(cfg)[0], [])
            cfg = relay.load_config(write_cfg(tmp, '[feedback]\nincoming_reactions = "yes"\n'))
            self.assertTrue(any("incoming_reactions" in e for e in relay.validate(cfg)[0]))


if __name__ == "__main__":
    unittest.main()
