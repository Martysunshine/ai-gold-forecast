"""Forecast ledger: issue immutable forecasts, then score them when the real outcome exists.

A forecast is stored once, with its model version, feature schema, input cutoff and base price. It is never edited: a
retrained model issues NEW forecasts. Scoring writes one outcome row, only from a completed official close.
"""
from __future__ import annotations

import hashlib
import json
import time

import numpy as np
import pandas as pd

from . import db
from .research_drivers import _block_bootstrap_ci

INSTRUMENT = "futures"
TARGET_DEFS = {
    "nowcast": "Official COMEX gold futures (GC=F) daily settlement of the target session, forecast intraday (13:30 America/New_York)",
    "fwd": "Official COMEX gold futures (GC=F) daily settlement close, N sessions after the base session",
}


def target_def(task: str) -> str:
    return TARGET_DEFS["nowcast"] if task == "nowcast" else TARGET_DEFS["fwd"]


def dataset_hash(close: pd.Series, drivers_df: pd.DataFrame | None = None) -> str:
    """A short content hash of the exact inputs a forecast was trained on (the dataset snapshot): prices, and driver values when used."""
    h = hashlib.sha256(pd.util.hash_pandas_object(close.astype(float).round(6), index=True).values.tobytes())
    if drivers_df is not None and len(drivers_df):
        h.update(pd.util.hash_pandas_object(drivers_df.select_dtypes("number").round(8), index=True).values.tobytes())
    return h.hexdigest()[:16]


def forecast_id(task: str, role: str, version: str, base_time: str, target_date: str) -> str:
    return f"{task}|{role}|{version}|{base_time}|{target_date}"


def issue(*, task: str, role: str, version: str, algorithm: str, feature_schema: str, input_cutoff: float, base_price: float,
          base_time: str, target_date: str, target_time: str, predicted: float, low: float | None, high: float | None,
          level: float | None, horizon: int | None = None, data_key: str | None = None, hours_left: float | None = None,
          issued_at: float | None = None, dataset_hash: str | None = None) -> bool:
    """Insert a forecast. Returns False (and changes nothing) if this exact forecast already exists or if it would be
    issued at or after its own target moment: such a "forecast" is not out-of-sample evidence and is never logged."""
    if not np.isfinite(predicted) or not np.isfinite(base_price):
        raise ValueError("forecast values must be finite")
    when = issued_at or time.time()
    if input_cutoff > when + 1:
        raise ValueError("input cutoff is in the future of the issue time")
    if when >= pd.Timestamp(target_time).timestamp():
        return False
    cur = db.connect().execute(
        "INSERT OR IGNORE INTO forecasts (id,task,instrument,target_def,role,model_version,algorithm,feature_schema,issued_at,input_cutoff,"
        "base_price,base_time,target_date,target_time,horizon,predicted,low,high,level,data_key,hours_left,dataset_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (forecast_id(task, role, version, base_time, target_date), task, INSTRUMENT, target_def(task), role, version, algorithm, feature_schema,
         issued_at or time.time(), input_cutoff, float(base_price), base_time, target_date, target_time, horizon, float(predicted),
         None if low is None else float(low), None if high is None else float(high), level, data_key, hours_left, dataset_hash))
    if cur.rowcount == 1:
        return True
    _audit_conflict(forecast_id(task, role, version, base_time, target_date), data_key, dataset_hash, float(predicted))
    return False


def _audit_conflict(fid: str, data_key: str | None, dataset_hash: str | None, predicted: float) -> None:
    """The id is deterministic, so re-issuing from DIFFERENT inputs is ignored (the original stays, with its original timestamp) -- but never
    silently: the conflict is recorded so a revised dataset can be seen, and an older forecast is never presented under a newer timestamp."""
    old = db.rows("SELECT data_key, dataset_hash, predicted FROM forecasts WHERE id=?", (fid,))
    if not old:
        return
    o = old[0]
    # Only a different INPUT SNAPSHOT counts. A next-settlement re-issue within the same hour differs only because the quote ticked: that is the normal
    # "first forecast of the hour stands" rule, not a conflict.
    differs = (dataset_hash and o["dataset_hash"] and dataset_hash != o["dataset_hash"]) or (data_key and o["data_key"] and data_key != o["data_key"])
    if not differs:
        return
    detail = json.dumps({"kept": {"data_key": o["data_key"], "dataset_hash": o["dataset_hash"], "predicted": o["predicted"]},
                         "rejected_reissue": {"data_key": data_key, "dataset_hash": dataset_hash, "predicted": predicted}}, default=float)
    if not db.rows("SELECT 1 FROM ledger_audit WHERE forecast_id=? AND kind='snapshot_conflict' AND detail=?", (fid, detail)):
        db.connect().execute("INSERT INTO ledger_audit (at, forecast_id, kind, detail) VALUES (?,?,?,?)", (time.time(), fid, "snapshot_conflict", detail))


FALLBACK_KIND = "fallback_not_a_model"
NOT_FALLBACK = f"f.id NOT IN (SELECT forecast_id FROM ledger_audit WHERE kind='{FALLBACK_KIND}')"


def flag_fallbacks() -> int:
    """Record (never delete or edit) intraday-model rows that were NOT model output.

    Before the fix, an intraday model asked for the NEXT session's settlement (issued after today's 13:30 ET settlement and before the next session opened)
    silently returned a zero move, so its "forecast" was just the price at issue time, identical to the baseline. Those rows are flagged here and are
    excluded from scoring and from every model statistic. The rule is the code path itself: the tick's trading session is not the target session."""
    from .nowcast import NY, session_label

    rows = db.rows("SELECT f.id, f.input_cutoff, f.target_date FROM forecasts f WHERE f.task='nowcast' AND f.algorithm!='unchanged' AND " + NOT_FALLBACK)
    n = 0
    for r in rows:
        lab = session_label(pd.Timestamp(r["input_cutoff"], unit="s", tz="UTC").tz_convert(NY))
        if lab is None or lab.date().isoformat() != r["target_date"]:
            db.connect().execute("INSERT INTO ledger_audit (at, forecast_id, kind, detail) VALUES (?,?,?,?)",
                                 (time.time(), r["id"], FALLBACK_KIND, json.dumps({"reason": "intraday model has no valid input for a next-session target; the stored value is the issue-time price, not a model estimate"})))
            n += 1
    return n


def mature(daily: pd.Series, now: float | None = None) -> int:
    """Score every unscored forecast whose target session's official close is now in ``daily`` (completed bars only)."""
    now = now or time.time()
    flag_fallbacks()
    pending = db.rows(f"SELECT f.* FROM forecasts f LEFT JOIN outcomes o ON o.forecast_id=f.id WHERE o.forecast_id IS NULL AND {NOT_FALLBACK}")
    if not pending:
        return 0
    closes = {d.date().isoformat(): float(v) for d, v in daily.items()}
    n = 0
    with db.tx() as c:
        for f in pending:
            actual = closes.get(f["target_date"])
            if actual is None or f["issued_at"] >= pd.Timestamp(f["target_time"]).timestamp():
                continue  # unmatured, or issued too late to count as a forecast (never scored)
            err = f["predicted"] - actual
            in_range = None if f["low"] is None or f["high"] is None else int(f["low"] <= actual <= f["high"])
            c.execute("INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?,?,?)",
                      (f["id"], actual, now, err, abs(err), abs(f["base_price"] - actual), in_range))
            n += 1
    return n


def stats(task: str, version: str | None = None, role: str | None = None, since_issued: float | None = None) -> dict:
    """Forward (live-issued) performance from matured outcomes only."""
    q = ("SELECT f.id,f.issued_at,f.horizon,f.base_time,f.target_date,o.abs_error,o.baseline_abs_error,o.in_range,f.predicted,f.base_price,o.actual "
         "FROM forecasts f JOIN outcomes o ON o.forecast_id=f.id WHERE f.task=? AND " + NOT_FALLBACK)
    p: list = [task]
    if version:
        q += " AND f.model_version=?"
        p.append(version)
    if role:
        q += " AND f.role=?"
        p.append(role)
    if since_issued:
        q += " AND f.issued_at>=?"
        p.append(since_issued)
    q += " ORDER BY f.base_time, f.issued_at"
    r = db.rows(q, tuple(p))
    if not r:
        return {"n_matured": 0, "n_sessions": 0}
    df = pd.DataFrame(r)
    mae, base = float(df["abs_error"].mean()), float(df["baseline_abs_error"].mean())
    h = int(df["horizon"].dropna().iloc[0]) if df["horizon"].notna().any() else 1
    out = {"n_matured": int(len(df)), "n_sessions": int(df["target_date"].nunique()), "mae": mae, "baseline_mae": base,
           "mae_vs_baseline_pct": (mae / base - 1) * 100 if base > 0 else None,
           "coverage_pct": float(df["in_range"].dropna().mean() * 100) if df["in_range"].notna().any() else None,
           "first_target": df["target_date"].min(), "last_target": df["target_date"].max(), "horizon": h}
    if len(df) >= 8 and base > 0:
        # Outcomes overlap (adjacent sessions share price paths), so resample in blocks, not single forecasts.
        # Nowcasts issued the same day share one outcome: collapse to per-session means first.
        per = df.groupby("target_date")[["abs_error", "baseline_abs_error"]].mean().sort_index()
        diff = (per["abs_error"] - per["baseline_abs_error"]).to_numpy()
        lo, hi = _block_bootstrap_ci(diff, block=max(h, 10), reps=1000)
        out["mae_diff_ci90_pct"] = [lo / base * 100, hi / base * 100]
    return out


def effective_blocks(n_sessions: int, horizon: int) -> float:
    """Independent-ish outcome count: adjacent h-session forecasts overlap, so n/h blocks, not n."""
    return n_sessions / max(1, horizon)


def latest(task: str, role: str = "champion", horizon: int | None = None) -> dict | None:
    q = "SELECT * FROM forecasts WHERE task=? AND role=?"
    p: list = [task, role]
    if horizon is not None:
        q += " AND horizon=?"
        p.append(horizon)
    q += " ORDER BY issued_at DESC LIMIT 1"
    r = db.rows(q, tuple(p))
    return r[0] if r else None


def with_outcome(forecast_id_: str) -> dict | None:
    r = db.rows("SELECT * FROM outcomes WHERE forecast_id=?", (forecast_id_,))
    return r[0] if r else None
