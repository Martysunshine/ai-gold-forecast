#!/usr/bin/env bash
# One command to install (first run only), build and launch the whole app.
#   ./start.sh            -> http://127.0.0.1:8742
#   PORT=9000 ./start.sh  -> choose another port
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8742}"

command -v python3 >/dev/null || { echo "python3 (3.11+) is required"; exit 1; }
command -v npm >/dev/null || { echo "Node.js 20+ and npm are required"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11+ is required"; exit 1; }
if (echo > "/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "Port $PORT is already in use. Run: PORT=<free port> ./start.sh"; exit 1
fi

if [ ! -x backend/.venv/bin/uvicorn ]; then
  echo "==> Installing backend dependencies"
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q -r backend/requirements.txt
fi
if [ ! -d frontend/node_modules ]; then
  echo "==> Installing frontend dependencies"
  (cd frontend && npm ci --no-audit --no-fund --loglevel=error)
fi
if [ ! -f frontend/dist/index.html ] || [ -n "$(find frontend/src frontend/index.html -newer frontend/dist/index.html -print -quit)" ]; then
  echo "==> Building frontend"
  (cd frontend && npm run build --silent)
fi

echo "==> Gold Outlook running at http://127.0.0.1:$PORT  (Ctrl+C to stop)"
cd backend && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
