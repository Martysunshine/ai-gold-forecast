#!/usr/bin/env bash
# Install (or refresh) the background worker as a per-user launchd agent. Safe to run repeatedly.
#   ./scripts/launchd/install.sh            install / update and start
#   ./scripts/launchd/install.sh --dry-run  write nothing; print the plist that would be installed
set -euo pipefail
source "$(dirname "$0")/common.sh"

render_plist() {
cat <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>-W</string><string>ignore</string>
    <string>-m</string><string>app.worker</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO/backend</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONPATH</key><string>$REPO/backend</string><key>GOLD_RUNTIME</key><string>launchd</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOGDIR/worker.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/worker.err.log</string>
</dict>
</plist>
PLISTEOF
}

if [ "${1:-}" = "--dry-run" ]; then render_plist; exit 0; fi

command -v python3 >/dev/null || { echo "python3 (3.11+) is required"; exit 1; }
if [ ! -x "$PY" ]; then
  echo "==> Creating backend virtualenv"
  python3 -m venv "$REPO/backend/.venv"
  "$REPO/backend/.venv/bin/pip" install -q -r "$REPO/backend/requirements.txt"
fi
mkdir -p "$LOGDIR" "$(dirname "$PLIST")"
render_plist > "$PLIST.tmp"
plutil -lint "$PLIST.tmp" >/dev/null
mv "$PLIST.tmp" "$PLIST"
# Idempotent: unload any previous copy (ignore "not loaded"), then load the fresh definition.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
echo "Installed $LABEL"
echo "  logs:   $LOGDIR/worker.log (and worker.err.log)"
echo "  status: ./scripts/launchd/status.sh"
echo "  remove: ./scripts/launchd/uninstall.sh"
echo "Note: a sleeping or powered-off Mac records nothing. After waking, the worker catches up on history; gaps stay gaps."
