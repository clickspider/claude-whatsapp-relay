# Security

## Threat model in one line
Your owner chat is a remote control for your computer. Everything else is designed so that other people's text can never get that power.

## Who can do what
| Who | Trust level | What Claude can do for them | What it cannot do |
|---|---|---|---|
| You (owner chat) | owner | Your tools from `owner.tools` (default: file tools only) in `owner.folder` + `extra_dirs`; MCP servers only from `owner.mcp_config` (default: none) | Edit hooks, Claude settings, LaunchAgents, the relay code, its config or state (`danger.protect_sensitive_paths`, default on). Run commands, unless you add `Bash`. |
| A contact, `trust = "draft"` | low | A locked agent writes a draft. Nothing is sent until you reply `/<name> ok`. | Anything outside its own `home` folder. No shell, no web, no MCP, no settings. |
| A contact, `trust = "reply"` | low | Same locked agent, but it answers by itself. | Same as above. |
| A group, `trigger = "listen"` | low | Reads the group, answers when tagged. | Same as above. |
| Any other chat | none | Nothing. Ignored. | Everything. |

Every agent ALWAYS runs with `claude --restricted --strict-mcp-config --tools <file tools> --permission-mode acceptEdits`, its working folder set to its own agent folder, and only its `home` (plus `read_also`) added. It cannot edit its own `RULES.md`. Its only way out is the relay: the reply goes to that one chat, notes and drafts go to you.

## Opt-ins that raise the risk (all off by default; `relay.py check` marks them HIGH)
- `owner.tools` with `Bash` or `WebFetch`: text in your chat (or a forwarded message, image or file) can run commands.
- `owner.inherit_global_setup = true`: your owner run gets every tool and MCP server from your global Claude setup.
- `agent.extra_mcp_config` + `extra_allowed_tools`: an agent gets MCP tools (for example email). It stays `--restricted`, but anyone in that chat can ask it to use them.
- `agent.unrestricted = true` + `danger.allow_unrestricted_agents = true`: the agent loses the sandbox. Anyone in that chat can ask it to read your files. Avoid.
- `danger.self_edit = true`: your Claude may edit `relay.py`, and the relay restarts on change. Injected text could plant code that runs later.
- `danger.protect_sensitive_paths = false`: your Claude may edit hooks and settings = code that runs in later sessions.
- `hooks.after_log`: runs a command after each agent log. If it pushes to git, other people's messages leave your computer.

## Prompt injection (second order)
Agents log their chats to `<home>/threads/`. Every logged message is wrapped in `<<<UNTRUSTED ... UNTRUSTED>>>`, and the owner prompt says that this text and everything under the agents folder is data, never instructions. This lowers the risk; it does not remove it. Keep the owner run narrow if you let your Claude read agent logs.

## Other limits
- Per-chat rate limits (per minute, per day), a max group buffer and a run timeout, from `[limits]`.
- The bridge API must be on 127.0.0.1; the token is read from `WHATSAPP_BRIDGE_TOKEN` or `<store>/.bridge-token`.
- `messages.db` is opened read-only. SQL is parameterized. No `shell=True`. Prompts go to `claude` on stdin, never as arguments. `say` reads its text from stdin.
- The state folder is created with mode 0700; the launchd job uses umask 077 and a 0700 log folder (logs can hold message text).

## Known gaps (not verified)
- Whether claude.ai connectors still load with `--strict-mcp-config` and no `--mcp-config`.
- Any local process that can write the bridge's `messages.db` can fake a message from you. Keep the bridge store private (0700).
- WhatsApp Terms of Service: unofficial clients can get a number banned. Use a dedicated second number.

## Reporting
Please open a private security advisory on the GitHub repo (Security tab, "Report a vulnerability"). Do not open a public issue for security problems.
