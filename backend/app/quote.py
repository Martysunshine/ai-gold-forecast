"""Live price fetchers (spot via gold-api.com, futures via Yahoo) and spot tick recording.

There is no buy/sell quote: neither free feed carries a real bid/ask, so none is shown.

Original notes:

Primary source: real-time **spot gold (XAU/USD)** from api.gold-api.com (no API key, updates
within seconds). Fallback: Yahoo Finance GC=F futures meta (~10 min delayed) if spot is down.

This is separate from the daily close series the model trains on. It gives the *current* price
so the UI can show a live buy/sell panel and a live-anchored forecast.

Honesty notes:
  * The feed exposes no real bid/ask, so buy/sell are the real price +/- a small, clearly
    labelled *indicative* spread. A real broker's spread differs; these are illustrative.
  * "Change today" is measured against the first spot price this process saw on the current
    UTC day (an approximate session open), not an official exchange open.
  * The model never sees any of this — it uses completed daily closes only.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import httpx

from . import config

SPOT_URL = "https://api.gold-api.com/price/XAU"


class QuoteUnavailable(Exception):
    pass


_lock = threading.Lock()
_cache: dict | None = None
_cache_at: float = 0.0
_day_open: dict[str, float] = {}  # UTC date -> first spot price seen that day (approx session open)


def _client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(timeout=config.HTTP_TIMEOUT, follow_redirects=True), True


def _fetch_spot(client: httpx.Client | None = None) -> dict:
    client, own = _client(client)
    try:
        resp = client.get(SPOT_URL, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        resp.raise_for_status()
        d = resp.json()
    except httpx.HTTPError as exc:
        raise QuoteUnavailable(f"spot feed unreachable: {exc.__class__.__name__}") from exc
    except ValueError as exc:
        raise QuoteUnavailable(f"spot feed returned unreadable data: {exc}") from exc
    finally:
        if own:
            client.close()
    price = d.get("price")
    if price is None or "error" in d:
        raise QuoteUnavailable(f"spot feed error: {d.get('error', 'no price')}")
    updated = d.get("updatedAt")
    tick = None
    if updated:
        try:
            tick = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
        except ValueError:
            tick = None
    return {"price": float(price), "tick_epoch": tick, "source": "Spot gold XAU/USD (api.gold-api.com)",
            "instrument": "Spot gold (XAU/USD), USD per troy ounce"}


def _fetch_yahoo(client: httpx.Client | None = None) -> dict:
    client, own = _client(client)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{config.SYMBOL}"
    try:
        resp = client.get(url, params={"range": "1d", "interval": "1m", "includePrePost": "true"},
                          headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        resp.raise_for_status()
        results = (resp.json().get("chart") or {}).get("result") or []
    except (httpx.HTTPError, ValueError) as exc:
        raise QuoteUnavailable(f"futures feed unreachable: {exc.__class__.__name__}") from exc
    finally:
        if own:
            client.close()
    if not results:
        raise QuoteUnavailable("futures feed returned no data.")
    meta = results[0].get("meta") or {}
    if meta.get("regularMarketPrice") is None:
        raise QuoteUnavailable("futures feed returned no price.")
    return {"price": float(meta["regularMarketPrice"]), "tick_epoch": meta.get("regularMarketTime"),
            "source": "Yahoo Finance GC=F futures (delayed)", "instrument": "COMEX gold futures (GC=F)",
            "day_high": meta.get("regularMarketDayHigh"), "day_low": meta.get("regularMarketDayLow"),
            "previous_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "volume": meta.get("regularMarketVolume"), "currency": meta.get("currency"), "exchange": meta.get("exchangeName")}


def record_tick(price: float, tick_epoch: float | None, retrieved: float | None = None) -> bool:
    """Persist a real spot observation (keyed by the provider's own timestamp, so repeats are ignored). Builds spot candle history."""
    if tick_epoch is None:
        return False
    from . import db  # local import: db imports config only, but keep quote importable without a database
    cur = db.connect().execute("INSERT OR IGNORE INTO spot_ticks (ts, price, retrieved) VALUES (?,?,?)", (float(tick_epoch), float(price), retrieved or time.time()))
    return cur.rowcount == 1


def poll_spot() -> dict:
    """Fetch the current spot price and record it. Used by the background worker; raises QuoteUnavailable on failure."""
    raw = _fetch_spot()
    record_tick(raw["price"], raw["tick_epoch"])
    return raw
