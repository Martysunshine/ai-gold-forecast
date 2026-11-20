"""SQLite store shared by the web app and the background worker (WAL mode, safe for concurrent readers + one writer).

Integrity rules are enforced by the database itself, not by convention:
  * ``forecasts`` rows can never be updated or deleted (triggers abort the statement).
  * ``outcomes`` rows are write-once: a forecast is scored exactly once, against the data that exists when it matures.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_ticks (
  ts REAL PRIMARY KEY,            -- provider's own update time (epoch seconds)
  price REAL NOT NULL,
  retrieved REAL NOT NULL         -- when this app fetched it
);

CREATE TABLE IF NOT EXISTS forecasts (
  id TEXT PRIMARY KEY,            -- deterministic: task|role|version|base|target (re-issuing the same forecast is a no-op)
  task TEXT NOT NULL,             -- nowcast | fwd_1 | fwd_5 | fwd_20
  instrument TEXT NOT NULL,       -- futures (GC=F)
  target_def TEXT NOT NULL,       -- exact definition of what is being predicted
  role TEXT NOT NULL,             -- champion | shadow | baseline
  model_version TEXT NOT NULL,
  algorithm TEXT NOT NULL,
  feature_schema TEXT NOT NULL,
  issued_at REAL NOT NULL,
  input_cutoff REAL NOT NULL,     -- epoch seconds: no input newer than this was used
  base_price REAL NOT NULL,
  base_time TEXT NOT NULL,        -- date (daily tasks) or ISO time (nowcast) of the base price
  target_date TEXT NOT NULL,      -- session date whose official close is the target
  target_time TEXT NOT NULL,      -- ISO time with timezone of the target moment
  horizon INTEGER,
  predicted REAL NOT NULL,
  low REAL, high REAL, level REAL,
  data_key TEXT,
  hours_left REAL
);
CREATE INDEX IF NOT EXISTS ix_forecasts_task_target ON forecasts(task, target_date);
CREATE TRIGGER IF NOT EXISTS forecasts_no_update BEFORE UPDATE ON forecasts
  BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecasts_no_delete BEFORE DELETE ON forecasts
  BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;

CREATE TABLE IF NOT EXISTS outcomes (
  forecast_id TEXT PRIMARY KEY REFERENCES forecasts(id),
  actual REAL NOT NULL,
  scored_at REAL NOT NULL,
  error REAL NOT NULL,            -- predicted - actual
  abs_error REAL NOT NULL,
  baseline_abs_error REAL NOT NULL,  -- |base price - actual|: what "unchanged" would have missed by
  in_range INTEGER                -- 1 / 0 / NULL when no range
);
CREATE TRIGGER IF NOT EXISTS outcomes_no_update BEFORE UPDATE ON outcomes
  BEGIN SELECT RAISE(ABORT, 'outcomes are write-once'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_delete BEFORE DELETE ON outcomes
  BEGIN SELECT RAISE(ABORT, 'outcomes are write-once'); END;

CREATE TABLE IF NOT EXISTS models (
  version TEXT PRIMARY KEY,       -- e.g. fwd_5:random_forest:gold_v1
  task TEXT NOT NULL,
  algorithm TEXT NOT NULL,
  feature_schema TEXT NOT NULL,
  status TEXT NOT NULL,           -- champion | challenger | retired | rejected
  created_at REAL NOT NULL,
  status_changed_at REAL NOT NULL,
  last_trained_at REAL,
  trained_through TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  task TEXT NOT NULL,
  kind TEXT NOT NULL,             -- evaluation | promotion | rollback | seed
  champion_before TEXT,
  champion_after TEXT,
  decision TEXT NOT NULL,         -- retain | promote | rollback | seed
  reason TEXT NOT NULL,
  evidence TEXT                   -- JSON
);

CREATE TABLE IF NOT EXISTS dev_evals (
  data_through TEXT NOT NULL,
  task TEXT NOT NULL,
  version TEXT NOT NULL,
  evaluated_at REAL NOT NULL,
  metrics TEXT NOT NULL,          -- JSON of walk-forward development metrics
  PRIMARY KEY (data_through, task, version)
);

CREATE TABLE IF NOT EXISTS news_items (
  article_id TEXT PRIMARY KEY,    -- hash of the normalised link
  title_key TEXT NOT NULL,        -- normalised title for syndication de-duplication
  title TEXT NOT NULL,
  source TEXT,
  link TEXT NOT NULL,
  published_at TEXT,              -- the publisher's original timestamp (ISO, UTC) - never overwritten
  first_seen_at REAL NOT NULL,    -- when this app first saw it
  duplicate_of TEXT
);
CREATE INDEX IF NOT EXISTS ix_news_title ON news_items(title_key);

CREATE TABLE IF NOT EXISTS job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL,           -- running | ok | error | skipped
  detail TEXT
);
-- ---------------------------------------------------------------------------------------------------------------------------
-- Alerts (kept separate from model training: nothing here is ever a training input)
-- ---------------------------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,            -- JSON
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_state (
  key TEXT PRIMARY KEY,           -- e.g. price:futures:4100 (armed / episode / last session) or last_forecast_check
  value TEXT NOT NULL,            -- JSON
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedupe_key TEXT NOT NULL UNIQUE,   -- instrument|threshold|target_session|alert_type|episode  (one alert per key, ever)
  alert_type TEXT NOT NULL,          -- forecast_headsup | price_crossing
  instrument TEXT NOT NULL,
  threshold REAL NOT NULL,
  target_session TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,          -- after this the SMS is not sent (a late "alert" would read as new)
  message TEXT NOT NULL,
  payload TEXT NOT NULL,             -- JSON: the exact numbers behind the message
  sms_state TEXT NOT NULL            -- not_requested | pending | simulated | handed_off | accepted | delivered | undelivered | failed | expired
);
CREATE INDEX IF NOT EXISTS ix_alert_events_created ON alert_events(created_at);

CREATE TABLE IF NOT EXISTS alert_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER REFERENCES alert_events(id),   -- NULL for the morning outlook
  kind TEXT NOT NULL,                -- immediate | morning
  created_at REAL NOT NULL,
  mode TEXT NOT NULL,                -- simulate | live
  state TEXT NOT NULL,               -- simulated | accepted | delivered | undelivered | failed
  provider_sid TEXT,                 -- the SMS provider's message id (accepted != delivered)
  accepted_at REAL,
  delivered_at REAL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_alert_deliveries_sid ON alert_deliveries(provider_sid);

CREATE TABLE IF NOT EXISTS review_tokens (
  token TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  event_id INTEGER,
  kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  topic TEXT NOT NULL,               -- forecast_check | price_check | handoff | sync | settings
  outcome TEXT NOT NULL,             -- short machine word: checked | fired | duplicate | stale | off | error ...
  detail TEXT
);

-- Experimental touch-probability forward record: predictions are written BEFORE the outcome exists and never edited.
CREATE TABLE IF NOT EXISTS touch_forecasts (
  id TEXT PRIMARY KEY,               -- origin_date|level|model
  issued_at REAL NOT NULL,
  origin_date TEXT NOT NULL,
  target_date TEXT NOT NULL,         -- the next daily bar whose high defines the event
  level REAL NOT NULL,
  z REAL NOT NULL,                   -- distance to the level in daily-volatility units at issue
  probability REAL NOT NULL,
  model TEXT NOT NULL,
  displayed INTEGER NOT NULL,        -- 1 only if the evidence gate passed when issued
  event_def TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS touch_forecasts_no_update BEFORE UPDATE ON touch_forecasts BEGIN SELECT RAISE(ABORT, 'touch forecasts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS touch_forecasts_no_delete BEFORE DELETE ON touch_forecasts BEGIN SELECT RAISE(ABORT, 'touch forecasts are immutable'); END;
CREATE TABLE IF NOT EXISTS touch_outcomes (
  forecast_id TEXT PRIMARY KEY REFERENCES touch_forecasts(id),
  actual_high REAL NOT NULL,
  hit INTEGER NOT NULL,
  scored_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS touch_outcomes_no_update BEFORE UPDATE ON touch_outcomes BEGIN SELECT RAISE(ABORT, 'outcomes are write-once'); END;
CREATE TRIGGER IF NOT EXISTS touch_outcomes_no_delete BEFORE DELETE ON touch_outcomes BEGIN SELECT RAISE(ABORT, 'outcomes are write-once'); END;
-- ---------------------------------------------------------------------------------------------------------------------------
-- Operations: durable job state, daily-cycle records, data quarantine, every training attempt, ledger audit
-- ---------------------------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_state (
  job TEXT PRIMARY KEY,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_attempt_at REAL, last_success_at REAL, last_error TEXT,
  next_retry_at REAL,                -- no attempt before this moment after repeated failures (bounded exponential back-off)
  gave_up INTEGER NOT NULL DEFAULT 0 -- retry limit reached: skipped until the next scheduled cycle resets it
);
CREATE TABLE IF NOT EXISTS cycle_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at REAL NOT NULL, finished_at REAL,
  trigger TEXT NOT NULL,             -- scheduled | catch-up | session | manual
  runtime TEXT, data_through TEXT,
  steps TEXT NOT NULL,               -- JSON: ordered step results (collect, validate, score, learn, ...)
  status TEXT NOT NULL               -- running | ok | partial | failed
);
CREATE TABLE IF NOT EXISTS data_quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  detected_at REAL NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
  detail TEXT NOT NULL, raw TEXT,    -- the raw observation exactly as received (never altered or deleted)
  status TEXT NOT NULL,              -- quarantined (excluded) | flagged (kept, needs review) | kept_after_check | resolved
  UNIQUE (source, kind, key)
);
CREATE TABLE IF NOT EXISTS training_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL, task TEXT NOT NULL, version TEXT NOT NULL,
  stage TEXT NOT NULL,               -- dev_eval | forecast_fit | feature_group
  status TEXT NOT NULL,              -- ok | failed
  data_through TEXT, duration_s REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS ledger_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL, forecast_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dev_series (   -- walk-forward predicted vs actual for the champion (DEVELOPMENT evidence, never forward results)
  data_through TEXT NOT NULL, task TEXT NOT NULL, version TEXT NOT NULL, series TEXT NOT NULL,
  PRIMARY KEY (data_through, task, version)
);
CREATE TABLE IF NOT EXISTS feature_evals (
  data_through TEXT NOT NULL, group_name TEXT NOT NULL, evaluated_at REAL NOT NULL, result TEXT NOT NULL,
  PRIMARY KEY (data_through, group_name)
);

-- ---------------------------------------------------------------------------------------------------------------------------
-- Paper trading (simulation only: nothing in this schema or its module can reach a broker)
-- ---------------------------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_books (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, created_at REAL NOT NULL,
  status TEXT NOT NULL,              -- active | paused | archived
  paused_reason TEXT, archived_at REAL,
  initial_cash REAL NOT NULL,
  params TEXT NOT NULL,              -- JSON: risk limits and cost assumptions frozen for this book
  baseline_price REAL, baseline_at REAL   -- first mark: the start of the buy-and-hold comparison
);
CREATE TABLE IF NOT EXISTS paper_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER NOT NULL REFERENCES paper_books(id),
  ts REAL NOT NULL, side TEXT NOT NULL,          -- buy | sell
  qty REAL NOT NULL, price REAL NOT NULL, cost REAL NOT NULL,
  forecast_id TEXT,                              -- the immutable forecast that justified the entry
  rationale TEXT NOT NULL, reason TEXT NOT NULL, -- reason: entry | target | stop | risk | manual
  order_key TEXT NOT NULL,
  UNIQUE (book_id, order_key)                    -- a decision is posted at most once
);
CREATE TABLE IF NOT EXISTS paper_equity (
  book_id INTEGER NOT NULL REFERENCES paper_books(id), ts REAL NOT NULL,
  cash REAL NOT NULL, qty REAL NOT NULL, mark REAL NOT NULL, equity REAL NOT NULL,
  PRIMARY KEY (book_id, ts)
);
CREATE TABLE IF NOT EXISTS paper_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER, ts REAL NOT NULL,
  kind TEXT NOT NULL,                            -- signal | blocked | pause | resume | reset | new_book | error
  detail TEXT NOT NULL, forecast_id TEXT
);
"""

_MIGRATIONS = ("ALTER TABLE forecasts ADD COLUMN dataset_hash TEXT",)

_local = threading.local()
_init_lock = threading.Lock()
_inited: set[str] = set()


def path():
    return config.DATA_DIR / "gold.db"


def connect() -> sqlite3.Connection:
    """A per-thread connection (autocommit; use ``tx()`` for multi-statement atomicity)."""
    p = str(path())
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == p:
        return conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with _init_lock:
        if p not in _inited:
            conn.executescript(SCHEMA)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(forecasts)").fetchall()}
            if "dataset_hash" not in cols:            # additive migration: older rows keep NULL ("not recorded"), nothing is rewritten
                conn.execute(_MIGRATIONS[0])
            _inited.add(p)
    _local.conn, _local.path = conn, p
    return conn


@contextmanager
def tx():
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def rows(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in connect().execute(sql, params).fetchall()]
