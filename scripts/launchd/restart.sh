#!/usr/bin/env bash
# Restart the background worker (it re-reads the code and catches up on anything it missed while it was down).
set -euo pipefail
source "$(dirname "$0")/common.sh"
[ -f "$PLIST" ] || { echo "Not installed. Run ./scripts/launchd/install.sh first."; exit 1; }
launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"
echo "Restarted $LABEL. Check it with ./scripts/launchd/status.sh"
