"""Data quality: detect, record and (where it matters) quarantine. Never delete, never "fix" a price, never judge a big move by its size alone.

What "self-cleaning" means here:
  * Problems are detected from what is stored (the raw daily CSVs as written, the OHLC file, recorded spot ticks, the provider's error stream)
    and written to ``data_quarantine`` with the raw observation exactly as it was, so there is always an audit trail.
  * ``quarantined``  the observation/feed is excluded from use until it is resolved. ``flagged`` it is kept in use but needs a look.
    ``kept_after_check`` it was suspicious, was checked, and stays in use. ``resolved`` the condition is gone.
  * A LARGE MOVE is never removed for being large. It is checked (does the day's high agree? did the next session immediately reverse it?) and
    recorded as ``kept_after_check`` or, if it looks like a one-day spike that fully reverses, ``flagged`` -- the value still stays in the series.
  * Only a few kinds block training (``BLOCKING``): a provider schema change, prices in the wrong units, and a feed that stopped updating.
    A blocked learning step keeps the working models and forecasts untouched and says why.
  * It never edits source code, thresholds or promotion rules, and never touches the forecast ledger or past outcomes.
"""
from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import config, db

NY = ZoneInfo("America/New_York")
BLOCKING = {"schema_change", "unit_inconsistent", "stale_feed"}
PRICE_MIN, PRICE_MAX = 300.0, 20000.0       # USD per troy ounce: outside this is a unit/scale error, not a market
MAX_DAY_RATIO = 2.0                         # a close more than 2x (or less than half) the previous one cannot be a gold move
LARGE_MOVE = 0.05                           # daily moves beyond 5% are CHECKED (never removed for size)
REVERSAL = 0.8                              # a spike that gives back >= 80% of itself the next session is flagged for review
OHLC_TOL = 0.01
SOURCE_TOL = 2.5                            # USD: the same session's close in two stored views should agree
STALE_SESSIONS = 3                          # the feed is stale if it lags the latest completed session by this many sessions or more
SETTLE_GRACE = timedelta(minutes=30)


@dataclass
class Finding:
    source: str
    kind: str
    key: str
    detail: str
    raw: dict | list | str | None = None
    status: str = "quarantined"
    extra: dict = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------------------------------------ audits
def audit_daily_text(text: str, source: str) -> list[Finding]:
    """Audit a Date,Close CSV exactly as written: duplicates, order, non-numeric / non-positive prices, unit scale, large moves."""
    out: list[Finding] = []
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return out
    head = [h.strip().lower() for h in rows[0]]
    if "date" not in head or "close" not in head:
        return [Finding(source, "schema_change", "columns", f"expected Date and Close columns, found {rows[0]}", rows[0])]
    di, ci = head.index("date"), head.index("close")
    seen: dict[str, int] = {}
    prev_date, parsed = None, []
    for n, r in enumerate(rows[1:], start=2):
        if len(r) <= max(di, ci):
            out.append(Finding(source, "schema_change", f"row{n}", f"row {n} has {len(r)} fields", r))
            continue
        d, raw_p = r[di].strip(), r[ci].strip()
        if d in seen:
            out.append(Finding(source, "duplicate_date", d, f"{d} appears on rows {seen[d]} and {n}", r))
            continue
        seen[d] = n
        if prev_date is not None and d < prev_date:
            out.append(Finding(source, "out_of_order", d, f"{d} (row {n}) comes after {prev_date}", r))
        prev_date = max(prev_date or d, d)
        try:
            p = float(raw_p)
        except ValueError:
            out.append(Finding(source, "non_numeric", d, f"price {raw_p!r} is not a number", r))
            continue
        if not np.isfinite(p) or p <= 0:
            out.append(Finding(source, "non_positive", d, f"price {p} is not strictly positive", r))
            continue
        parsed.append((d, p, r))
    for i, (d, p, r) in enumerate(parsed):
        if not (PRICE_MIN <= p <= PRICE_MAX):
            out.append(Finding(source, "unit_inconsistent", d, f"{p} USD/oz is outside the plausible range {PRICE_MIN:.0f}-{PRICE_MAX:.0f}: wrong units or scale?", r))
        elif i and (p / parsed[i - 1][1] > MAX_DAY_RATIO or p / parsed[i - 1][1] < 1 / MAX_DAY_RATIO):
            out.append(Finding(source, "unit_inconsistent", d, f"close is {p / parsed[i - 1][1]:.2f}x the previous session: not a market move", r))
    return out


def large_moves(close: pd.Series, high: pd.Series | None, source: str) -> list[Finding]:
    """Check every >5% daily move instead of discarding it. The move stays in the series either way."""
    out: list[Finding] = []
    c = close.astype(float)
    ret = c.pct_change()
    idx = list(c.index)
    for i in np.where(ret.abs() > LARGE_MOVE)[0]:
        d = idx[i]
        key = d.date().isoformat()
        checks, spike = [], False
        if high is not None and d in high.index and np.isfinite(high.loc[d]):
            ok = c.loc[d] <= high.loc[d] + OHLC_TOL
            checks.append(f"the day's high {high.loc[d]:.2f} {'agrees with' if ok else 'is BELOW'} the close")
        if i + 1 < len(c):
            nxt = float(c.iloc[i + 1] / c.iloc[i] - 1)
            back = -nxt / float(ret.iloc[i]) if ret.iloc[i] else 0.0
            spike = back >= REVERSAL
            checks.append(f"next session moved {nxt:+.2%} ({'reversed' if spike else 'did not reverse'} {back:.0%} of it)")
        else:
            checks.append("no later session yet to confirm it")
        f = Finding(source, "large_move", key, f"{float(ret.iloc[i]):+.2%} in one session ({c.iloc[i - 1]:.2f} -> {c.iloc[i]:.2f}). " + "; ".join(checks) + ". Kept in the series.",
                    {"date": key, "close": float(c.iloc[i]), "previous": float(c.iloc[i - 1])}, "flagged" if spike else "kept_after_check")
        out.append(f)
    return out


def audit_ohlc(ohlc: pd.DataFrame, close: pd.Series, source: str) -> list[Finding]:
    """The stored high/close file: a high below the close is impossible; the same session's close should agree with the main series."""
    out: list[Finding] = []
    for d, r in ohlc.iterrows():
        key = d.date().isoformat()
        if not (np.isfinite(r["high"]) and np.isfinite(r["close"])) or r["high"] <= 0 or r["high"] < r["close"] - OHLC_TOL:
            out.append(Finding(source, "invalid_ohlc", key, f"high {r['high']} vs close {r['close']}", {"high": float(r["high"]), "close": float(r["close"])}))
        elif d in close.index and abs(float(close.loc[d]) - float(r["close"])) > SOURCE_TOL:
            out.append(Finding(source, "source_disagreement", key, f"close {r['close']:.2f} here vs {float(close.loc[d]):.2f} in the main series", {"ohlc_close": float(r["close"]), "series_close": float(close.loc[d])}, "flagged"))
    return out


def audit_ticks(rows: list[dict]) -> list[Finding]:
    """Recorded spot ticks: non-positive prices and jumps that gold cannot make in seconds (> 3% between consecutive ticks)."""
    out: list[Finding] = []
    prev = None
    for r in rows:
        if not (r["price"] > 0):
            out.append(Finding("spot_ticks", "non_positive", str(r["ts"]), f"tick price {r['price']}", dict(r)))
        elif prev and abs(r["price"] / prev["price"] - 1) > 0.03:
            out.append(Finding("spot_ticks", "large_move", str(r["ts"]), f"{r['price'] / prev['price'] - 1:+.2%} between consecutive ticks ({prev['price']} -> {r['price']}). Kept; not used by any model.", dict(r), "kept_after_check"))
        prev = r
    return out


def expected_last_session(now: datetime | None = None) -> pd.Timestamp:
    """The latest session whose settlement should be available: ended 17:00 ET + a grace, per the exchange calendar (not 'yesterday')."""
    from . import sessions

    now = (now or datetime.now(NY)).astimezone(NY)
    days = sessions.sessions_between(pd.Timestamp(now.date()) - pd.Timedelta(days=12), pd.Timestamp(now.date()))
    for d in reversed(list(days)):
        end = datetime(d.year, d.month, d.day, 17, 0, tzinfo=NY) + SETTLE_GRACE
        if now >= end:
            return pd.Timestamp(d)
    return pd.Timestamp(days[0])


def staleness(series: pd.Series, now: datetime | None = None) -> dict:
    from . import sessions

    exp = expected_last_session(now)
    last = series.index[-1]
    behind = max(0, len(sessions.sessions_between(last + pd.Timedelta(days=1), exp)))
    return {"expected_last_session": exp.date().isoformat(), "data_through": last.date().isoformat(), "sessions_behind": behind, "stale": behind >= STALE_SESSIONS}


def missing_sessions(series: pd.Series, window: int = 60) -> list[Finding]:
    from . import sessions

    v = sessions.verify(series.iloc[-250:])
    miss = [d for d in v["rule_sessions_without_bar"] if d >= (series.index[-1] - pd.Timedelta(days=window * 1.5)).date().isoformat()]
    return [Finding("daily", "missing_session", d, f"the calendar has a session on {d} but the provider has no bar for it", None, "flagged") for d in miss]


def classify_provider_error(source: str, message: str) -> Finding | None:
    """Turn the provider's error text into a finding. Network trouble and rate limits are transient (job retries handle them), not data-quality events."""
    m = (message or "").lower()
    if any(w in m for w in ("malformed", "unreadable", "unexpected", "schema")):
        return Finding(source, "schema_change", datetime.now(NY).date().isoformat(), f"the provider's response no longer matches what the parser expects: {message[:200]}", message)
    if "failed validation" in m or "no data" in m:
        return Finding(source, "provider_data_rejected", datetime.now(NY).date().isoformat(), f"provider data was rejected by validation: {message[:200]}", message, "flagged")
    return None


# ------------------------------------------------------------------------------------------------------------------------------ records
def record(f: Finding, now: float | None = None) -> bool:
    """Insert once per (source, kind, key). Returns True if new. The raw observation is stored verbatim; nothing is ever updated except status -> resolved."""
    cur = db.connect().execute("INSERT OR IGNORE INTO data_quarantine (detected_at, source, kind, key, detail, raw, status) VALUES (?,?,?,?,?,?,?)",
                               (now or time.time(), f.source, f.kind, f.key, f.detail, None if f.raw is None else json.dumps(f.raw, default=str), f.status))
    return cur.rowcount == 1


def resolve(source: str, kind: str, keep_keys: set[str] | None = None) -> int:
    """Mark open findings of a transient kind resolved when the condition has gone (keys still present stay open)."""
    q = "UPDATE data_quarantine SET status='resolved' WHERE source=? AND kind=? AND status IN ('quarantined','flagged')"
    p: list = [source, kind]
    if keep_keys:
        q += f" AND key NOT IN ({','.join('?' * len(keep_keys))})"
        p += sorted(keep_keys)
    return db.connect().execute(q, tuple(p)).rowcount


def open_blocking() -> list[dict]:
    q = ",".join("?" * len(BLOCKING))
    return db.rows(f"SELECT source, kind, key, detail FROM data_quarantine WHERE kind IN ({q}) AND status='quarantined'", tuple(sorted(BLOCKING)))


def ok_to_train() -> tuple[bool, list[dict]]:
    b = open_blocking()
    return (not b), b


def provider_error(source: str, message: str) -> None:
    f = classify_provider_error(source, message)
    if f:
        record(f)


def provider_ok(source: str) -> None:
    resolve(source, "schema_change")
    resolve(source, "provider_data_rejected")


def _read(p: Path) -> str | None:
    try:
        return p.read_text()
    except OSError:
        return None


def run(now: datetime | None = None) -> dict:
    """Audit everything stored and record new findings. Returns counts and whether training may proceed."""
    from .data import DataStore

    found: list[Finding] = []
    for name in ("provider.csv", "active.csv"):
        t = _read(config.DATA_DIR / name)
        if t:
            found += audit_daily_text(t, name)
    series = None
    try:
        ds = DataStore().get()
        series = ds.series
    except Exception:  # noqa: BLE001 - no data at all is reported by the staleness/availability paths, not here
        series = None
    stale = None
    if series is not None and len(series):
        high = None
        ohlc_text = _read(config.DATA_DIR / "gc_daily_ohlc.csv")
        if ohlc_text:
            o = pd.read_csv(io.StringIO(ohlc_text), parse_dates=["date"]).set_index("date")
            found += audit_ohlc(o, series, "gc_daily_ohlc.csv")
            high = o["high"]
        found += large_moves(series, high, "daily")
        found += missing_sessions(series)
        stale = staleness(series, now)
        if stale["stale"]:
            found.append(Finding("daily", "stale_feed", "daily", f"the data ends {stale['data_through']} but the latest completed session is {stale['expected_last_session']} ({stale['sessions_behind']} sessions behind)", stale))
        else:
            resolve("daily", "stale_feed")
    ticks = db.rows("SELECT ts, price FROM spot_ticks ORDER BY ts DESC LIMIT 5000")[::-1]
    found += audit_ticks(ticks)
    new = sum(1 for f in found if record(f))
    ok, blocking = ok_to_train()
    return {"checked_at": time.time(), "new_findings": new, "ok_to_train": ok, "blocking": blocking, "staleness": stale,
            "open": {r["kind"]: r["n"] for r in db.rows("SELECT kind, COUNT(*) AS n FROM data_quarantine WHERE status IN ('quarantined','flagged') GROUP BY kind")}}


def summary(limit: int = 12) -> dict:
    return {"by_status": {r["status"]: r["n"] for r in db.rows("SELECT status, COUNT(*) AS n FROM data_quarantine GROUP BY status")},
            "by_kind": {r["kind"]: r["n"] for r in db.rows("SELECT kind, COUNT(*) AS n FROM data_quarantine GROUP BY kind")},
            "blocking": open_blocking(),
            "recent": db.rows("SELECT detected_at, source, kind, key, detail, status FROM data_quarantine ORDER BY id DESC LIMIT ?", (limit,))}
