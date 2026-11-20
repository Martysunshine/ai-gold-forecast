#!/usr/bin/env bash
# Is the worker installed, loaded and healthy? What has it done recently?
source "$(dirname "$0")/common.sh"
echo "== launchd =="
if [ -f "$PLIST" ]; then echo "plist installed: $PLIST"; else echo "plist NOT installed (run ./scripts/launchd/install.sh)"; fi
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl print "$DOMAIN/$LABEL" | grep -E "state =|pid =|last exit code|runs =" | sed 's/^[[:space:]]*/  /'
else
  echo "  not loaded"
fi
echo "== worker =="
(cd "$REPO/backend" && PYTHONPATH=. "$PY" -W ignore -m app.worker status)
echo "== recent log =="
tail -n 8 "$LOGDIR/worker.log" 2>/dev/null || echo "(no log yet)"
