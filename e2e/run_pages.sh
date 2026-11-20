#!/usr/bin/env bash
# Browser checks for the Learning, Paper trading and Alerts & settings pages against a THROWAWAY copy of the data.
# A simulated forecast and a simulated trade are seeded into the copy (clearly synthetic, never in the real database); real data is untouched.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; PORT="${PORT:-8761}"
cp -R "$ROOT/backend/data/." "$TMP/"
export GOLD_DATA_DIR="$TMP" GOLD_ALERTS_MODE=simulate GOLD_EMBEDDED_WORKER=0
cd "$ROOT/backend"
PYTHONPATH=. .venv/bin/python - <<'PY'
import time
from app import db, ledger, paper
now = time.time()
t = time.strftime("%Y-%m-%dT%H:%M:%S-05:00", time.gmtime(now + 6 * 86400))
ledger.issue(task="fwd_5", role="champion", version="fwd_5:random_forest:gold_v1", algorithm="random_forest", feature_schema="gold_v1", input_cutoff=now - 3600,
             base_price=3750.0, base_time="2099-SIMULATED", target_date=t[:10], target_time=t, predicted=3750.0 * 1.006, low=3600.0, high=3900.0, level=0.8, horizon=5,
             data_key="simulated", issued_at=now - 30)
b = paper.current_book() or paper.new_book()
paper.evaluate(b["id"], {"price": 3750.0, "as_of": now}, now)           # a simulated entry from the simulated forecast
paper.evaluate(b["id"], {"price": 3762.0, "as_of": now + 120}, now + 120)
print("seeded fills:", len(paper.fills(b["id"])))
PY
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$TMP/server.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for i in $(seq 1 40); do curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 0.5; done
cd "$ROOT/e2e"
URL="http://127.0.0.1:$PORT" OUT="${OUT:-./shots}" node verify_pages.mjs
