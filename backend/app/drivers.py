"""Market-driver inputs (dollar, yields, real yields, equities, volatility) with point-in-time availability.

Every observation carries:
  obs_date      the market date it describes
  avail_time    the earliest moment we may treat it as known (conservative; see AVAILABILITY)
  retrieved_at  when this app downloaded it
  source, unit

Features for a forecast made at ``origin_time`` use ONLY observations whose avail_time <= origin_time
(an as-of join on availability, never on the observation date). A same-date close is therefore not usable for a
forecast issued before that close was published. No economic-announcement or consensus data is included: a
timestamped historical consensus source is not available without a paid data licence.
"""
from __future__ import annotations

import io
import threading
import time
from datetime import datetime, timezone

import httpx
import numpy as np
import pandas as pd

from . import config

NY = "America/New_York"

# series -> (provider, id, unit, kind, availability rule, human label)
#   kind: "px" = price-like (log changes), "rate" = yield in percent (simple differences)
SERIES = {
    "dxy": ("yahoo", "DX-Y.NYB", "index points", "px", "same_day_1800", "US dollar index (DXY)"),
    "y10": ("yahoo", "^TNX", "percent", "rate", "same_day_1800", "US 10-year Treasury yield"),
    "spx": ("yahoo", "^GSPC", "index points", "px", "same_day_1800", "S&P 500"),
    "vix": ("yahoo", "^VIX", "index points", "px", "same_day_1800", "VIX volatility index"),
    "real10": ("fred", "DFII10", "percent", "rate", "next_day_1800", "US 10-year real yield (TIPS)"),
}
# Conservative publication times: Yahoo daily closes are all final by 18:00 New York time the same day; FRED's H.15
# yields are published on the following business day (treated as 18:00 the next calendar day).
STALE_AFTER_DAYS = {"yahoo": 4, "fred": 6}

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "df": None}
CACHE_TTL = 30 * 60


class DriversUnavailable(Exception):
    pass


def _avail(obs_date: pd.Series, rule: str) -> pd.Series:
    base = pd.to_datetime(obs_date).dt.tz_localize(NY)
    if rule == "same_day_1800":
        return base + pd.Timedelta(hours=18)
    if rule == "next_day_1800":
        return base + pd.Timedelta(days=1, hours=18)
    raise ValueError(rule)


def _fetch_yahoo(sym: str, client: httpx.Client) -> pd.Series:
    r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                   params={"range": "6y", "interval": "1d", "includePrePost": "false"},
                   headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    r.raise_for_status()
    res = ((r.json().get("chart") or {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        raise DriversUnavailable(f"{sym}: empty response")
    tz = res["meta"].get("exchangeTimezoneName") or NY
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(tz).tz_localize(None).normalize()
    s = pd.Series(res["indicators"]["quote"][0]["close"], index=idx, dtype=float).dropna()
    s = s[~s.index.duplicated(keep="last")]
    # Drop a still-forming bar for today's session: its close is not final.
    meta = res["meta"]
    reg_end = ((meta.get("currentTradingPeriod") or {}).get("regular") or {}).get("end")
    if reg_end and time.time() < reg_end and len(s) and s.index[-1].date() == pd.Timestamp(meta.get("regularMarketTime", 0), unit="s", tz="UTC").tz_convert(tz).date():
        s = s.iloc[:-1]
    return s


def _fetch_fred(sid: str, client: httpx.Client) -> pd.Series:
    r = client.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": sid})
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna()
    df = df[pd.to_datetime(df["date"]) >= pd.Timestamp.now() - pd.Timedelta(days=6 * 365)]
    return pd.Series(df["value"].to_numpy(), index=pd.to_datetime(df["date"]).dt.normalize(), dtype=float)


def fetch_all(client: httpx.Client | None = None) -> pd.DataFrame:
    """Long frame: series, obs_date, value, avail_time, retrieved_at, source, unit. Missing series are skipped, not invented."""
    own = client is None
    client = client or httpx.Client(timeout=config.HTTP_TIMEOUT * 2, follow_redirects=True)
    rows, errors = [], {}
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for key, (provider, sid, unit, kind, rule, label) in SERIES.items():
            try:
                s = _fetch_yahoo(sid, client) if provider == "yahoo" else _fetch_fred(sid, client)
            except (httpx.HTTPError, ValueError, DriversUnavailable) as exc:
                errors[key] = f"{exc.__class__.__name__}: {exc}"
                continue
            d = pd.DataFrame({"series": key, "obs_date": s.index, "value": s.to_numpy()})
            d["avail_time"] = _avail(d["obs_date"], rule)
            d["retrieved_at"], d["source"], d["unit"] = retrieved, f"{provider}:{sid}", unit
            rows.append(d)
    finally:
        if own:
            client.close()
    if not rows:
        raise DriversUnavailable("No driver series could be downloaded. " + "; ".join(errors.values()))
    out = pd.concat(rows, ignore_index=True)
    out.attrs["errors"] = errors
    return out


def load(force: bool = False) -> pd.DataFrame:
    """Driver observations from the on-disk cache (data/drivers.csv), refreshed at most every 30 minutes."""
    path = config.DATA_DIR / "drivers.csv"
    with _lock:
        now = time.time()
        if not force and _cache["df"] is not None and now - _cache["at"] < CACHE_TTL:
            return _cache["df"]
        try:
            df = fetch_all()
            tmp = path.with_suffix(".tmp")
            df.assign(avail_time=df["avail_time"].astype(str)).to_csv(tmp, index=False)
            tmp.replace(path)
        except (DriversUnavailable, httpx.HTTPError) as exc:
            if path.exists():  # keep the last valid copy; the panel shows it as stale
                df = pd.read_csv(path, parse_dates=["obs_date"])
                df["avail_time"] = pd.to_datetime(df["avail_time"], utc=True).dt.tz_convert(NY)
                df.attrs["errors"] = {"all": f"refresh failed, using saved copy ({exc})"}
            else:
                raise DriversUnavailable(str(exc)) from exc
        _cache.update(at=now, df=df)
        return df


# --------------------------------------------------------------------------- #
# Features (point-in-time)
# --------------------------------------------------------------------------- #
GROUPS = {
    "dollar": ["dxy_chg1", "dxy_chg5"],
    "yields": ["y10_chg1", "y10_chg5", "real10_chg1", "real10_chg5"],
    "equities_vol": ["spx_ret1", "spx_ret5", "vix_level", "vix_chg5"],
}
ALL_DRIVER_FEATURES = [f for g in GROUPS.values() for f in g]


def _series_features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-observation features, indexed by availability time. Changes use only that series' own earlier observations."""
    parts = []
    for key, (_, _, _, kind, _, _) in SERIES.items():
        d = df[df["series"] == key].sort_values("obs_date")
        if d.empty:
            continue
        v = d["value"].to_numpy(dtype=float)
        s = pd.Series(v, index=pd.DatetimeIndex(d["obs_date"]))
        f = pd.DataFrame(index=d.index)
        if kind == "px":
            lg = np.log(s.to_numpy())
            one, five = np.r_[np.nan, np.diff(lg)], pd.Series(lg).diff(5).to_numpy()
        else:
            one, five = np.r_[np.nan, np.diff(v)], pd.Series(v).diff(5).to_numpy()
        name = {"dxy": "dxy_chg", "y10": "y10_chg", "real10": "real10_chg", "spx": "spx_ret", "vix": "vix_chg"}[key]
        cols = {f"{name}1": one, f"{name}5": five}
        if key == "vix":
            cols = {"vix_level": v / 100.0, "vix_chg5": pd.Series(np.log(v)).diff(5).to_numpy()}
        out = pd.DataFrame(cols)
        out["avail_time"] = pd.to_datetime(d["avail_time"]).to_numpy()
        out["obs_date"] = d["obs_date"].to_numpy()
        parts.append(out.dropna().sort_values("avail_time"))
    return parts  # list of frames, one per series


def asof_features(origins: pd.Series, df: pd.DataFrame, same_day_control: bool = False) -> pd.DataFrame:
    """Driver features for each forecast origin (a Series of tz-aware timestamps), using only what was available by then.

    ``same_day_control=True`` joins on the observation DATE (value treated as known at 13:30 that day) instead of the
    availability time: a deliberately leaky control used only to measure how much an availability-blind join would have
    flattered the results. Never used for production forecasts.
    """
    left = pd.DataFrame({"origin": origins.dt.tz_convert("UTC").astype("datetime64[ns, UTC]").to_numpy(), "_i": np.arange(len(origins))})
    left = left.sort_values("origin")
    out = pd.DataFrame(index=origins.index)
    for part in _series_features(df):
        cols = [c for c in part.columns if c not in ("avail_time", "obs_date")]
        if same_day_control:
            avail = pd.to_datetime(part["obs_date"]).dt.tz_localize(NY) + pd.Timedelta(hours=13, minutes=30)
        else:
            avail = pd.to_datetime(part["avail_time"], utc=True)
        right = part[cols].copy()
        right["_avail"] = avail.dt.tz_convert("UTC").astype("datetime64[ns, UTC]").to_numpy()
        right = right.sort_values("_avail")
        merged = pd.merge_asof(left, right, left_on="origin", right_on="_avail", direction="backward")
        for c in cols:
            out[c] = merged.set_index("_i")[c].sort_index().to_numpy()
        out[f"__avail_{cols[0]}"] = merged.set_index("_i")["_avail"].sort_index().to_numpy()
    return out


# --------------------------------------------------------------------------- #
# Panel payload (freshness / missing)
# --------------------------------------------------------------------------- #
def panel(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    try:
        df = load()
    except DriversUnavailable as exc:
        return {"status": "unavailable", "message": str(exc), "items": []}
    errors = df.attrs.get("errors", {})
    items = []
    for key, (provider, sid, unit, kind, rule, label) in SERIES.items():
        d = df[df["series"] == key].sort_values("obs_date")
        if d.empty:
            items.append({"key": key, "label": label, "status": "missing", "unit": unit, "error": errors.get(key), "source": f"{provider}:{sid}"})
            continue
        last, prev = d.iloc[-1], d.iloc[-2] if len(d) > 1 else None
        age_days = (pd.Timestamp(now).tz_convert("UTC").tz_localize(None).normalize() - pd.Timestamp(last["obs_date"]).normalize()).days
        change = None
        if prev is not None:
            change = (float(last["value"]) - float(prev["value"])) if kind == "rate" else (float(last["value"]) / float(prev["value"]) - 1) * 100
        items.append({
            "key": key, "label": label, "value": round(float(last["value"]), 3), "unit": unit,
            "change": None if change is None else round(change, 3), "change_unit": "pp" if kind == "rate" else "%",
            "obs_date": pd.Timestamp(last["obs_date"]).date().isoformat(),
            "available_at": pd.Timestamp(last["avail_time"]).isoformat(), "retrieved_at": str(last["retrieved_at"]),
            "source": last["source"], "age_days": int(age_days),
            "status": "stale" if age_days > STALE_AFTER_DAYS[provider] else "fresh",
            "note": "Published the next business day; the latest value is always about one day behind." if provider == "fred" else None,
        })
    return {"status": "ok", "items": items, "errors": errors,
            "note": "Inputs are joined to forecasts by when they became available, not by their date."}
