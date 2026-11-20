"""Price data: provider fetch, validation, caching and CSV import.

Design rules:
  * Only real observations are ever returned. There is NO synthetic/sample fallback.
  * If the provider fails we fall back to the last cached real data (clearly flagged
    as stale) or to a user-uploaded CSV. If neither exists the caller gets
    ``DataUnavailable`` and the UI shows an empty state.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from . import config

INSTRUMENT_PROVIDER = {
    "name": "COMEX Gold Futures (GC=F), front-month continuous",
    "kind": "futures",
    "unit": "USD per troy ounce",
    "note": "Futures contract prices, not spot gold. Continuous series can contain small jumps when the contract rolls.",
}
INSTRUMENT_UPLOAD = {
    "name": "User-supplied gold price series",
    "kind": "unknown",
    "unit": "USD per troy ounce (assumed - not verifiable)",
    "note": "Instrument and units are whatever the uploaded file contains; the app cannot verify them.",
}


class DataUnavailable(Exception):
    """No real price data could be obtained."""


class ValidationError(ValueError):
    """Uploaded or fetched data failed validation."""


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
_DATE_NAMES = ("date", "datetime", "time", "timestamp", "day")
_CLOSE_NAMES = ("close", "adj close", "adj_close", "adjclose", "price", "settle", "last", "value")


def _find_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {c.strip().lower(): c for c in columns}
    for name in candidates:
        if name in lowered:
            return lowered[name]
    return None


def validate_prices(df: pd.DataFrame, today: date | None = None) -> tuple[pd.Series, list[str]]:
    """Validate a raw frame with a date column and a close column.

    Returns ``(series, warnings)`` where series is a float Series of closes indexed by a
    sorted, unique ``DatetimeIndex`` (naive dates). Raises ``ValidationError`` on bad input.
    """
    today = today or datetime.now(timezone.utc).date()
    warnings: list[str] = []
    if df is None or df.empty:
        raise ValidationError("The file contains no rows.")

    date_col = _find_column(list(df.columns), _DATE_NAMES)
    close_col = _find_column(list(df.columns), _CLOSE_NAMES)
    if date_col is None or close_col is None:
        raise ValidationError(
            "Could not find the required columns. Expected a date column (e.g. 'Date') and a price column "
            f"(e.g. 'Close'); found: {', '.join(map(str, df.columns))}."
        )

    dates = pd.to_datetime(df[date_col], errors="coerce", utc=True)
    bad_dates = int(dates.isna().sum())
    if bad_dates:
        raise ValidationError(f"{bad_dates} row(s) have an unparseable date (first at row {int(np.argmax(dates.isna().to_numpy())) + 2}).")

    raw_close = df[close_col]
    if not pd.api.types.is_numeric_dtype(raw_close):
        raw_close = raw_close.astype(str).str.replace(r"[,$\s]", "", regex=True)
    close = pd.to_numeric(raw_close, errors="coerce")
    missing = int(close.isna().sum())
    if missing:
        # Rows with no price (e.g. holidays exported as blanks) are dropped, but only if rare.
        if missing > 0.05 * len(df):
            raise ValidationError(f"{missing} of {len(df)} price values are missing or non-numeric (more than 5%).")
        warnings.append(f"Dropped {missing} row(s) with a missing or non-numeric price.")

    series = pd.Series(close.to_numpy(dtype=float), index=dates.dt.tz_localize(None).dt.normalize())
    series = series[~series.isna()]
    if not np.isfinite(series.to_numpy()).all():
        raise ValidationError("Prices contain infinite values.")
    if (series <= 0).any():
        raise ValidationError("Prices must be strictly positive; found zero or negative values.")

    if series.index.duplicated().any():
        n_dup = int(series.index.duplicated().sum())
        warnings.append(f"Removed {n_dup} duplicate date(s), keeping the last value for each.")
        series = series[~series.index.duplicated(keep="last")]
    if not series.index.is_monotonic_increasing:
        warnings.append("Rows were not in chronological order and have been sorted.")
        series = series.sort_index()

    if series.index[-1].date() > today:
        raise ValidationError(f"The file contains future dates (latest {series.index[-1].date()}).")
    if len(series) < config.MIN_ROWS:
        raise ValidationError(f"Need at least {config.MIN_ROWS} daily observations; got {len(series)}.")

    jumps = series.pct_change().abs()
    if (jumps > config.MAX_ABS_DAILY_MOVE).any():
        when = jumps.idxmax().date()
        raise ValidationError(
            f"Implausible one-day move of {jumps.max():.0%} on {when}. Check units, splits or a stray value."
        )
    gaps = series.index.to_series().diff().dt.days.dropna()
    if (gaps > 10).any():
        warnings.append(f"The series has {int((gaps > 10).sum())} gap(s) longer than 10 calendar days.")

    series.name = "close"
    return series, warnings


def parse_csv_bytes(raw: bytes) -> tuple[pd.Series, list[str]]:
    if len(raw) > config.MAX_UPLOAD_BYTES:
        raise ValidationError(f"File is larger than {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not raw.strip():
        raise ValidationError("The file is empty.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("The file is not valid UTF-8 text.") from None
    try:
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    except Exception as exc:  # noqa: BLE001 - surface any parser failure as a validation message
        raise ValidationError(f"Could not parse the file as CSV: {exc}") from None
    return validate_prices(df)


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
@dataclass
class FetchResult:
    series: pd.Series
    warnings: list[str] = field(default_factory=list)
    dropped_partial_bar: bool = False
    in_progress: dict | None = None  # last trade of the unfinished session (shown, never modelled)
    pending_settlement: dict | None = None  # a finished session whose official settlement the provider has not posted yet


def _local_dates(epoch_seconds, tz: str) -> pd.DatetimeIndex:
    """Exchange-local calendar dates. Uses the real timezone per timestamp (DST-aware), NOT a fixed offset."""
    utc = pd.to_datetime(np.asarray(epoch_seconds, dtype="int64"), unit="s", utc=True)
    return utc.tz_convert(tz).tz_localize(None).normalize()


def parse_yahoo_chart(payload: dict, now: float | None = None, session_end: str | None = None) -> FetchResult:
    """Convert a Yahoo Finance chart payload into a validated close series (gold).

    ``session_end`` ("HH:MM" exchange time) switches the in-progress rule from the provider's calendar-day trading period
    (GC=F reports 00:00-23:59, which would keep a finished session "unfinished" until midnight) to the real session end.
    """
    frame, meta, dropped, in_progress = _yahoo_frame(payload, now, session_end)
    series, warnings = validate_prices(frame)
    return FetchResult(series=series, warnings=warnings, dropped_partial_bar=dropped, in_progress=in_progress)


def _yahoo_frame(payload: dict, now: float | None, session_end: str | None = None):
    now = now if now is not None else time.time()
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        raise DataUnavailable(f"Provider error: {chart['error'].get('description', chart['error'])}")
    results = chart.get("result") or []
    if not results:
        raise DataUnavailable("Provider returned no data.")
    res = results[0]
    stamps = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    if not stamps or len(stamps) != len(closes):
        raise DataUnavailable("Provider response was malformed.")
    meta = res.get("meta") or {}
    tz = meta.get("exchangeTimezoneName") or "UTC"
    frame = pd.DataFrame({"Date": _local_dates(stamps, tz), "Close": closes}).dropna(subset=["Close"])

    dropped, in_progress = False, None
    regular_end = (((meta.get("currentTradingPeriod") or {}).get("regular")) or {}).get("end")
    market_time = meta.get("regularMarketTime")
    if session_end and len(frame):
        hh, mm = (int(x) for x in session_end.split(":"))
        last_day = frame["Date"].iloc[-1]
        end_epoch = (last_day.tz_localize(tz) + pd.Timedelta(hours=hh, minutes=mm)).timestamp()
        if now < end_epoch:  # that session is still trading: its bar is not a completed observation
            frame = frame.iloc[:-1]
            dropped = True
            if meta.get("regularMarketPrice") is not None and market_time:
                in_progress = {"price": float(meta["regularMarketPrice"]),
                               "time": pd.to_datetime(int(market_time), unit="s", utc=True).isoformat(timespec="minutes"),
                               "session_date": last_day.date().isoformat()}
    elif regular_end and market_time and now < regular_end and len(frame):
        last_day = _local_dates([market_time], tz)[0]
        if frame["Date"].iloc[-1] == last_day:
            frame = frame.iloc[:-1]
            dropped = True
            if meta.get("regularMarketPrice") is not None:
                in_progress = {
                    "price": float(meta["regularMarketPrice"]),
                    "time": pd.to_datetime(int(market_time), unit="s", utc=True).isoformat(timespec="minutes"),
                    "session_date": last_day.date().isoformat(),
                }
    return frame, meta, dropped, in_progress


def _get_chart(symbol: str, history_range: str, client: httpx.Client | None, interval: str = "1d") -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": history_range, "interval": interval, "includePrePost": "true" if interval != "1d" else "false"}
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    own = client is None
    client = client or httpx.Client(timeout=config.HTTP_TIMEOUT, follow_redirects=True)
    try:
        resp = client.get(url, params=params, headers=headers)
        if resp.status_code == 429:
            raise DataUnavailable("Provider is rate-limiting requests (HTTP 429).")
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise DataUnavailable(f"Could not reach the price provider: {exc.__class__.__name__}: {exc}") from exc
    except ValueError as exc:
        raise DataUnavailable(f"Provider returned unreadable data: {exc}") from exc
    finally:
        if own:
            client.close()


def settlement_proxies(payload: dict) -> dict:
    """Settlement-time price per session date: the close of the 5-minute bar that ends at 13:30 America/New_York."""
    res = (((payload or {}).get("chart") or {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        return {}
    ts = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    close = (res["indicators"]["quote"][0] or {}).get("close") or []
    out = {}
    for t, c in zip(ts, close):
        if c is not None and t.hour == 13 and t.minute == 25:
            out[t.tz_localize(None).normalize()] = float(c)
    return out


def apply_settlement_check(series: pd.Series, proxies: dict, tolerance: float) -> tuple[pd.Series, dict | None]:
    """Drop the newest bar if its close does not agree with the settlement-time price.

    Before the provider posts the settlement, a finished session's daily close is just the last trade (typically $5-12 away).
    Accepting it would record the wrong 'official close'. Agreement within ``tolerance`` means the settlement is posted.
    """
    if not len(series):
        return series, None
    day, close = series.index[-1], float(series.iloc[-1])
    proxy = proxies.get(day)
    if proxy is None or abs(close - proxy) <= tolerance:
        return series, None
    return series.iloc[:-1], {"date": day.date().isoformat(), "close_seen": round(close, 2), "settlement_price": round(proxy, 2),
                              "reason": "The provider has not yet posted this session's official settlement; its daily bar still shows the last trade."}


def fetch_yahoo(symbol: str | None = None, history_range: str | None = None, client: httpx.Client | None = None, now: float | None = None) -> FetchResult:
    symbol = symbol or config.SYMBOL
    payload = _get_chart(symbol, history_range or config.HISTORY_RANGE, client)
    session_end = config.SESSION_END_ET or None
    try:
        result = parse_yahoo_chart(payload, now=now, session_end=session_end)
    except ValidationError as exc:
        raise DataUnavailable(f"Provider data failed validation: {exc}") from exc
    if session_end and len(result.series):
        try:
            proxies = settlement_proxies(_get_chart(symbol, "5d", client, interval="5m"))
            kept, pending = apply_settlement_check(result.series, proxies, config.SETTLEMENT_TOLERANCE_USD)
        except DataUnavailable:
            # Cannot verify against settlement-time prices: fall back to the conservative calendar-day rule.
            tz = "America/New_York"
            day_end = (result.series.index[-1].tz_localize(tz) + pd.Timedelta(days=1)).timestamp()
            kept, pending = ((result.series.iloc[:-1], {"date": result.series.index[-1].date().isoformat(), "reason": "Could not verify the settlement; waiting for the calendar day to end."})
                             if (now if now is not None else time.time()) < day_end else (result.series, None))
        if pending:
            result.series = kept
            result.pending_settlement = pending
            result.warnings = list(result.warnings) + [f"Session {pending['date']} is over but its official settlement is not posted yet; it is excluded until it is."]
    return result


# --------------------------------------------------------------------------- #
# Store (on-disk cache + active dataset)
# --------------------------------------------------------------------------- #
@dataclass
class Dataset:
    series: pd.Series
    meta: dict

    @property
    def key(self) -> str:
        """Identity of the data: changes whenever any date or value changes (used to tie results to data)."""
        h = hashlib.sha1()
        h.update(str(self.meta.get("source_kind")).encode())
        h.update(self.series.index.values.astype("datetime64[D]").tobytes())
        h.update(np.round(self.series.to_numpy(dtype=float), 4).tobytes())
        return h.hexdigest()[:12]

    @property
    def fingerprint(self) -> str:  # backwards-compatible alias
        return self.key


class DataStore:
    """Two on-disk slots ("provider", "upload") and a pointer to the active one.

    * An invalid upload is rejected *before* anything is written, so the active data is never disturbed.
    * A failed refresh never replaces data: the last valid dataset is returned with ``provider_error`` set and its
      original ``fetched_at`` so callers can show its age.
    * ``use_provider()`` switches from an upload back to provider data (live, or the saved provider copy).
    """

    def __init__(self, directory: Path | None = None, fetcher=fetch_yahoo):
        self.dir = Path(directory or config.DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._state = self.dir / "state.json"
        self._fetcher = fetcher
        self._lock = threading.RLock()

    # -- persistence --------------------------------------------------------
    def _paths(self, slot: str):
        return self.dir / f"{slot}.csv", self.dir / f"{slot}.json"

    def _load(self, slot: str) -> Dataset | None:
        csv_path, meta_path = self._paths(slot)
        if not (csv_path.exists() and meta_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text())
            df = pd.read_csv(csv_path, parse_dates=["Date"])
            series = pd.Series(df["Close"].to_numpy(dtype=float), index=pd.DatetimeIndex(df["Date"]), name="close")
            return Dataset(series, meta)
        except Exception:  # noqa: BLE001 - a corrupt slot is treated as empty
            return None

    def _save(self, slot: str, series: pd.Series, meta: dict) -> Dataset:
        csv_path, meta_path = self._paths(slot)
        tmp = csv_path.with_suffix(".tmp")
        pd.DataFrame({"Date": series.index.strftime("%Y-%m-%d"), "Close": series.to_numpy()}).to_csv(tmp, index=False)
        tmp.replace(csv_path)
        meta_path.write_text(json.dumps(meta, indent=2))
        return Dataset(series, meta)

    def _active(self) -> str:
        try:
            return json.loads(self._state.read_text()).get("active", "provider")
        except Exception:  # noqa: BLE001
            return "provider"

    def _set_active(self, slot: str) -> None:
        self._state.write_text(json.dumps({"active": slot}))

    @staticmethod
    def _flag_error(ds: Dataset, exc: Exception) -> Dataset:
        meta = dict(ds.meta)
        meta["provider_error"] = str(exc)
        meta["warnings"] = list(meta.get("warnings", [])) + [f"Live refresh failed ({exc}). Showing previously saved data."]
        return Dataset(ds.series, meta)

    def _fetch_and_store(self) -> Dataset:
        if config.OFFLINE:
            raise DataUnavailable("Offline mode (GOLD_OFFLINE=1): live data requests are disabled.")
        result = self._fetcher()
        now = time.time()
        meta = {
            "source_kind": "provider",
            "source": "Yahoo Finance chart API",
            "symbol": config.SYMBOL,
            "instrument": INSTRUMENT_PROVIDER,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fetched_at_epoch": now,
            "partial_bar_excluded": result.dropped_partial_bar,
            "in_progress": result.in_progress,
            "pending_settlement": result.pending_settlement,
            "warnings": result.warnings
            + (["The in-progress trading session was excluded; the latest value is the last completed daily close."]
               if result.dropped_partial_bar else []),
        }
        return self._save("provider", result.series, meta)

    # -- public API ---------------------------------------------------------
    def cached(self) -> Dataset | None:
        """The active dataset as saved on disk, without contacting the provider or writing anything (None when nothing is saved).
        For readers in another process, such as the export, while the worker keeps the saved copy fresh."""
        with self._lock:
            if self._active() == "upload":
                upload = self._load("upload")
                if upload is not None:
                    return upload
            return self._load("provider")

    def get(self, force_refresh: bool = False) -> Dataset:
        """The active dataset. ``force_refresh`` re-fetches provider data (and, if an upload is active, switches back to
        provider data only when the fetch succeeds)."""
        with self._lock:
            upload = self._load("upload") if self._active() == "upload" else None
            if upload is not None and not force_refresh:
                return upload
            cached = self._load("provider")
            if upload is None and cached and not force_refresh:
                age = time.time() - cached.meta.get("fetched_at_epoch", 0)
                if age < config.CACHE_TTL_SECONDS and not config.OFFLINE:
                    return cached
            try:
                ds = self._fetch_and_store()
            except DataUnavailable as exc:
                retained = upload or cached
                if retained is not None:
                    return self._flag_error(retained, exc)
                raise DataUnavailable(f"{exc} No cached data exists. Upload a CSV with Date and Close columns to continue.") from exc
            self._set_active("provider")
            return ds

    def use_provider(self) -> Dataset:
        """Switch from an uploaded file back to provider data: live if reachable, otherwise the saved provider copy."""
        with self._lock:
            try:
                ds = self._fetch_and_store()
            except DataUnavailable as exc:
                cached = self._load("provider")
                if cached is None:
                    raise DataUnavailable(f"{exc} No saved provider data exists, so the uploaded data stays active.") from exc
                self._set_active("provider")
                return self._flag_error(cached, exc)
            self._set_active("provider")
            return ds

    def save_upload(self, raw: bytes, filename: str) -> Dataset:
        series, warnings = parse_csv_bytes(raw)  # raises ValidationError before anything is written
        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename or "upload.csv")[:80]
        meta = {
            "source_kind": "upload",
            "source": f"User CSV upload: {safe_name}",
            "symbol": None,
            "instrument": INSTRUMENT_UPLOAD,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fetched_at_epoch": time.time(),
            "warnings": warnings,
        }
        with self._lock:
            ds = self._save("upload", series, meta)
            self._set_active("upload")
            return ds
