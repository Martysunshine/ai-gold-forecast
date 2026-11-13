#!/usr/bin/env bash
# Browser end-to-end checks against the production build, served by FastAPI.
#   ./e2e/run.sh                 # uses the Chrome installed on this machine
#   CHROME_PATH=/path/to/chrome SHOTS=./docs/screenshots ./e2e/run.sh
# Starts three throw-away servers on ports 8743-8745: live data, offline with a seeded cache, offline with no data.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

[ -x backend/.venv/bin/uvicorn ] || { echo "Run ./start.sh once first (it creates backend/.venv)."; exit 1; }
[ -f frontend/dist/index.html ] || (cd frontend && npm run build --silent)
[ -d e2e/node_modules ] || (cd e2e && npm ci --no-audit --no-fund --loglevel=error)

if [ -z "${CHROME_PATH:-}" ]; then
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" "$(command -v google-chrome || true)" "$(command -v chromium || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && CHROME_PATH="$c" && break
  done
fi
[ -n "${CHROME_PATH:-}" ] || { echo "Chrome not found. Set CHROME_PATH."; exit 1; }
export CHROME_PATH

TMP="$(mktemp -d)"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT
mkdir -p "$TMP/live" "$TMP/empty"
for p in 8743 8744 8745; do
  if (echo > "/dev/tcp/127.0.0.1/$p") 2>/dev/null; then echo "Port $p is in use"; exit 1; fi
done
serve() { (cd "$ROOT/backend" && exec env "${@:2}" .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$1" >"$TMP/server-$1.log" 2>&1) & PIDS+=("$!"); }
wait_for() { for _ in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$1/api/health" && return 0; sleep 0.5; done; echo "server on $1 did not start"; exit 1; }

serve 8743 GOLD_DATA_DIR="$TMP/live"; wait_for 8743
curl -s -f -o /dev/null "http://127.0.0.1:8743/api/prices" || { echo "Live data fetch failed (needs internet access)."; exit 1; }
cp -r "$TMP/live" "$TMP/cached"
serve 8744 GOLD_OFFLINE=1 GOLD_DATA_DIR="$TMP/cached"; wait_for 8744
serve 8745 GOLD_OFFLINE=1 GOLD_DATA_DIR="$TMP/empty"; wait_for 8745

export LIVE=http://127.0.0.1:8743 CACHED=http://127.0.0.1:8744 EMPTY=http://127.0.0.1:8745
export OUT="${SHOTS:-$TMP/shots}"
(cd e2e && node run.mjs)
