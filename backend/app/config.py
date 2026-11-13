"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("GOLD_DATA_DIR", BASE_DIR / "data"))

SYMBOL = os.getenv("GOLD_SYMBOL", "GC=F")
HISTORY_RANGE = os.getenv("GOLD_HISTORY_RANGE", "5y")
CACHE_TTL_SECONDS = int(os.getenv("GOLD_CACHE_TTL_SECONDS", str(6 * 3600)))
NEWS_TTL_SECONDS = int(os.getenv("GOLD_NEWS_TTL_SECONDS", str(15 * 60)))
HTTP_TIMEOUT = float(os.getenv("GOLD_HTTP_TIMEOUT", "12"))
USER_AGENT = os.getenv("GOLD_USER_AGENT", "Mozilla/5.0 (compatible; gold-prediction/0.1)")
CORS_ORIGINS = [o.strip() for o in os.getenv("GOLD_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MIN_ROWS = 400  # minimum daily observations needed to build features + walk-forward folds
MAX_ABS_DAILY_MOVE = 0.30  # a >30% one-day move in gold is treated as a data error
HORIZONS = (1, 5, 20)
