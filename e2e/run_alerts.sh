#!/usr/bin/env bash
# Runs the alerts browser checks against a throwaway copy of the data: simulated prices and forecasts, simulation mode, no SMS, real data untouched.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; PORT="${PORT:-8760}"
cp -R "$ROOT/backend/data/." "$TMP/"
export GOLD_DATA_DIR="$TMP" GOLD_ALERTS_MODE=simulate GOLD_EMBEDDED_WORKER=0
cd "$ROOT/backend"
PYTHONPATH=. .venv/bin/python - <<PY
import time
from app import alerts, alerts_sim, n8n_link, db
now = time.time()
alerts.save_settings({"position_status": "open"}, now)               # these checks exercise the entry-price comparisons, which only apply to an OPEN position (the closed default is covered by run_pages.sh)
alerts_sim.issue_sim_forecast(4120.0, now)                       # a simulated next-session forecast above both levels
alerts.check_forecasts(now)
alerts.check_price(now, quote={"price": 4101.0, "as_of": now - 120, "source": "simulated", "instrument": alerts.INSTRUMENT_NAME, "basis": "futures"})
n8n_link.deliver_pending(now)                                    # simulation: nothing is sent
n8n_link.push_snapshot(now, force=True, fetch_quote=False)
open("$TMP/token.txt", "w").write(db.rows("SELECT token FROM review_tokens ORDER BY created_at DESC LIMIT 1")[0]["token"])
PY
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$TMP/server.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for i in $(seq 1 40); do curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 0.5; done
cd "$ROOT/e2e"
URL="http://127.0.0.1:$PORT" TOKEN_FILE="$TMP/token.txt" OUT="${OUT:-./shots}" node verify_alerts.mjs
