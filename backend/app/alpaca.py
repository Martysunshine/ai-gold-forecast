"""Optional, read-only GLD ETF daily bars from Alpaca market data.

GLD (SPDR Gold Shares, NYSE Arca) is shown next to COMEX gold futures as *context only*: it is a different
instrument (an ETF priced per share, US equity hours) and it is never an input to the forecast. No order or
account endpoints are used.

Keys are read from the environment (see .env.example) and only ever sent to Alpaca in request headers. They are
never returned by the API, logged, or put into error messages: every failure is mapped to a fixed message.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from . import config
from .data import ValidationError, validate_prices

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
SYMBOL = "GLD"
EXCHANGE_TZ = "America/New_York"
INSTRUMENT = {
    "name": "SPDR Gold Shares (GLD), NYSE Arca",
    "kind": "etf",
    "unit": "USD per share",
    "note": "An exchange-traded fund that holds gold bullion. A different instrument from COMEX gold futures; context only, not used by the forecast.",
}


class AlpacaError(Exception):
    """A failure with a user-safe message (never contains keys)."""


def configured() -> bool:
    return bool(config.ALPACA_KEY_ID and config.ALPACA_SECRET_KEY)


def _headers() -> dict:
    return {"APCA-API-KEY-ID": config.ALPACA_KEY_ID, "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY, "Accept": "application/json"}


def _status_message(code: int) -> str:
    if code in (401, 403):
        return f"Alpaca rejected the API keys (HTTP {code}). Check ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env."
    if code == 422:
        return f"Alpaca rejected the request (HTTP 422). Check ALPACA_DATA_FEED (use 'iex' on the free plan)."
    if code == 429:
        return "Alpaca is rate-limiting requests (HTTP 429). Try again shortly."
    return f"Alpaca market data returned HTTP {code}."


def parse_bars(bars: list[dict], now: datetime | None = None) -> pd.Series:
    """Daily bars -> validated close series dated by New York session date. Drops today's bar while the session is open."""
    if not bars:
        raise AlpacaError("Alpaca returned no GLD bars.")
    stamps = pd.to_datetime([b["t"] for b in bars], utc=True).tz_convert(EXCHANGE_TZ)
    frame = pd.DataFrame({"Date": stamps.tz_localize(None).normalize(), "Close": [b.get("c") for b in bars]})
    now_ny = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert(EXCHANGE_TZ)
    session_open = now_ny.hour * 60 + now_ny.minute < 16 * 60 + 15  # regular close 16:00 ET, small buffer
    if len(frame) and frame["Date"].iloc[-1] == now_ny.tz_localize(None).normalize() and session_open:
        frame = frame.iloc[:-1]
    try:
        series, _ = validate_prices(frame)
    except ValidationError as exc:
        raise AlpacaError(f"GLD data from Alpaca failed validation: {exc}") from None
    return series


def fetch_gld(client: httpx.Client | None = None, now: datetime | None = None) -> pd.Series:
    if not configured():
        raise AlpacaError("Alpaca keys are not configured.")
    now = now or datetime.now(timezone.utc)
    params = {
        "symbols": SYMBOL,
        "timeframe": "1Day",
        "start": (now - timedelta(days=5 * 365 + 2)).strftime("%Y-%m-%d"),
        "adjustment": "all",
        "feed": config.ALPACA_DATA_FEED,
        "limit": 10000,
    }
    own = client is None
    client = client or httpx.Client(timeout=config.HTTP_TIMEOUT)
    bars: list[dict] = []
    try:
        for _ in range(20):  # pagination guard
            try:
                resp = client.get(BARS_URL, params=params, headers=_headers())
            except httpx.HTTPError as exc:
                raise AlpacaError(f"Could not reach Alpaca market data ({exc.__class__.__name__}).") from None
            if resp.status_code != 200:
                raise AlpacaError(_status_message(resp.status_code))
            try:
                body = resp.json()
            except ValueError:
                raise AlpacaError("Alpaca returned unreadable data.") from None
            bars.extend((body.get("bars") or {}).get(SYMBOL) or [])
            token = body.get("next_page_token")
            if not token:
                break
            params = {**params, "page_token": token}
    finally:
        if own:
            client.close()
    return parse_bars(bars, now)


@dataclass
class GldData:
    series: pd.Series
    meta: dict


class GldStore:
    """Disk cache for GLD bars: 15-minute TTL; a failed fetch keeps the last valid data and reports the error."""

    def __init__(self, directory: Path | None = None, fetcher=fetch_gld):
        self.dir = Path(directory or config.DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._csv, self._meta = self.dir / "gld.csv", self.dir / "gld.json"
        self._fetcher = fetcher
        self._lock = threading.Lock()

    def _load(self) -> GldData | None:
        if not (self._csv.exists() and self._meta.exists()):
            return None
        try:
            df = pd.read_csv(self._csv, parse_dates=["Date"])
            return GldData(pd.Series(df["Close"].to_numpy(dtype=float), index=pd.DatetimeIndex(df["Date"]), name="gld"), json.loads(self._meta.read_text()))
        except Exception:  # noqa: BLE001
            return None

    def get(self, force: bool = False) -> GldData:
        with self._lock:
            cached = self._load()
            if cached and not force and not config.OFFLINE and time.time() - cached.meta.get("fetched_at_epoch", 0) < config.CACHE_TTL_SECONDS:
                return cached
            try:
                if config.OFFLINE:
                    raise AlpacaError("Offline mode (GOLD_OFFLINE=1): live data requests are disabled.")
                series = self._fetcher()
            except AlpacaError as exc:
                if cached:
                    return GldData(cached.series, {**cached.meta, "provider_error": str(exc)})
                raise
            meta = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "fetched_at_epoch": time.time(), "feed": config.ALPACA_DATA_FEED}
            pd.DataFrame({"Date": series.index.strftime("%Y-%m-%d"), "Close": series.to_numpy()}).to_csv(self._csv, index=False)
            self._meta.write_text(json.dumps(meta))
            return GldData(series, meta)


def compare(gc: pd.Series, gld: pd.Series, window: int = 252) -> dict:
    """Compare GC=F and GLD on their common dates: indexed levels (100 at the first common date), and over the last
    ``window`` common dates the correlation of daily log returns and each instrument's return."""
    common = gc.index.intersection(gld.index)
    if len(common) < 60:
        raise AlpacaError("GLD and the active gold series overlap on fewer than 60 dates, so they cannot be compared.")
    a, b = gc.loc[common].astype(float), gld.loc[common].astype(float)
    rows = [{"date": d.date().isoformat(), "gc": round(float(x), 3), "gld": round(float(y), 3)}
            for d, x, y in zip(common, a / a.iloc[0] * 100, b / b.iloc[0] * 100)]
    tail = common[-(window + 1):]
    ra, rb = np.diff(np.log(a.loc[tail].to_numpy())), np.diff(np.log(b.loc[tail].to_numpy()))
    return {
        "common_dates": int(len(common)),
        "start": common[0].date().isoformat(),
        "end": common[-1].date().isoformat(),
        "indexed": rows,
        "window": {
            "sessions": int(len(tail) - 1),
            "start": tail[0].date().isoformat(),
            "end": tail[-1].date().isoformat(),
            "return_correlation": float(np.corrcoef(ra, rb)[0, 1]),
            "gc_return_pct": float((a.loc[tail[-1]] / a.loc[tail[0]] - 1) * 100),
            "gld_return_pct": float((b.loc[tail[-1]] / b.loc[tail[0]] - 1) * 100),
        },
    }
