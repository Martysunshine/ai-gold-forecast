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
USER_AGENT = os.getenv("GOLD_USER_AGENT", "Mozilla/5.0 (compatible; ai-gold-forecast/0.2)")
CORS_ORIGINS = [o.strip() for o in os.getenv("GOLD_CORS_ORIGINS", "http://localhost:5183,http://127.0.0.1:5183").split(",") if o.strip()]

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MIN_ROWS = 500  # minimum daily observations for features + validation/calibration/holdout periods
MAX_ABS_DAILY_MOVE = 0.30  # a >30% one-day move in gold is treated as a data error
HORIZONS = (1, 5, 20)

# Optional: Alpaca market data for the GLD ETF comparison view (read-only; never a model input).
# Keys stay on the server: they are never returned by the API, logged, or included in error messages.
ALPACA_KEY_ID = os.getenv("ALPACA_API_KEY_ID", "").strip()
ALPACA_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY", "").strip()
ALPACA_DATA_FEED = (os.getenv("ALPACA_DATA_FEED", "iex").strip() or "iex").lower()
