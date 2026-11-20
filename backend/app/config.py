"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR.parent
# Optional local settings and secrets. .env is git-ignored; real environment variables take precedence.
load_dotenv(REPO_DIR / ".env", override=False)
DATA_DIR = Path(os.getenv("GOLD_DATA_DIR", BASE_DIR / "data"))

SYMBOL = os.getenv("GOLD_SYMBOL", "GC=F")
HISTORY_RANGE = os.getenv("GOLD_HISTORY_RANGE", "5y")
CACHE_TTL_SECONDS = int(os.getenv("GOLD_CACHE_TTL_SECONDS", str(15 * 60)))
OFFLINE = os.getenv("GOLD_OFFLINE", "").lower() in {"1", "true", "yes"}  # never call external providers; use cache/upload only
NEWS_TTL_SECONDS = int(os.getenv("GOLD_NEWS_TTL_SECONDS", str(15 * 60)))
HTTP_TIMEOUT = float(os.getenv("GOLD_HTTP_TIMEOUT", "12"))
USER_AGENT = os.getenv("GOLD_USER_AGENT", "Mozilla/5.0 (compatible; gold-prediction/0.1)")
CORS_ORIGINS = [o.strip() for o in os.getenv("GOLD_CORS_ORIGINS", "http://localhost:5183,http://127.0.0.1:5183").split(",") if o.strip()]

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MIN_ROWS = 500  # minimum daily observations for features + validation/calibration/holdout periods
MAX_ABS_DAILY_MOVE = 0.30  # a >30% one-day move in gold is treated as a data error
HORIZONS = (1, 5, 20)
# A daily bar is only a completed observation once its session has ended AND its close agrees with the settlement-time price.
# Measured over 48 sessions: the finalised GC=F daily close is within $1.50 of the 5-minute close at 13:25 ET (mean $0.48).
SESSION_END_ET = os.getenv("GOLD_SESSION_END_ET", "17:00" if os.getenv("GOLD_SYMBOL", "GC=F") == "GC=F" else "")
SETTLEMENT_TOLERANCE_USD = float(os.getenv("GOLD_SETTLEMENT_TOLERANCE", "2.0"))

# Optional: Alpaca market data for the GLD ETF comparison view (read-only; never a model input).
# Keys stay on the server: they are never returned by the API, logged, or included in error messages.
ALPACA_KEY_ID = os.getenv("ALPACA_API_KEY_ID", "").strip()
ALPACA_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY", "").strip()
ALPACA_DATA_FEED = (os.getenv("ALPACA_DATA_FEED", "iex").strip() or "iex").lower()

# Alerts. The SMS provider (Twilio) and the recipient phone number live ONLY in n8n; this backend never sees them.
# GOLD_ALERTS_MODE=simulate (default) never contacts n8n or sends an SMS. "live" needs the three N8N_* values below.
ALERTS_MODE = os.getenv("GOLD_ALERTS_MODE", "simulate").strip().lower()
N8N_WEBHOOK_BASE = os.getenv("N8N_WEBHOOK_BASE", "").strip().rstrip("/")    # e.g. https://you.app.n8n.cloud/webhook
N8N_AUTH_HEADER = os.getenv("N8N_AUTH_HEADER", "X-Gold-Secret").strip()
N8N_AUTH_SECRET = os.getenv("N8N_AUTH_SECRET", "").strip()                  # also entered once in n8n as a Header Auth credential
ALERT_REVIEW_BASE_URL = os.getenv("ALERT_REVIEW_BASE_URL", "").strip().rstrip("/")  # default: <N8N_WEBHOOK_BASE>/gold-review

# Optional shared secret for state-changing API calls (POST/PUT/DELETE). Without it, only loopback clients from this app's own origin may change anything.
# Use it (behind HTTPS) if the API is ever reachable from another machine. The browser UI does not send it, so a token-protected API is for scripts.
API_TOKEN = os.getenv("GOLD_API_TOKEN", "").strip()
