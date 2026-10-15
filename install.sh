#!/bin/bash
# claude-whatsapp-relay installer (macOS). Safe to run again: every step checks first.
#
#   ./install.sh             install or repair
#   ./install.sh --dry-run   print every step, change nothing
#   ./install.sh --uninstall remove only what this script created (asks before deleting data)
#   ./install.sh --yes       no questions: each answer comes from RELAY_<NAME>, else the default.
#                            Never reads /dev/tty. Prints `LORE-STEP <id> <status> [detail]` lines for apps.
#                            RELAY_NUMBER (required) RELAY_NAME RELAY_FOLDER RELAY_TZNAME RELAY_LABEL RELAY_PORT
#                            RELAY_CONTINUE RELAY_BREW RELAY_MKDIR RELAY_GET_MODEL RELAY_START (y/n, default y)
#                            RELAY_JOBS=n: the caller runs the bridge and relay jobs itself (an app that registers
#                            them under its own name). install.sh pairs, checks, then stops its bridge; no launchd jobs.
#                            RELAY_BRIDGE_LABEL: the bridge job label written to install.bridge_label
#                            RELAY_DEL_CLONE RELAY_DEL_MODEL RELAY_DEL_CONFIG (--uninstall, y/n, default n)
#   ./install.sh --yes --tools-only   check and install the tools, then stop
#
# Every setting lives in relay.config.toml (made from relay.config.example.toml on the first run).
# The only setting outside it: CLAUDE_WHATSAPP_RELAY_DIR, where `curl ... | bash` clones this repo
# (default ~/.claude-whatsapp-relay/relay), because the config does not exist yet at that point.
set -euo pipefail

REPO_URL="https://github.com/clickspider/claude-whatsapp-relay.git"
DRY=0
YES=0
TOOLS_ONLY=0
MODE=install
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --uninstall) MODE=uninstall ;;
    --yes) YES=1 ;;
    --tools-only) TOOLS_ONLY=1 ;;
    -h|--help) sed -n '2,17p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Unknown option: $a (use --dry-run, --uninstall, --yes or --tools-only)"; exit 1 ;;
  esac
done
# RELAY_JOBS=n (only under --yes): no launchd jobs; the calling app registers the bridge and relay itself.
JOBS=y; [ "$YES" = 1 ] && JOBS="${RELAY_JOBS:-y}"

# ---------- output ----------
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; D=""; N=""; fi
step() { printf '\n%s==> %s%s\n' "$B" "$1" "$N"; }
ok()   { printf '  %sok%s    %s\n' "$G" "$N" "$1"; }
info() { printf '  %s\n' "$1"; }
warn() { printf '  %swarn%s  %s\n' "$Y" "$N" "$1"; }
die()  { lore fail "$(printf '%s' "$1" | tr '\n' ' ' | tr -s ' ')"; printf '\n%sStopped:%s %s\n' "$R" "$N" "$1" >&2; exit 1; }
# lore ID STATUS [detail]: one machine-readable progress line, only under --yes (apps parse it).
lore() { [ "$YES" = 1 ] && printf 'LORE-STEP %s\n' "$*"; return 0; }

# run: every command that changes something goes through here.
run() {
  if [ "$DRY" = 1 ]; then printf '  %s[dry-run]%s %s\n' "$D" "$N" "$*"; return 0; fi
  "$@"
}

# ask VAR "question" "default": reads from the terminal, also under `curl | bash`.
# Under --yes: the answer is $RELAY_<VAR>, else the default. /dev/tty is never read.
ask() {
  local _v="$1" _q="$2" _d="${3:-}" _a="" _e="RELAY_$1"
  if [ "$YES" = 1 ]; then
    _a="${!_e:-$_d}"
    printf '  ? %s: %s\n' "$_q" "$_a"
  elif [ "$DRY" = 1 ]; then
    printf '  %s[dry-run]%s would ask: %s (using "%s")\n' "$D" "$N" "$_q" "$_d"
    _a="$_d"
  else
    printf '  %s?%s %s%s: ' "$B" "$N" "$_q" "${_d:+ [$_d]}" > /dev/tty
    IFS= read -r _a < /dev/tty || true
    _a="${_a:-$_d}"
  fi
  printf -v "$_v" '%s' "$_a"
}

# yes_no KEY "question" y|n  -> exit status 0 for yes. KEY names the --yes answer: RELAY_<KEY>.
yes_no() {
  ask "$1" "$2 (y/n)" "$3"
  case "${!1}" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

[ "$(uname -s)" = "Darwin" ] || die "this installer is macOS only for now. On Linux, follow the manual steps in README.md."
[ "$(id -u)" != 0 ] || die "do not run this with sudo. It installs per-user launchd jobs."

# ---------- bootstrap: under `curl | bash` there is no checkout yet ----------
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$(dirname "$SELF")/relay.py" ]; then
  ROOT="$(cd "$(dirname "$SELF")" && pwd)"
else
  ROOT="${CLAUDE_WHATSAPP_RELAY_DIR:-$HOME/.claude-whatsapp-relay/relay}"
  step "Get claude-whatsapp-relay into $ROOT"
  command -v git >/dev/null || die "git is missing. Run: xcode-select --install   then run this again."
  if [ -d "$ROOT/.git" ]; then run git -C "$ROOT" pull --ff-only
  else run mkdir -p "$(dirname "$ROOT")"; run git clone "$REPO_URL" "$ROOT"; fi
  [ "$DRY" = 1 ] && { info "[dry-run] would continue with: bash $ROOT/install.sh $*"; exit 0; }
  if [ "$YES" = 1 ]; then exec bash "$ROOT/install.sh" "$@"; fi
  exec bash "$ROOT/install.sh" "$@" < /dev/tty
fi

CONFIG="$ROOT/relay.config.toml"
EXAMPLE="$ROOT/relay.config.example.toml"
MANIFEST="$ROOT/.installed"
UID_NUM="$(id -u)"

# Remember what this script created, so --uninstall removes only that.
remember() {
  [ "$DRY" = 1 ] && return 0
  touch "$MANIFEST" && chmod 600 "$MANIFEST"
  grep -qxF "$1" "$MANIFEST" || printf '%s\n' "$1" >> "$MANIFEST"
}

# ---------- python 3.11+ (tomllib) ----------
find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import tomllib' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

# Read every setting the installer needs from the config, as KEY=value lines.
read_config() {
  "$PY" - "$1" <<'EOF'
import os, sys, tomllib, urllib.parse
path = sys.argv[1]
c = tomllib.load(open(path, "rb"))
base = os.path.dirname(os.path.abspath(path))
def p(v):
    v = os.path.expanduser(str(v))
    return os.path.normpath(v if os.path.isabs(v) else os.path.join(base, v))
i, b, l = c.get("install", {}), c.get("bridge", {}), c.get("launchd", {})
api = b.get("api", "http://127.0.0.1:8080/api")
u = urllib.parse.urlparse(api)
out = {
    "MANAGE_BRIDGE": str(i.get("manage_bridge", True)).lower(),
    "WA_DIR": p(i.get("whatsapp_mcp_dir", "~/.claude-whatsapp-relay/whatsapp-mcp")),
    "WA_REPO": i.get("whatsapp_mcp_repo", "https://github.com/verygoodplugins/whatsapp-mcp.git"),
    "WA_REF": i.get("whatsapp_mcp_ref", ""),
    "BRIDGE_LABEL": i.get("bridge_label", "com.claude-whatsapp-relay.bridge"),
    "STORE": p(b.get("store", "")) if b.get("store") else "",
    "PORT": str(u.port or 8080),
    "HOST": u.hostname or "",
    "RELAY_LABEL": l.get("label", "com.example.claude-whatsapp-relay"),
    "LOG_DIR": p(l.get("log_dir", "~/Library/Logs/claude-whatsapp-relay")),
    "MODEL": p(c.get("paths", {}).get("whisper_model", "models/ggml-large-v3-turbo-q5_0.bin")),
}
for k, v in out.items():
    if "\n" in v:
        sys.exit(f"{k} has a line break")
    print(f"{k}={v}")
EOF
}

load_settings() {
  local line
  while IFS= read -r line; do
    printf -v "${line%%=*}" '%s' "${line#*=}"
  done < <(read_config "$1")
  [[ "$BRIDGE_LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || die "install.bridge_label may only use letters, digits, . _ -"
  [[ "$PORT" =~ ^[0-9]+$ ]] || die "bad port in bridge.api"
}

plist_path() { printf '%s/Library/LaunchAgents/%s.plist' "$HOME" "$1"; }
job_loaded() { launchctl print "gui/$UID_NUM/$1" >/dev/null 2>&1; }

# =====================================================================
# UNINSTALL
# =====================================================================
if [ "$MODE" = uninstall ]; then
  step "Uninstall: only what install.sh created (listed in $MANIFEST)"
  [ -f "$MANIFEST" ] || { info "Nothing to remove: no $MANIFEST. Nothing was created by install.sh here."; exit 0; }
  # Jobs first, so nothing holds files open.
  while IFS=' ' read -r kind val; do
    [ "$kind" = launchd ] || continue
    info "launchd job $val"
    if job_loaded "$val"; then run launchctl bootout "gui/$UID_NUM/$val" || true; fi
    if [ -f "$(plist_path "$val")" ]; then run rm -f "$(plist_path "$val")"; fi
  done < "$MANIFEST"
  KEPT=""
  while IFS=' ' read -r kind val; do
    case "$kind" in
      clone)
        [ -d "$val" ] || continue
        warn "$val holds your WhatsApp session and message history."
        if yes_no DEL_CLONE "Delete $val? This cannot be undone" n; then run rm -rf "$val"; else info "Kept $val"; KEPT="$KEPT$kind $val"$'\n'; fi ;;
      model)
        [ -f "$val" ] || continue
        if yes_no DEL_MODEL "Delete the speech model $val (~550 MB)?" n; then run rm -f "$val"; else info "Kept $val"; KEPT="$KEPT$kind $val"$'\n'; fi ;;
      config)
        [ -f "$val" ] || continue
        if yes_no DEL_CONFIG "Delete your settings $val?" n; then run rm -f "$val"; else info "Kept $val"; KEPT="$KEPT$kind $val"$'\n'; fi ;;
      brew)
        info "Homebrew package '$val' was installed by install.sh. Other apps may use it."
        info "  To remove it yourself: brew uninstall $val" ;;
    esac
  done < "$MANIFEST"
  # Keep only what you chose to keep, so a later --uninstall can still offer it.
  if [ -n "$KEPT" ] && [ "$DRY" = 0 ]; then printf '%s' "$KEPT" > "$MANIFEST"; else run rm -f "$MANIFEST"; fi
  step "Done. Logs (if any) stay in ~/Library/Logs/claude-whatsapp-relay."
  exit 0
fi

# =====================================================================
# INSTALL
# =====================================================================
cat <<EOF

${B}claude-whatsapp-relay${N}: talk to Claude Code in your own folder from WhatsApp.

${Y}Read this first:${N}
  1. Your WhatsApp chat becomes a remote control for this computer.
  2. The bridge is an unofficial WhatsApp client. That breaks WhatsApp's Terms
     of Service, and the number can get banned. Use a second number for it.
  3. Nothing is installed or started without a yes from you.
EOF
[ "$DRY" = 1 ] && printf '\n%s--dry-run: nothing below runs. Commands are printed only.%s\n' "$Y" "$N"
yes_no CONTINUE "Continue?" y || { lore fail "not confirmed"; exit 0; }

# ---------- 1. tools ----------
step "1/6  Check the tools"
lore tools start
command -v brew >/dev/null && lore tool brew ok || die "Homebrew is missing. Install it from https://brew.sh, then run this again."
xcode-select -p >/dev/null 2>&1 || die "Apple's command line tools are missing (the bridge needs a C compiler). Run: xcode-select --install"

MISSING=""
command -v go >/dev/null          && ok "go ($(go version | awk '{print $3}'))" || MISSING="$MISSING go"
command -v ffmpeg >/dev/null      && ok "ffmpeg" || MISSING="$MISSING ffmpeg"
command -v whisper-cli >/dev/null && ok "whisper-cli" || MISSING="$MISSING whisper-cpp"
PY="$(find_python || true)"
[ -n "$PY" ] && ok "python 3.11+ ($PY)" || MISSING="$MISSING python@3.12"
for f in go ffmpeg whisper-cpp python@3.12; do
  case " $MISSING " in *" $f "*) lore tool "$f" missing ;; *) lore tool "$f" ok ;; esac
done
if [ -n "$MISSING" ]; then
  info "Missing:$MISSING"
  yes_no BREW "Install them with Homebrew now (brew install$MISSING)?" y || die "install the missing tools, then run this again."
  for f in $MISSING; do lore tool "$f" installing; run brew install "$f"; remember "brew $f"; lore tool "$f" installed; done
  [ -n "$PY" ] || PY="$(find_python || true)"
  [ -n "$PY" ] || { [ "$DRY" = 1 ] && PY=python3; } || die "python 3.11+ still not found after brew install."
fi

if command -v claude >/dev/null && claude --version >/dev/null 2>&1; then
  ok "Claude Code ($(claude --version 2>/dev/null | head -1))"
  lore tool claude ok
else
  lore tool claude missing
  die "Claude Code is not installed. Install it with:
    curl -fsSL https://claude.ai/install.sh | bash
  Then run 'claude' once and sign in there (this installer never signs in for you).
  Then run this installer again."
fi
info "Claude Code must be signed in: run 'claude' once yourself if you have not yet."
lore tools ok
if [ "$TOOLS_ONLY" = 1 ]; then lore done ok; exit 0; fi

# ---------- 2. config ----------
step "2/6  Your settings (relay.config.toml)"
if [ -f "$CONFIG" ]; then
  ok "Using your existing $CONFIG (edit it to change answers)"
  SETTINGS_FROM="$CONFIG"
else
  info "A few questions. Every answer goes into relay.config.toml and nowhere else."
  info "Only messages from YOUR number reach Claude. Every other chat is ignored."
  while :; do
    ask NUMBER "Your own WhatsApp number (the one you send FROM), country code first, digits only, e.g. 31612345678" ""
    NUMBER="${NUMBER//[^0-9]/}"
    [[ "$NUMBER" =~ ^[1-9][0-9]{6,14}$ ]] && break
    [ "$DRY" = 1 ] && { NUMBER="15551234567"; break; }
    [ "$YES" = 1 ] && die "RELAY_NUMBER is not a full number with country code (digits only)."
    warn "That does not look like a full number with country code. Try again."
  done
  ask NAME "What should Claude call you?" "$(id -F 2>/dev/null | awk '{print $1}')"
  ask FOLDER "Which folder should Claude work in? (it can read and edit everything in it)" "$HOME/dossier"
  FOLDER="${FOLDER/#\~/$HOME}"
  TZ_DEF="$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')"
  ask TZNAME "Your timezone" "${TZ_DEF:-UTC}"
  if [ ! -d "$FOLDER" ]; then
    yes_no MKDIR "$FOLDER does not exist. Create it?" y && run mkdir -p "$FOLDER" || die "pick an existing folder."
  fi
  WA_DEF="$HOME/.claude-whatsapp-relay/whatsapp-mcp"
  # Only --yes callers (apps) pick another launchd label or bridge port; the terminal flow keeps the defaults.
  LABEL="com.claude-whatsapp-relay.relay"; PORT_ANS=8080; BLABEL="com.claude-whatsapp-relay.bridge"
  if [ "$YES" = 1 ]; then LABEL="${RELAY_LABEL:-$LABEL}"; PORT_ANS="${RELAY_PORT:-$PORT_ANS}"; BLABEL="${RELAY_BRIDGE_LABEL:-$BLABEL}"; fi
  [[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || die "RELAY_LABEL may only use letters, digits, . _ -"
  [[ "$BLABEL" =~ ^[A-Za-z0-9._-]+$ ]] || die "RELAY_BRIDGE_LABEL may only use letters, digits, . _ -"
  [[ "$PORT_ANS" =~ ^[0-9]+$ ]] || die "RELAY_PORT must be a number"
  if [ "$DRY" = 1 ]; then
    info "[dry-run] would write $CONFIG (mode 600) from the example with your answers"
    SETTINGS_FROM="$EXAMPLE"
  else
    "$PY" - "$EXAMPLE" "$CONFIG" "$NUMBER@s.whatsapp.net" "${NAME:-Owner}" "$FOLDER" "$TZNAME" \
         "$WA_DEF/whatsapp-bridge/store" "$PY" "$LABEL" "http://127.0.0.1:$PORT_ANS/api" "$BLABEL" <<'EOF'
import json, os, sys
src, dst, jid, name, folder, tz, store, py, label, api, blabel = sys.argv[1:]
want = {("owner", "jid"): jid, ("owner", "name"): name, ("owner", "folder"): folder,
        ("owner", "timezone"): tz, ("bridge", "store"): store, ("bridge", "api"): api,
        ("launchd", "label"): label, ("launchd", "python"): py, ("install", "bridge_label"): blabel}
sec, out, done = "", [], set()
for line in open(src).read().splitlines(True):
    s = line.strip()
    if s.startswith("[") and not s.startswith("[["):
        sec = s.strip("[]")
    key = s.split("=", 1)[0].strip() if "=" in s and not s.startswith("#") else None
    if (sec, key) in want and (sec, key) not in done:
        line = f"{key} = {json.dumps(want[(sec, key)], ensure_ascii=False)}\n"
        done.add((sec, key))
    out.append(line)
missing = set(want) - done
if missing:
    sys.exit(f"example config is missing {sorted(missing)}")
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("".join(out))
EOF
    chmod 600 "$CONFIG"
    remember "config $CONFIG"
    ok "Wrote $CONFIG (only you can read it)"
    SETTINGS_FROM="$CONFIG"
  fi
fi
load_settings "$SETTINGS_FROM"
lore config ok

# ---------- 3. bridge ----------
step "3/6  WhatsApp bridge (whatsapp-mcp)"
BRIDGE_DIR="$WA_DIR/whatsapp-bridge"
BRIDGE_BIN="$BRIDGE_DIR/whatsapp-bridge"
health() {
  local tok
  tok="$(cat "$BRIDGE_DIR/store/.bridge-token" 2>/dev/null || true)"
  [ -n "$tok" ] && curl -fsS -o /dev/null -m 3 -H "Authorization: Bearer $tok" "http://127.0.0.1:$PORT/api/health"
}
if [ "$MANAGE_BRIDGE" != true ]; then
  info "install.manage_bridge = false: you run the bridge yourself. Skipping."
else
  [ "$STORE" = "$BRIDGE_DIR/store" ] || die "bridge.store ($STORE) must be $BRIDGE_DIR/store when install.manage_bridge = true. Fix relay.config.toml."
  [ "$HOST" = 127.0.0.1 ] || [ "$HOST" = localhost ] || die "bridge.api must use 127.0.0.1"

  if [ -d "$WA_DIR/.git" ]; then
    ok "Code already in $WA_DIR"
    run git -C "$WA_DIR" fetch --tags --quiet
  else
    run mkdir -p "$(dirname "$WA_DIR")"
    run git clone --quiet "$WA_REPO" "$WA_DIR"
    remember "clone $WA_DIR"
  fi
  if [ -n "$WA_REF" ]; then run git -C "$WA_DIR" -c advice.detachedHead=false checkout --quiet "$WA_REF"; fi
  info "Building the bridge (Go). The first build downloads Go modules and takes a minute."
  if [ "$DRY" = 1 ]; then info "[dry-run] (cd $BRIDGE_DIR && go build -o whatsapp-bridge .)"
  else
    (cd "$BRIDGE_DIR" && go build -o whatsapp-bridge .) || die "the bridge did not build. See the Go errors above."
    ok "Built $BRIDGE_BIN"
  fi
  lore build ok

  BRIDGE_PLIST="$(plist_path "$BRIDGE_LABEL")"
  if [ "$DRY" = 0 ] && job_loaded "$BRIDGE_LABEL" && health; then
    ok "Bridge job $BRIDGE_LABEL already runs and is connected"
    run launchctl kickstart -k "gui/$UID_NUM/$BRIDGE_LABEL"   # pick up the new build
  else
    if [ "$DRY" = 0 ] && job_loaded "$BRIDGE_LABEL"; then run launchctl bootout "gui/$UID_NUM/$BRIDGE_LABEL" || true; fi
    if [ "$DRY" = 0 ] && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      die "port $PORT is already in use (another bridge?). Either set a free port in bridge.api,
  or keep your own bridge: set install.manage_bridge = false and bridge.store to its store/ folder."
    fi
    if [ "$DRY" = 0 ] && [ -s "$BRIDGE_DIR/store/whatsapp.db" ]; then
      info "A WhatsApp session exists. Starting the bridge to check it (a QR shows only if it must pair again)."
    else
      info "Now pair the bridge with WhatsApp, on the phone with the bridge number:"
      info "  WhatsApp > Settings > Linked devices > Link a device > scan the QR below."
    fi
    lore qr wait
    if [ "$DRY" = 1 ]; then
      info "[dry-run] (cd $BRIDGE_DIR && WHATSAPP_BRIDGE_PORT=$PORT ./whatsapp-bridge)   # shows the QR, waits up to 5 min"
    else
      (cd "$BRIDGE_DIR" && WHATSAPP_BRIDGE_PORT="$PORT" exec ./whatsapp-bridge) &
      BPID=$!
      trap 'kill "$BPID" 2>/dev/null || true' EXIT INT TERM
      paired=0
      for _ in $(seq 1 150); do
        sleep 2
        kill -0 "$BPID" 2>/dev/null || break
        if health; then paired=1; break; fi
      done
      if [ "$paired" = 0 ] || [ "$JOBS" != n ]; then   # JOBS=n: keep it up for the check; the EXIT trap stops it
        kill "$BPID" 2>/dev/null || true
        wait "$BPID" 2>/dev/null || true
        trap - EXIT INT TERM
      fi
      [ "$paired" = 1 ] || die "the bridge did not connect (QR not scanned in time, or it stopped). Run ./install.sh again."
      printf '\n'; ok "Paired and connected"; lore paired ok
    fi
    run mkdir -p "$LOG_DIR"; run chmod 700 "$LOG_DIR"
    if [ "$JOBS" = n ]; then info "RELAY_JOBS=n: the caller starts the bridge job ($BRIDGE_LABEL)."
    else
    esc() { printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
    if [ "$DRY" = 1 ]; then info "[dry-run] write $BRIDGE_PLIST from launchd/bridge.plist.template"
    else
      mkdir -p "$(dirname "$BRIDGE_PLIST")"   # a new Mac user has no ~/Library/LaunchAgents yet
      sed -e "s|__LABEL__|$(esc "$BRIDGE_LABEL")|" -e "s|__BRIDGE_BIN__|$(esc "$BRIDGE_BIN")|" \
          -e "s|__BRIDGE_DIR__|$(esc "$BRIDGE_DIR")|" -e "s|__PORT__|$(esc "$PORT")|" \
          -e "s|__HOME__|$(esc "$HOME")|" -e "s|__LOG_DIR__|$(esc "$LOG_DIR")|g" \
          "$ROOT/launchd/bridge.plist.template" > "$BRIDGE_PLIST"
      plutil -lint "$BRIDGE_PLIST" >/dev/null
    fi
    run launchctl bootstrap "gui/$UID_NUM" "$BRIDGE_PLIST"
    remember "launchd $BRIDGE_LABEL"
    if [ "$DRY" = 0 ]; then
      for _ in $(seq 1 15); do health && break; sleep 2; done
      health && ok "Bridge runs in the background ($BRIDGE_LABEL). Log: $LOG_DIR/bridge.log" \
             || die "the bridge job started but does not answer. Look in $LOG_DIR/bridge.log"
    fi
    fi
  fi
fi
lore bridge ok

# ---------- 4. speech model ----------
step "4/6  Local speech model (voice notes never leave this computer)"
if [ -f "$MODEL" ]; then ok "Model already there: $MODEL"; lore model ok
elif yes_no GET_MODEL "Download the whisper model now (~550 MB)?" y; then
  lore model downloading
  run "$ROOT/scripts/get-model.sh"; remember "model $MODEL"; lore model ok
else warn "Skipped. Voice notes will fail until you run scripts/get-model.sh"; lore model skip; fi

# ---------- 5. check ----------
step "5/6  Check every setting"
if [ "$DRY" = 1 ]; then info "[dry-run] $PY $ROOT/relay.py check --config $CONFIG"
else "$PY" "$ROOT/relay.py" check --config "$CONFIG" || die "fix the problems above in $CONFIG, then run ./install.sh again."; fi
lore check ok

# ---------- 6. start ----------
step "6/6  Start the relay"
if [ "$JOBS" = n ]; then
  info "RELAY_JOBS=n: the caller starts the relay job ($RELAY_LABEL)."
  lore start skip
elif yes_no START "Start the relay now, and on every login?" y; then
  # install-launchd.sh reads the config with the python3 first on PATH: make that the 3.11+ one.
  PATH="$(dirname "$PY"):$PATH" run "$ROOT/scripts/install-launchd.sh" "$CONFIG" --load
  remember "launchd $RELAY_LABEL"
  lore start ok
else
  info "Not started. Later: scripts/install-launchd.sh --load"
  lore start skip
fi
lore done ok

cat <<EOF

${G}${B}Done.${N}
  1. From your own phone, send "hi" to the bridge number. Expect 👀, then ✅, then an answer.
  2. Change settings in $CONFIG, then run ./install.sh again.
  3. Logs: $LOG_DIR
  4. Remove it: ./install.sh --uninstall
EOF
