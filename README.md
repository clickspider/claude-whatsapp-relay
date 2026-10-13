# claude-whatsapp-relay

Talk to Claude Code in your own folder from WhatsApp. Text, voice notes, photos and files go in; answers (or voice notes) come back.

**Why:** no third-party server reads your messages. WhatsApp connects to a bridge on your own computer ([whatsapp-mcp](https://github.com/verygoodplugins/whatsapp-mcp)), voice notes are transcribed locally with whisper.cpp, and Claude Code runs in your own folder. The only things that leave your computer are WhatsApp itself and your normal Claude conversations.

## How it works
1. The bridge stores incoming messages in a local SQLite file. The relay reads it (read-only) every 2 seconds.
2. A message from **your own chat** runs `claude -p --resume <session>` in your folder (for example a [dossier](https://github.com/clickspider/dossier-starter)). The answer goes back over the bridge API on 127.0.0.1.
3. Voice notes are transcribed with `whisper-cli`. Answers can come back as voice (macOS `say`).
4. Optional and off by default: a locked Claude agent for one contact or group, with its own rules file and its own folder.

**Default = owner-only.** Every other chat is ignored until you declare an agent in the config.

## Quick start (paste a prompt)
Install [whatsapp-mcp](https://github.com/verygoodplugins/whatsapp-mcp) and pair it first. Then paste this into Claude Code, inside your dossier folder:

```
Clone https://github.com/clickspider/claude-whatsapp-relay into ./tools/claude-whatsapp-relay
and follow its SETUP.md with me, one question at a time.
```

By hand:
```bash
cp relay.config.example.toml relay.config.toml   # fill it in
scripts/get-model.sh                              # local speech model, ~550 MB
python3 relay.py check                            # every setting + its risk, and dependency checks
python3 relay.py plan                             # the exact claude commands, runs nothing
scripts/install-launchd.sh                        # macOS: writes the launchd job (add --load to start)
```

Needs: macOS (for `say` and launchd; Linux works without voice replies and launchd), Python 3.11+, Claude Code, ffmpeg, whisper.cpp (`whisper-cli`), and the whatsapp-mcp bridge.

## Commands in your chat
- Anything: goes to your Claude. Start with "voice" to get a voice-note answer.
- `/new`: fresh conversation (a short hand-off carries over).
- For an agent named `alex`: `/alex ok` (send the held draft), `/alex status`, `/alex pause`, `/alex resume`, `/alex intro`, `/alex <your words>`, `/asalex <text>` (dry run: you play the contact).

## Read before you use it
- **Your chat is a remote control for your computer.** Whoever can write in it, can steer Claude. See [SECURITY.md](SECURITY.md).
- **Use a dedicated second WhatsApp number** for the bridge. Unofficial WhatsApp clients break WhatsApp's Terms of Service, and the number can get banned.
- Settings, risks and setup: [SETUP.md](SETUP.md) and [relay.config.example.toml](relay.config.example.toml).

## Tests
`python3 -m unittest discover tests`

## License
MIT. See [LICENSE](LICENSE). Builds on whatsapp-mcp (MIT) and whisper.cpp (MIT).
