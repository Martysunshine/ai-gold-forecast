"""Chart data and price summary for the two instruments. Every candle is a real observation; nothing is manufactured.

Instruments (never mixed):
  futures  COMEX gold futures GC=F via Yahoo Finance. Real OHLC at every range. Quotes are ~10 minutes delayed.
           Session = 18:00 ET (previous evening) to 17:00 ET. "Previous completed close" = the official 13:30 ET settlement.
  spot     Spot gold XAU/USD, real-time price from gold-api.com. No keyless provider offers spot OHLC history, so candles are
           built from price ticks this app has RECORDED itself; they begin when recording began and gaps stay gaps.
           Session = FX day, 17:00 ET to 17:00 ET.
"""
from __future__ import annotations

import threading
import time

import httpx
import numpy as np
import pandas as pd

from . import config, db

NY = "America/New_York"
# range -> (Yahoo range, Yahoo interval, seconds per bar for spot aggregation, span seconds for spot)
RANGES = {
    "1D": ("1d", "5m", 300, 86400),
    "1W": ("5d", "30m", 1800, 7 * 86400),
    "1M": ("1mo", "1h", 3600, 31 * 86400),
    "3M": ("3mo", "1d", 86400, 92 * 86400),
    "1Y": ("1y", "1d", 86400, 366 * 86400),
    "5Y": ("5y", "1wk", 7 * 86400, 5 * 366 * 86400),
}
DAILY_INTERVALS = {"1d", "1wk"}
_cache: dict = {}
_lock = threading.Lock()
CHART_TTL = 20


class MarketUnavailable(Exception):
    pass


def _chart(range_: str, interval: str) -> dict:
    key = (range_, interval)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CHART_TTL:
            return hit[1]
    try:
        r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{config.SYMBOL}",
                      params={"range": range_, "interval": interval, "includePrePost": "true"},
                      headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"}, timeout=config.HTTP_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        res = ((r.json().get("chart") or {}).get("result") or [None])[0]
    except (httpx.HTTPError, ValueError) as exc:
        raise MarketUnavailable(f"Could not reach the futures data provider ({exc.__class__.__name__}).") from exc
    if not res or not res.get("timestamp"):
        raise MarketUnavailable("The futures provider returned no data.")
    with _lock:
        _cache[key] = (time.time(), res)
    return res


# --------------------------------------------------------------------------- #
# OHLC
# --------------------------------------------------------------------------- #
def futures_ohlc(range_: str, since: float | None = None) -> dict:
    yr, iv, _, _ = RANGES[range_]
    res = _chart(yr, iv)
    q = res["indicators"]["quote"][0]
    tz = res["meta"].get("exchangeTimezoneName") or NY
    bars = []
    daily = iv in DAILY_INTERVALS
    for i, t in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue  # a missing bar stays missing
        if since is not None and t < since:
            continue
        time_key = pd.Timestamp(t, unit="s", tz="UTC").tz_convert(tz).strftime("%Y-%m-%d") if daily else int(t)
        bars.append({"time": time_key, "open": round(float(o), 2), "high": round(float(h), 2), "low": round(float(l), 2), "close": round(float(c), 2),
                     "volume": (int(q["volume"][i]) if q.get("volume") and q["volume"][i] is not None else None)})
    # Yahoo can repeat a daily key at a session boundary; keep the last (most complete) one.
    dedup = {b["time"]: b for b in bars}
    bars = sorted(dedup.values(), key=lambda b: str(b["time"]) if daily else b["time"])
    tick = res["meta"].get("regularMarketTime")
    return {"instrument": "futures", "range": range_, "interval": iv, "time_kind": "day" if daily else "epoch", "bars": bars,
            "source": "Yahoo Finance · COMEX GC=F", "real_ohlc": True, "history_start": bars[0]["time"] if bars else None,
            "delay_seconds": int(max(0, time.time() - tick)) if tick else None, "timezone": tz,
            "gap_policy": "Overnight, weekend and holiday closures are compressed out of the axis; no candles are created for them."}


def _bucket_start(ts: np.ndarray, step: int, interval: str) -> np.ndarray:
    """Start of each bar. Daily bars follow the FX day (17:00 New York); weekly bars start on the Monday of that FX day."""
    if interval == "1d":
        shifted = pd.to_datetime(ts, unit="s", utc=True).tz_convert(NY) + pd.Timedelta(hours=7)  # 17:00 ET -> 00:00 of the next label
        return shifted.normalize().tz_localize(None).to_numpy().astype("datetime64[s]").astype(np.int64)
    if interval == "1wk":
        shifted = pd.to_datetime(ts, unit="s", utc=True).tz_convert(NY) + pd.Timedelta(hours=7)
        monday = (shifted.normalize() - pd.to_timedelta(shifted.dayofweek, unit="D")).tz_localize(None)
        return monday.to_numpy().astype("datetime64[s]").astype(np.int64)
    return (ts // step) * step


def spot_ohlc(range_: str, since: float | None = None) -> dict:
    _, iv, step, span = RANGES[range_]
    now = time.time()
    start = now - span
    r = db.rows("SELECT ts, price FROM spot_ticks WHERE ts>=? ORDER BY ts", (start,))
    first = db.rows("SELECT MIN(ts) AS t FROM spot_ticks")[0]["t"]
    daily = iv in DAILY_INTERVALS
    out = {"instrument": "spot", "range": range_, "interval": iv, "time_kind": "day" if daily else "epoch", "bars": [], "real_ohlc": True,
           "source": "gold-api.com spot XAU/USD · ticks recorded by this app", "history_start": first, "timezone": NY,
           "delay_seconds": None,
           "gap_policy": "Candles exist only where ticks were recorded. Periods when the recorder was not running appear as gaps."}
    if not r:
        return out
    df = pd.DataFrame(r)
    df["b"] = _bucket_start(df["ts"].to_numpy(), step, iv)
    g = df.groupby("b")["price"].agg(["first", "max", "min", "last", "count"])
    for b, row in g.iterrows():
        if since is not None and b < since:
            continue
        key = pd.Timestamp(int(b), unit="s").strftime("%Y-%m-%d") if daily else int(b)
        out["bars"].append({"time": key, "open": round(float(row["first"]), 2), "high": round(float(row["max"]), 2),
                            "low": round(float(row["min"]), 2), "close": round(float(row["last"]), 2), "volume": None, "ticks": int(row["count"])})
    out["delay_seconds"] = int(max(0, now - df["ts"].iloc[-1]))
    return out


def ohlc(instrument: str, range_: str, since: float | None = None) -> dict:
    if range_ not in RANGES:
        raise ValueError(f"range must be one of {list(RANGES)}")
    return futures_ohlc(range_, since) if instrument == "futures" else spot_ohlc(range_, since)


# --------------------------------------------------------------------------- #
# Price summary
# --------------------------------------------------------------------------- #
def _status(tick_age: float | None, in_hours: bool, provider_delay_hint: int) -> dict:
    if tick_age is None:
        return {"state": "unavailable", "label": "No data"}
    if tick_age > 40 * 60:
        return {"state": "stale" if in_hours else "closed", "label": "Stale: no recent trades" if in_hours else "Market closed"}
    if tick_age > max(150, provider_delay_hint * 0.6):
        return {"state": "delayed", "label": f"Delayed ~{round(tick_age / 60)} min"}
    return {"state": "live", "label": "Live"}


def _trading_hours_et(ts: pd.Timestamp, spot: bool) -> bool:
    """Sun 18:00 ET -> Fri 17:00 ET, excluding the 17:00-18:00 daily break (futures) - a coarse check used only for labelling."""
    wd, hr = ts.dayofweek, ts.hour + ts.minute / 60
    if wd == 5 or (wd == 4 and hr >= 17) or (wd == 6 and hr < 18):
        return False
    return spot or not (17 <= hr < 18)


def futures_summary(daily: pd.Series) -> dict:
    res = _chart("5d", "5m")
    q, meta = res["indicators"]["quote"][0], res["meta"]
    ts = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(NY)
    df = pd.DataFrame({"t": ts, "o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"]}).dropna()
    if df.empty:
        raise MarketUnavailable("No futures bars available.")
    df["session"] = (df["t"] + pd.Timedelta(hours=6)).dt.normalize().dt.tz_localize(None)
    sess = df["session"].iloc[-1]
    cur = df[df["session"] == sess]
    prev = daily[daily.index < sess]
    tick = meta.get("regularMarketTime")
    price = float(meta.get("regularMarketPrice") or cur["c"].iloc[-1])
    prev_close = float(prev.iloc[-1]) if len(prev) else None
    now = time.time()
    status = _status(now - tick if tick else None, _trading_hours_et(pd.Timestamp(now, unit="s", tz="UTC").tz_convert(NY), False), 600)
    return {"instrument": "futures", "name": "Gold futures (GC=F)", "currency": "USD", "unit": "USD per troy ounce", "price": round(price, 2),
            "as_of_epoch": tick, "delay_seconds": int(now - tick) if tick else None, "status": status,
            "session": {"date": sess.date().isoformat(), "open": round(float(cur["o"].iloc[0]), 2), "high": round(float(cur["h"].max()), 2),
                        "low": round(float(cur["l"].min()), 2), "starts": "18:00 ET previous evening",
                        "convention": "Session runs 18:00 ET (previous evening) to 17:00 ET. Change is measured against the previous official 13:30 ET settlement."},
            "previous_close": round(prev_close, 2) if prev_close is not None else None,
            "previous_close_status": "recorded" if prev_close is not None else "unavailable_from_provider",
            "previous_close_note": "Official 13:30 ET settlement of the previous completed session." if prev_close is not None else "Unavailable from provider.",
            "previous_close_date": prev.index[-1].date().isoformat() if len(prev) else None,
            "change": round(price - prev_close, 2) if prev_close is not None else None,
            "change_pct": round((price / prev_close - 1) * 100, 3) if prev_close else None,
            "update_every_seconds": 15, "provider": "Yahoo Finance (free feed, ~10 min delayed)"}


ROLL_TOLERANCE_S = 10 * 60  # a recorded tick must be this close to the 17:00 ET roll to count as the session boundary price


def et_1700(day) -> pd.Timestamp:
    """17:00 New York wall-clock time on ``day`` (DST-correct: built from wall-clock fields, never by adding 17h to midnight)."""
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=17, tz=NY)


def spot_session_start(now_et: pd.Timestamp) -> pd.Timestamp:
    """Start of the current FX-style spot session: the most recent 17:00 ET (there is no Saturday-17:00 start; weekends roll back to Friday)."""
    day = now_et.date()
    start = et_1700(day)
    if now_et < start:
        start = et_1700(day - pd.Timedelta(days=1))
    while start.dayofweek == 5:
        start = et_1700(start.date() - pd.Timedelta(days=1))
    return start


def spot_boundary_reference(start: pd.Timestamp) -> pd.Timestamp:
    """The moment whose last price is 'the previous close' for a session starting at ``start``.

    Normally the same 17:00 ET roll; the Sunday-evening open has no roll of its own, so its previous close is Friday 17:00 ET."""
    return et_1700(start.date() - pd.Timedelta(days=2)) if start.dayofweek == 6 else start


def spot_summary() -> dict:
    now = time.time()
    now_et = pd.Timestamp(now, unit="s", tz="UTC").tz_convert(NY)
    start = spot_session_start(now_et)
    s0 = start.timestamp()
    ref = spot_boundary_reference(start).timestamp()
    last = db.rows("SELECT ts, price FROM spot_ticks ORDER BY ts DESC LIMIT 1")
    if not last:
        raise MarketUnavailable("No spot ticks recorded yet.")
    in_sess = db.rows("SELECT ts, price FROM spot_ticks WHERE ts>=? ORDER BY ts", (s0,))
    before = db.rows("SELECT ts, price FROM spot_ticks WHERE ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1", (ref, ref - ROLL_TOLERANCE_S))
    price, tick = last[0]["price"], last[0]["ts"]
    prices = [r["price"] for r in in_sess] or [price]
    prev = before[0]["price"] if before else None
    open_ = in_sess[0]["price"] if in_sess else None
    open_late = bool(in_sess and in_sess[0]["ts"] - s0 > 1800)  # recording started after the session opened
    status = _status(now - tick, _trading_hours_et(now_et, True), 60)
    return {"instrument": "spot", "name": "Spot gold (XAU/USD)", "currency": "USD", "unit": "USD per troy ounce", "price": round(price, 2),
            "as_of_epoch": tick, "delay_seconds": int(now - tick), "status": status,
            "session": {"date": (start + pd.Timedelta(days=1)).date().isoformat(), "open": round(open_, 2) if open_ is not None else None,
                        "high": round(max(prices), 2), "low": round(min(prices), 2), "starts": "17:00 ET",
                        "open_note": "First price recorded after the session began (recording started late)." if open_late else None,
                        "convention": "FX-style day: 17:00 ET to 17:00 ET (daylight saving handled). Open, high and low come from prices this app recorded; change is against the last price recorded at the 17:00 ET roll."},
            "previous_close": round(prev, 2) if prev is not None else None,
            "previous_close_status": "recorded" if prev is not None else "unavailable_from_provider",
            "previous_close_note": (f"Last price recorded at the 17:00 ET roll ({pd.Timestamp(before[0]['ts'], unit='s', tz='UTC').tz_convert(NY).strftime('%a %H:%M:%S')} ET)" if prev is not None
                                    else "Unavailable from provider: gold-api.com's history requires an API key, so there is no free backfill. It appears once this app has recorded a price at a 17:00 ET roll."),
            "previous_close_date": None,
            "change": round(price - prev, 2) if prev is not None else None,
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "update_every_seconds": 10, "provider": "gold-api.com (real-time spot, recorded every 10 s)"}
