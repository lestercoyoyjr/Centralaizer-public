#!/usr/bin/env bash
#
# Install a macOS LaunchAgent so the Centralaizer hub (main.py) auto-starts on
# login and auto-restarts if it ever exits (KeepAlive). Fixes the failure mode
# where a `nohup python main.py` dies on reboot/shell-close and silently stops
# capturing sessions.
#
# Usage:  scripts/install-launchagent.sh           # install + load
#         scripts/install-launchagent.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
PY="$REPO/.venv/bin/python"
LABEL="com.centralaizer.hub"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/.localmem"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL"
  exit 0
fi

[ -x "$PY" ] || { echo "venv python not found at $PY — create the venv first" >&2; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$REPO/main.py</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/hub.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/hub.err.log</string>
</dict>
</plist>
EOF

# stop any hub already bound to the ports so the agent's instance can bind them,
# then (re)load the agent. Match bare "main.py" — a manual `python main.py` shows
# up with argv "main.py" (relative), not the absolute path.
pkill -f "main\.py" 2>/dev/null || true
sleep 2
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
echo "installed + loaded $LABEL"
echo "  plist: $PLIST"
echo "  logs:  $LOGDIR/hub.log  (errors: hub.err.log)"
echo "  manage: launchctl {unload|load} \"$PLIST\"   ·   uninstall: $0 --uninstall"
