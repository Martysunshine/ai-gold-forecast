#!/usr/bin/env bash
# Show the worker's logs.   ./scripts/launchd/logs.sh          last 40 lines of each log
#                           ./scripts/launchd/logs.sh -f       follow them live (Ctrl-C to stop)
source "$(dirname "$0")/common.sh"
if [ "${1:-}" = "-f" ]; then
  exec tail -n 20 -F "$LOGDIR/worker.log" "$LOGDIR/worker.err.log"
fi
for f in worker.log worker.err.log; do
  echo "== $LOGDIR/$f =="
  tail -n 40 "$LOGDIR/$f" 2>/dev/null || echo "(no $f yet)"
done
