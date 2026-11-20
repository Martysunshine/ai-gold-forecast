#!/usr/bin/env bash
# Stop and remove the background worker. Keeps all data (forecast ledger, models, logs).
set -euo pipefail
source "$(dirname "$0")/common.sh"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "Stopped $LABEL" || echo "$LABEL was not loaded"
rm -f "$PLIST" && echo "Removed $PLIST"
echo "Data in $REPO/backend/data and logs in $LOGDIR were left in place."
