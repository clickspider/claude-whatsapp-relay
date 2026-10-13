#!/bin/bash
# Fill launchd/relay.plist.template from the config and (only with --load) start the job.
# Usage: scripts/install-launchd.sh [path/to/relay.config.toml] [--load]
# Without --load it only writes the plist and prints the commands. macOS only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$ROOT/relay.config.toml"
LOAD=0
for a in "$@"; do
  case "$a" in
    --load) LOAD=1 ;;
    *) CONFIG="$(cd "$(dirname "$a")" && pwd)/$(basename "$a")" ;;
  esac
done
[ -f "$CONFIG" ] || { echo "No config at $CONFIG"; exit 1; }

# Read [launchd] from the config with the configured python (must be 3.11+ for tomllib).
read_cfg() {
  python3 - "$CONFIG" <<'EOF'
import os, sys
try:
    import tomllib
except ImportError:
    sys.exit("python3 on PATH is older than 3.11; run this script with a newer python3 first in PATH")
c = tomllib.load(open(sys.argv[1], "rb")).get("launchd", {})
base = os.path.dirname(sys.argv[1])
log = os.path.expanduser(c.get("log_dir", "~/Library/Logs/claude-whatsapp-relay"))
log = log if os.path.isabs(log) else os.path.join(base, log)
print(c.get("label", "com.example.claude-whatsapp-relay"))
print(c.get("python", "python3"))
print(os.path.normpath(log))
EOF
}
{ read -r LABEL; read -r PY; read -r LOG_DIR; } < <(read_cfg)
PY_PATH="$(command -v "$PY" || true)"
[ -n "$PY_PATH" ] || { echo "python not found: $PY"; exit 1; }
"$PY_PATH" -c 'import tomllib' 2>/dev/null || { echo "$PY_PATH is older than Python 3.11"; exit 1; }
[[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "launchd.label may only use letters, digits, . _ -"; exit 1; }

# Validate first; refuse to install a broken or placeholder config.
"$PY_PATH" "$ROOT/relay.py" check --config "$CONFIG" || { echo "Fix the check errors first."; exit 1; }

mkdir -p "$LOG_DIR" && chmod 700 "$LOG_DIR"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ -f "$PLIST" ]; then
  cp "$PLIST" "$PLIST.bak-$(date +%Y%m%d-%H%M%S)"
  echo "Backed up the old $PLIST"
fi
CLAUDE_DIR="$(dirname "$(command -v claude || echo /usr/local/bin/claude)")"
SAFE_PATH="$CLAUDE_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
esc() { printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
sed -e "s|__LABEL__|$(esc "$LABEL")|" -e "s|__PYTHON__|$(esc "$PY_PATH")|" \
    -e "s|__RELAY__|$(esc "$ROOT/relay.py")|" -e "s|__CONFIG__|$(esc "$CONFIG")|" \
    -e "s|__PATH__|$(esc "$SAFE_PATH")|" -e "s|__HOME__|$(esc "$HOME")|" \
    -e "s|__LOG_DIR__|$(esc "$LOG_DIR")|g" \
    "$ROOT/launchd/relay.plist.template" > "$PLIST"
plutil -lint "$PLIST"
echo "Wrote $PLIST"
if [ "$LOAD" = 1 ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "Started $LABEL. Logs: $LOG_DIR/relay.log"
else
  echo "Not started. To start:  launchctl bootstrap gui/$(id -u) \"$PLIST\""
fi
