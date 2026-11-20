"""Intraday "today's close" forecast.

The app's official daily close for gold futures (GC=F) is the COMEX settlement at ~13:30 America/New_York
(verified: hourly prices around 13:30 match the daily series to a median of ~$3; later or earlier hours do not).
This module forecasts that number from how the market is moving *right now*.

Method (kept deliberately honest):
  * Anchor = the latest futures price. Expected move = 0 unless the daily research lab (lab.py) has promoted an ML
    model that beat the "price stays where it is" baseline on held-out sessions.
  * Range = 10th/90th percentile of historical remaining-move errors, in units of recent hourly volatility x
    sqrt(hours left to settlement). It tightens as 13:30 approaches and widens when the market is jumpy.
  * Calibration coverage is measured on sessions the calibration never saw.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import httpx
import numpy as np
import pandas as pd

from . import config, quote

NY = "America/New_York"
SETTLE_H = 13.5  # COMEX gold settlement, hours after midnight New York time
MIN_LEFT, MAX_LEFT = 0.4, 24.0  # hours before settlement used for training rows
LEVEL = 0.80
FEATS = ["r_prev", "r_open", "r_1h", "r_3h", "r_6h", "vol_6h", "hrs_left", "d_vol60", "d_mom5", "d_mom20"]
HOURS_BUCKETS = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 6.0), (6.0, 12.0), (12.0, 99.0)]
BARS_TTL = 600  # seconds


class NowcastUnavailable(Exception):
    pass


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def fetch_hourly(client: httpx.Client | None = None) -> pd.DataFrame:
    """Two years of hourly GC=F bars (New York time) with the close of each bar."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{config.SYMBOL}"
    own = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        resp = client.get(url, params={"range": "730d", "interval": "1h", "includePrePost": "true"},
                          headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        resp.raise_for_status()
        res = ((resp.json().get("chart") or {}).get("result") or [None])[0]
    except (httpx.HTTPError, ValueError) as exc:
        raise NowcastUnavailable(f"Could not load intraday history: {exc.__class__.__name__}") from exc
    finally:
        if own:
            client.close()
    if not res or not res.get("timestamp"):
        raise NowcastUnavailable("Intraday history was empty.")
    ts = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(NY)
    bars = pd.DataFrame({"t": ts, "c": res["indicators"]["quote"][0]["close"]}).dropna().drop_duplicates("t")
    return bars.sort_values("t").reset_index(drop=True)


def session_label(ts: pd.Timestamp) -> pd.Timestamp | None:
    """Trading-session date of a New York timestamp. A session opens 18:00 the previous evening; weekends -> None."""
    label = (ts + pd.Timedelta(hours=6)).normalize().tz_localize(None)
    return label if label.dayofweek < 5 else None


def build_frame(bars: pd.DataFrame, daily: pd.Series) -> pd.DataFrame:
    """One row per hourly bar: features known at the bar's end, plus the target (log move to the session's official close).

    The live (unfinished) session is included with a NaN target.
    """
    b = bars.copy()
    b["asof"] = b["t"] + pd.Timedelta(hours=1)
    b["session"] = (b["asof"] + pd.Timedelta(hours=6)).dt.normalize().dt.tz_localize(None)
    b = b[b["session"].dt.dayofweek < 5].sort_values("asof").reset_index(drop=True)
    b["hrs_left"] = (b["session"].dt.tz_localize(NY) + pd.Timedelta(hours=SETTLE_H) - b["asof"]).dt.total_seconds() / 3600
    lp = np.log(b["c"])
    b["lp"] = lp
    b["first"] = b.groupby("session")["c"].transform("first")
    b["r_open"] = lp - np.log(b["first"])
    b["r_1h"], b["r_3h"], b["r_6h"] = lp - lp.shift(1), lp - lp.shift(3), lp - lp.shift(6)
    hourly_ret = lp.diff()
    b["vol_6h"], b["vol_24"] = hourly_ret.rolling(6).std(), hourly_ret.rolling(24).std()

    dl = np.log(daily.astype(float))
    ctx = pd.DataFrame({"prev_close": daily.astype(float), "d_vol60": dl.diff().rolling(60).std(),
                        "d_mom5": dl - dl.shift(5), "d_mom20": dl - dl.shift(20)})
    ctx.index.name = "dday"
    # Daily context strictly *before* the session (allow_exact_matches=False): known before the session starts.
    b = pd.merge_asof(b.sort_values("session"), ctx.reset_index(), left_on="session", right_on="dday",
                      allow_exact_matches=False, direction="backward").sort_values("asof").reset_index(drop=True)
    b["r_prev"] = b["lp"] - np.log(b["prev_close"])
    b["session_close"] = daily.reindex(b["session"]).to_numpy()
    b["target"] = np.log(b["session_close"]) - b["lp"]
    return b.replace([np.inf, -np.inf], np.nan)


def training_rows(frame: pd.DataFrame) -> pd.DataFrame:
    rows = frame[(frame["hrs_left"] >= MIN_LEFT) & (frame["hrs_left"] <= MAX_LEFT)]
    return rows.dropna(subset=FEATS + ["target", "vol_24"]).reset_index(drop=True)


def session_split(rows: pd.DataFrame, train_frac: float = 0.6, val_frac: float = 0.2):
    """Chronological split by whole sessions: (train_mask, val_mask, test_mask)."""
    sessions = np.array(sorted(rows["session"].unique()))
    v0, t0 = sessions[int(len(sessions) * train_frac)], sessions[int(len(sessions) * (train_frac + val_frac))]
    s = rows["session"]
    return (s < v0).to_numpy(), ((s >= v0) & (s < t0)).to_numpy(), (s >= t0).to_numpy()


# --------------------------------------------------------------------------- #
# Calibration (range + typical error by hours left)
# --------------------------------------------------------------------------- #
def calibrate(rows: pd.DataFrame) -> dict:
    train, val, test = session_split(rows)
    pre = train | val
    scale = rows["vol_24"].to_numpy() * np.sqrt(rows["hrs_left"].to_numpy())
    z = rows["target"].to_numpy() / scale
    lo_q, hi_q = (1 - LEVEL) / 2, 1 - (1 - LEVEL) / 2
    q_lo, q_hi = float(np.quantile(z[pre], lo_q)), float(np.quantile(z[pre], hi_q))
    inside = (z >= q_lo) & (z <= q_hi)
    coverage = float(np.mean(inside[test]) * 100)
    # Hourly rows inside one session share a single outcome, so they are NOT independent observations. The honest sample
    # size is the number of distinct sessions; resample whole sessions to get an interval.
    per_session = pd.Series(inside[test].astype(float)).groupby(rows.loc[test, "session"].to_numpy()).mean().to_numpy()
    rng = np.random.default_rng(0)
    boots = [float(rng.choice(per_session, len(per_session)).mean() * 100) for _ in range(2000)]
    price = rows["c"].to_numpy()
    err = np.abs(price * (np.exp(rows["target"].to_numpy()) - 1))  # $ miss of "close = price now"
    hl = rows["hrs_left"].to_numpy()
    table = []
    for lo, hi in HOURS_BUCKETS:
        k = test & (hl >= lo) & (hl < hi)
        if k.sum() >= 20:
            table.append({"from_h": lo, "to_h": hi, "typical_error_usd": float(np.median(err[k])), "n": int(k.sum())})
    return {"q_lo": q_lo, "q_hi": q_hi, "coverage_pct": coverage, "n_test": int(test.sum()),
            "n_sessions": int(len(per_session)), "coverage_by_session_pct": float(per_session.mean() * 100),
            "coverage_ci90_pct": [float(np.quantile(boots, 0.05)), float(np.quantile(boots, 0.95))],
            "test_period": {"start": rows.loc[test, "session"].min().date().isoformat(),
                            "end": rows.loc[test, "session"].max().date().isoformat()},
            "error_by_hours_left": table}


# --------------------------------------------------------------------------- #
# Live forecast
# --------------------------------------------------------------------------- #
@dataclass
class _State:
    built_at: float
    key: str
    frame: pd.DataFrame
    cal: dict
    models: dict          # algorithm -> fitted model (champion and shadows); empty values are never used for the baseline
    champion_algo: str


_lock = threading.RLock()
_state: _State | None = None


def _champion_algo() -> str:
    from . import registry
    return registry.champion("nowcast")["algorithm"]


def _build_state(daily: pd.Series) -> _State:
    from . import registry
    from .learning import make_intraday_model

    frame = build_frame(fetch_hourly(), daily)
    rows = training_rows(frame)
    if len(rows) < 2000:
        raise NowcastUnavailable("Not enough intraday history to forecast.")
    X, y = rows[FEATS].to_numpy(), rows["target"].to_numpy()  # matured targets only: unfinished sessions have no target
    models = {a: make_intraday_model(a).fit(X, y) for a in registry.NOWCAST_ALGOS}
    return _State(time.time(), f"{daily.index[-1].date()}|{len(daily)}", frame, calibrate(rows), models, _champion_algo())


def _typical_error(cal: dict, hrs: float) -> float | None:
    for row in cal["error_by_hours_left"]:
        if row["from_h"] <= hrs < row["to_h"]:
            return row["typical_error_usd"]
    return None


def target_session(tick_ts: pd.Timestamp) -> pd.Timestamp:
    """The session whose 13:30 ET settlement is the next one after ``tick_ts`` (skips weekends and exchange holidays)."""
    from .sessions import next_sessions, sessions_between

    sess = session_label(tick_ts)
    if sess is not None and len(sessions_between(sess, sess)) == 1 and tick_ts < sess.tz_localize(NY) + pd.Timedelta(hours=SETTLE_H):
        return sess
    return next_sessions(sess if sess is not None else tick_ts.tz_localize(None).normalize(), 1)[0]


def get(daily: pd.Series, force: bool = False) -> dict:
    """Forecast of the next official close (13:30 ET settlement) from the latest futures price."""
    global _state
    with _lock:
        key = f"{daily.index[-1].date()}|{len(daily)}"
        if force or _state is None or _state.key != key or time.time() - _state.built_at > BARS_TTL or _state.champion_algo != _champion_algo():
            _state = _build_state(daily)
        st = _state
    try:
        live = quote._fetch_yahoo()
    except quote.QuoteUnavailable as exc:
        raise NowcastUnavailable(str(exc)) from exc

    now = time.time()
    tick = live.get("tick_epoch") or now
    tick_ts = pd.Timestamp(tick, unit="s", tz="UTC").tz_convert(NY)
    price = float(live["price"])
    target = target_session(tick_ts)
    settle = target.tz_localize(NY) + pd.Timedelta(hours=SETTLE_H)
    hrs = (settle - tick_ts).total_seconds() / 3600
    scale_hrs = min(hrs, MAX_LEFT)  # weekends do not add trading time
    market_open = (now - tick) < 40 * 60
    sess_now = session_label(tick_ts)
    same_session = sess_now is not None and sess_now == target
    last_official = {"date": daily.index[-1].date().isoformat(), "close": round(float(daily.iloc[-1]), 2)}
    prev_close_available = daily.index[-1] < target and same_session  # previous official close is the right reference only before settlement
    base = {"symbol": config.SYMBOL, "instrument": "Gold futures (GC=F)", "anchor_price": round(price, 2), "anchor_time": tick_ts.isoformat(),
            "anchor_time_epoch": tick, "anchor_delay_seconds": int(max(0, now - tick)), "level": LEVEL,
            "target_date": target.date().isoformat(), "target_time": settle.isoformat(), "timezone": NY, "target_label": "13:30 ET COMEX settlement",
            "hours_left": round(hrs, 2), "minutes_to_target": int(max(0, (settle.timestamp() - now) / 60)),
            "phase": "pre_settlement" if same_session else "next_session", "market_open": market_open,
            "last_official_close": last_official,
            "reference": ({"kind": "previous_settlement", "label": f"previous settlement ({last_official['date']})", "price": last_official["close"]}
                          if prev_close_available else {"kind": "live_price", "label": "latest futures price", "price": round(price, 2)}),
            "calibration": {"coverage_pct": round(st.cal["coverage_pct"], 1), "n_rows": st.cal["n_test"], "n_sessions": st.cal["n_sessions"],
                            "coverage_by_session_pct": round(st.cal["coverage_by_session_pct"], 1),
                            "coverage_ci90_pct": [round(x, 1) for x in st.cal["coverage_ci90_pct"]], "period": st.cal["test_period"]}}

    last = st.frame.iloc[-1]
    vol24 = float(last["vol_24"]) if np.isfinite(last["vol_24"]) else float(st.frame["vol_24"].dropna().iloc[-1])
    s = vol24 * np.sqrt(max(scale_hrs, 0.05))

    # An intraday model is trained on states INSIDE a session (hours left until THAT session's settlement). Once today's 13:30 settlement has passed and the
    # next session has not opened, no such state exists for the next target: the model has no valid input, so it is UNAVAILABLE. It must never be given a
    # zero return and shown as an estimate (that is just the current price, i.e. the baseline).
    intraday_ok = same_session and pd.Timestamp(last["session"]) == target
    why_no_model = None if intraday_ok else (
        "The intraday models only forecast a settlement from inside its own trading session. The next session has not opened yet, so there is no valid input for them.")

    def predict(algo: str) -> float | None:
        if algo == "unchanged":
            return 0.0
        if not intraday_ok:
            return None
        x = last[FEATS].copy()
        x["r_prev"], x["r_open"] = np.log(price / last["prev_close"]), np.log(price / last["first"])
        x["hrs_left"] = scale_hrs
        return float(st.models[algo].predict(x.to_numpy(dtype=float).reshape(1, -1))[0]) if x.notna().all() else None

    def pack(algo: str) -> dict:
        mu = predict(algo)
        if mu is None:
            return {"algorithm": algo, "available": False, "reason": why_no_model or "The latest intraday inputs are incomplete."}
        return {"algorithm": algo, "available": True, "predicted_close": round(price * float(np.exp(mu)), 2),
                "low": round(price * float(np.exp(mu + st.cal["q_lo"] * s)), 2), "high": round(price * float(np.exp(mu + st.cal["q_hi"] * s)), 2),
                "expected_move_pct": round((float(np.exp(mu)) - 1) * 100, 3)}

    champ = st.champion_algo
    main = pack(champ)
    fallback = not main["available"]            # the champion is a model that cannot predict this target: the explicit baseline is shown instead
    if fallback:
        main = pack("unchanged")
    typical = _typical_error(st.cal, scale_hrs)
    cands = [pack(a) for a in st.models]
    return {**base, **{k: v for k, v in main.items() if k not in ("available", "algorithm")}, "baseline_forecast": pack("unchanged"),
            "status": "open" if market_open else "closed", "method": "baseline" if (champ == "unchanged" or fallback) else champ,
            "fallback_to_baseline": fallback, "intraday_models_available": intraday_ok, "intraday_unavailable_reason": why_no_model,
            "candidates": [c for c in cands if c["available"]], "unavailable_candidates": [c for c in cands if not c["available"]], "typical_error_usd": typical,
            "message": ("Reference estimate: assumes no change. No machine-learning model has earned promotion over 'price stays put'." if champ == "unchanged" else
                        (f"Reference estimate: assumes no change. The {champ} model cannot predict this target yet." if fallback else
                         f"Forecast = latest futures price adjusted by the {champ} model, which earned promotion."))
            + ("" if market_open else " The market is closed; the price shown is the last trade.")}
