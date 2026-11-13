"""FastAPI application: prices, forecasts, news, CSV upload and forecast export."""
from __future__ import annotations

import csv
import io
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import alpaca, config
from .data import DataStore, DataUnavailable, Dataset, ValidationError
from .model import ModelError, run_prediction
from .news import NewsService

app = FastAPI(title="AI Gold Forecast API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["*"])

store = DataStore()
news_service = NewsService()
gld_store = alpaca.GldStore()
_forecast_cache: dict[str, dict] = {}
_forecast_lock = threading.Lock()


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _dataset(refresh: bool = False) -> Dataset:
    try:
        return store.get(force_refresh=refresh)
    except DataUnavailable as exc:
        raise _err(503, "no_data", str(exc)) from exc


def realised_vol_pct(series, window: int = 20) -> float:
    """Annualised close-to-close volatility (percent) over the last ``window`` daily log returns."""
    r = np.diff(np.log(series.to_numpy(dtype=float)))[-window:]
    return float(np.std(r, ddof=1) * np.sqrt(252) * 100)


def price_payload(ds: Dataset, today=None) -> dict:
    s = ds.series
    today = today or datetime.now(timezone.utc).date()
    last_date, last = s.index[-1], float(s.iloc[-1])
    prev_date, prev = s.index[-2], float(s.iloc[-2])
    age = (today - last_date.date()).days
    stale = age > 4  # allows a normal weekend plus one missing session
    fetched_epoch = ds.meta.get("fetched_at_epoch", 0)
    age_min = max(0.0, (datetime.now(timezone.utc).timestamp() - fetched_epoch) / 60)
    kind = ds.meta.get("source_kind")
    if kind == "upload":
        status = {"mode": "upload", "label": "Uploaded CSV"}
    elif ds.meta.get("provider_error"):
        status = {"mode": "cached", "label": "Cached (live refresh failed)"}
    elif age_min <= config.CACHE_TTL_SECONDS / 60 + 1:
        status = {"mode": "live", "label": "Live"}
    else:
        status = {"mode": "cached", "label": "Cached"}
    return {
        "data_key": ds.key,
        "source": ds.meta.get("source"),
        "source_kind": kind,
        "symbol": ds.meta.get("symbol"),
        "instrument": ds.meta.get("instrument"),
        "fetched_at": ds.meta.get("fetched_at"),
        "age_minutes": round(age_min, 1),
        "warnings": ds.meta.get("warnings", []),
        "provider_error": ds.meta.get("provider_error"),
        "status": status,
        "session": {
            "latest_is_completed_session": True,
            "in_progress": ds.meta.get("in_progress"),
            "note": ("The latest value is the last completed daily close. The session still in progress is excluded from the model."
                     if ds.meta.get("partial_bar_excluded") else "The latest value is the last daily close in the data."),
        },
        "freshness": {
            "latest_date": last_date.date().isoformat(),
            "age_days": age,
            "status": "stale" if stale else "fresh",
            "message": (f"Latest observation is {age} days old - the data source may be out of date." if stale
                        else f"Latest observation: {last_date.date().isoformat()}"),
        },
        "latest": {
            "date": last_date.date().isoformat(),
            "close": last,
            "previous_date": prev_date.date().isoformat(),
            "previous_close": prev,
            "change": last - prev,
            "change_pct": (last / prev - 1) * 100,
        },
        "stats": {"realised_vol_20d_annualised_pct": realised_vol_pct(s)},
        "count": int(len(s)),
        "first_date": s.index[0].date().isoformat(),
        "prices": [{"date": d.date().isoformat(), "close": round(float(v), 2)} for d, v in s.items()],
    }


def _predict_for(ds: Dataset, horizon: int) -> dict:
    cache_key = f"{ds.key}|{horizon}"
    with _forecast_lock:
        hit = _forecast_cache.get(cache_key)
        if hit:
            return {**hit, "served_from_cache": True}
        try:
            result = run_prediction(ds.series, horizon)
        except ModelError as exc:
            raise _err(422, "insufficient_history", str(exc)) from exc
        result["data_key"] = ds.key
        result["data"] = {
            "source": ds.meta.get("source"),
            "source_kind": ds.meta.get("source_kind"),
            "instrument": ds.meta.get("instrument"),
            "latest_date": ds.series.index[-1].date().isoformat(),
        }
        for k in [k for k in _forecast_cache if not k.startswith(ds.key + "|")]:
            del _forecast_cache[k]  # keep only the current dataset's results
        _forecast_cache[cache_key] = result
        return {**result, "served_from_cache": False}


def _horizon(h: int) -> int:
    if h not in config.HORIZONS:
        raise _err(422, "bad_horizon", f"horizon must be one of {list(config.HORIZONS)}.")
    return h


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/prices")
def get_prices(refresh: bool = False):
    return price_payload(_dataset(refresh))


@app.post("/api/prices/refresh")
def refresh_prices():
    return price_payload(_dataset(True))


@app.post("/api/prices/use-provider")
def use_provider():
    try:
        return price_payload(store.use_provider())
    except DataUnavailable as exc:
        raise _err(503, "no_provider_data", str(exc)) from exc


@app.post("/api/prices/upload")
async def upload_prices(file: UploadFile = File(...)):
    raw = await file.read(config.MAX_UPLOAD_BYTES + 1)
    try:
        ds = store.save_upload(raw, file.filename or "upload.csv")
    except ValidationError as exc:
        raise _err(422, "invalid_csv", str(exc)) from exc
    return price_payload(ds)


@app.get("/api/predict")
def predict(horizon: int = 5):
    return _predict_for(_dataset(), _horizon(horizon))


EXPORT_COLUMNS = ["horizon_sessions", "forecast_origin_date", "target_session_date", "origin_close_usd", "model", "forecast_price_usd",
                  "projected_change_pct", "target_range_low_usd", "target_range_high_usd", "range_nominal_level",
                  "range_observed_test_coverage_pct", "test_period_start", "test_period_end", "test_n", "test_mae_model_usd",
                  "test_mae_unchanged_usd", "verdict", "instrument", "source", "data_key", "generated_at"]


def export_row(r: dict, instrument_name: str, source: str) -> list:
    """One CSV row. Numbers use the same precision the interface displays (2 decimals)."""
    f, t = r["forecast"], r["test"]
    i = f["interval"]
    return [r["horizon"], r["base_date"], r["target_date"], f"{r['base_price']:.2f}", f["model_label"], f"{f['price']:.2f}",
            f"{f['change_pct']:.2f}", f"{i['low']:.2f}", f"{i['high']:.2f}", i["level"], f"{i['observed_coverage_pct']:.0f}",
            t["period"]["start"], t["period"]["end"], t["n"], f"{t['methods'][f['model']]['mae']:.2f}", f"{t['methods']['unchanged']['mae']:.2f}",
            t["verdict"], instrument_name, source, r["data_key"], r["generated_at"]]


@app.get("/api/predict/download")
def download_prediction(horizon: int = 5, key: str | None = None):
    ds = _dataset()
    if key is not None and key != ds.key:
        raise _err(409, "stale_data", "The active dataset has changed since this forecast was run. Run the forecast again before exporting.")
    r = _predict_for(ds, _horizon(horizon))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    w.writerow(export_row(r, (ds.meta.get("instrument") or {}).get("name", ""), ds.meta.get("source", "")))
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="gold-forecast-{r["horizon"]}-sessions-{r["base_date"]}.csv"'})


GLD_SETUP = [
    "Create a free Alpaca account and generate paper-trading or market-data API keys.",
    "Copy .env.example to .env in the repository root and set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY.",
    "Restart the app (start.cmd on Windows, ./start.sh on macOS and Linux). Only market data is read; no orders are placed.",
]
GLD_NOTE = "GLD is an exchange-traded fund, not the futures contract. Shown for comparison only; not used by the forecast."


@app.get("/api/compare/gld")
def compare_gld(refresh: bool = False):
    if not alpaca.configured():
        return {"configured": False, "setup": GLD_SETUP, "note": GLD_NOTE}
    try:
        g = gld_store.get(force=refresh)
    except alpaca.AlpacaError as exc:
        raise _err(502, "alpaca_error", str(exc)) from exc
    ds = _dataset()
    try:
        comparison = alpaca.compare(ds.series, g.series)
    except alpaca.AlpacaError as exc:
        raise _err(422, "no_overlap", str(exc)) from exc
    last_date, prev = g.series.index[-1], float(g.series.iloc[-2])
    age_min = max(0.0, (datetime.now(timezone.utc).timestamp() - g.meta.get("fetched_at_epoch", 0)) / 60)
    return {
        "configured": True,
        "note": GLD_NOTE,
        "instrument": alpaca.INSTRUMENT,
        "source": f"Alpaca market data ({g.meta.get('feed', config.ALPACA_DATA_FEED)} feed)",
        "fetched_at": g.meta.get("fetched_at"),
        "age_minutes": round(age_min, 1),
        "provider_error": g.meta.get("provider_error"),
        "status": {"mode": "cached", "label": "Cached (refresh failed)"} if g.meta.get("provider_error") else {"mode": "live", "label": "Live"},
        "latest": {"date": last_date.date().isoformat(), "close": float(g.series.iloc[-1]), "change_pct": (float(g.series.iloc[-1]) / prev - 1) * 100},
        "count": int(len(g.series)),
        "gold_source": ds.meta.get("source"),
        "comparison": comparison,
    }


@app.get("/api/news")
def get_news(refresh: bool = False):
    return news_service.get(force=refresh)


# Serve the built frontend (npm run build) from the same origin when present.
_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
