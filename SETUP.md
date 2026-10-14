# SETUP: steps for Claude

Claude: set up this relay WITH the user, from inside their dossier (or any folder they want to control from WhatsApp).
Rules for you:
1. Ask ONE short question at a time. Wait for the answer.
2. Before you write a setting, explain in one or two sentences what it does and its risk (LOW / MED / HIGH).
3. Write every answer into `relay.config.toml` (copy it from `relay.config.example.toml` first). Never put personal values anywhere else.
4. Never send a WhatsApp message, start a background job or install anything without a clear "yes".
5. Keep the safe default when the user is not sure.

## 0. Say what this is (no question yet)
Tell the user, in these words or close:
"This lets you talk to Claude in this folder from WhatsApp. Three things first:
1. Your WhatsApp chat becomes a remote control for this computer. Whoever can write in it can steer Claude here.
2. It uses an unofficial WhatsApp client. That breaks WhatsApp's Terms of Service, and the number can get banned.
3. So the safest setup is a dedicated second WhatsApp number for the bridge (a cheap prepaid SIM or eSIM), and you message it from your normal number."
Then ask: "Do you want to continue?" Stop if the answer is no.

## 1. Check the parts (no questions)
1. Check: `python3 --version` (3.11 or newer), `claude --version`, `ffmpeg -version`, `whisper-cli --help`. List what is missing with the install command (`brew install ffmpeg whisper-cpp`), and ask before you install.
2. Check that the whatsapp-mcp bridge is installed and paired. If not, offer `./install.sh` on macOS (it asks before each step, clones and builds the bridge, and shows the QR code; the user scans it, so the user runs it in their own terminal). Otherwise point the user to https://github.com/verygoodplugins/whatsapp-mcp and stop here until it runs.
3. Copy the example: `cp relay.config.example.toml relay.config.toml && chmod 600 relay.config.toml`.

## 2. Interview (one question at a time)
1. "Which number did you pair with the bridge, and from which number will you send messages to it?"
   Explain: only the second one (yours) becomes the owner chat. Every other chat is ignored. Write `owner.jid` as `<country code><number>@s.whatsapp.net`, digits only, no `+`.
2. "What should Claude call you?" → `owner.name`.
3. "Which folder should Claude work in?" Suggest the current dossier. Explain: MED, Claude can read and edit everything in it from WhatsApp. → `owner.folder`.
4. "What is your timezone?" (for example `Europe/Amsterdam`) → `owner.timezone`. Explain: a fresh session starts every day at 04:00, with a short hand-off.
5. "Which tools may Claude use from WhatsApp?" Recommend the default: file tools only (LOW). Explain: adding `Bash` lets a message run commands on this computer (HIGH). → `owner.tools`.
6. "Should Claude use any MCP servers from WhatsApp, for example a calendar?" Default: none (LOW). If yes, write a small JSON file in the `claude --mcp-config` format with only those servers, and set `owner.mcp_config` (MED). Suggest putting send-type tools in `owner.denied_tools`.
7. "Do you want voice notes answered with a voice note?" → `owner.voice_replies` (LOW, macOS only).
8. "How many messages per minute and per day is normal for you?" Explain: a cap also caps the Claude bill. → `[limits]`. Keep the defaults if unsure.
9. "Where is the bridge's store folder?" Find it (usually `<whatsapp-mcp>/whatsapp-bridge/store`; `install.sh` uses `~/.claude-whatsapp-relay/whatsapp-mcp/whatsapp-bridge/store`) and confirm. → `bridge.store`. If the user runs their own bridge, set `install.manage_bridge = false`.
10. "Do you want Claude to answer anyone else (a contact or a group)?" Recommend NO for the first week. Agents are off by default.
    If yes, for each agent: explain it is a locked Claude (no shell, no web, only its own folder), copy `agents/example/` to `agents/<name>/`, fill in `RULES.md` with the user, add an `[[agent]]` block, and start with `live = false` and `trust = "draft"` (every reply waits for `/<name> ok`). Explain each HIGH option (`extra_mcp_config`, `unrestricted`) only if the user asks for more power, and recommend against it.
11. Leave `[danger]` and `hooks.after_log` at their defaults. Mention them in one line each, only if the user asks.

## 3. Check
1. Run `scripts/get-model.sh` (ask first: it downloads about 550 MB).
2. Run `python3 relay.py check`. Read the SETTINGS table back to the user in plain words. Fix every ERROR and FAIL with them.
3. Run `python3 relay.py plan` and show the owner command line. Point out `--tools` and `--strict-mcp-config`.

## 4. Start (ask first)
1. Ask: "Start the relay now, and on every login?" On yes (macOS): `scripts/install-launchd.sh --load`.
   Without launchd: `python3 relay.py run` in a terminal.
2. Ask the user to send "hi" from their own number. Watch the log (`[launchd] log_dir`/relay.log). Expect 👀 then ✅ on the message.
3. Tell the user: "Done. Your chat is now a remote control for this folder. Keep your phone locked, and do not forward strangers' messages into it."
4. If this is a dossier with a `backup.sh`: never add `relay.config.toml` to a public repo.
