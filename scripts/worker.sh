#!/usr/bin/env bash
# One entry point for the background worker (macOS launchd agent).
#   ./scripts/worker.sh install     install and start it (safe to repeat)
#   ./scripts/worker.sh status      is it installed, running, healthy; what ran last; which runtime is operating
#   ./scripts/worker.sh logs [-f]   show (or follow) its logs
#   ./scripts/worker.sh restart     restart it
#   ./scripts/worker.sh uninstall   stop and remove it (data and logs are kept)
#   ./scripts/worker.sh cycles      the persisted full-cycle records (trigger, time, steps) and whether the 23:30 UTC schedule has run
#   ./scripts/worker.sh once      run one full cycle in this terminal (collect, validate, score, learn, publish, alerts, paper)
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)/launchd"
cmd="${1:-status}"; shift || true
case "$cmd" in
  install|status|logs|restart|uninstall) exec "$D/$cmd.sh" "$@" ;;
  cycles) source "$D/common.sh"; cd "$REPO/backend" && PYTHONPATH=. exec "$PY" -W ignore -m app.worker cycles ;;
  once) source "$D/common.sh"; cd "$REPO/backend" && PYTHONPATH=. exec "$PY" -W ignore -m app.worker once ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
