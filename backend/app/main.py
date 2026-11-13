"""FastAPI application: prices, forecast, news, CSV upload and forecast download."""
from __future__ import annotations

import csv
import io
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import config
from .data import DataStore, DataUnavailable, Dataset, ValidationError
from .model import ModelError, run_forecast
from .news import NewsService

app = FastAPI(title="Gold Outlook API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["*"])

store = DataStore()
news_service = NewsService()
_forecast_cache: dict[str, dict] = {}
_forecast_lock = threading.Lock()


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _dataset(refresh: bool = False) -> Dataset:
    try:
        return store.get(force_refresh=refresh)
    except DataUnavailable as exc:
        raise _err(503, "no_data", str(exc)) from exc


def price_payload(ds: Dataset, today=None) -> dict:
    s = ds.series
    today = today or datetime.now(timezone.utc).date()
    last_date, last = s.index[-1], float(s.iloc[-1])
    prev_date, prev = s.index[-2], float(s.iloc[-2])
    age = (today - last_date.date()).days
    stale = age > 4  # allows a normal weekend plus one missing session
    freshness = {
        "latest_date": last_date.date().isoformat(),
        "age_days": age,
        "status": "stale" if stale else "fresh",
        "message": (f"Latest observation is {age} days old - the data source may be out of date." if stale
                    else f"Latest observation: {last_date.date().isoformat()}" + (" (today)" if age == 0 else f" ({age} day{'s' if age != 1 else ''} ago)")),
    }
    return {
        "source": ds.meta.get("source"),
        "source_kind": ds.meta.get("source_kind"),
        "symbol": ds.meta.get("symbol"),
        "instrument": ds.meta.get("instrument"),
        "fetched_at": ds.meta.get("fetched_at"),
        "warnings": ds.meta.get("warnings", []),
        "provider_error": ds.meta.get("provider_error"),
        "freshness": freshness,
        "latest": {
            "date": last_date.date().isoformat(),
            "close": last,
            "previous_date": prev_date.date().isoformat(),
            "previous_close": prev,
            "change": last - prev,
            "change_pct": (last / prev - 1) * 100,
        },
        "count": int(len(s)),
        "first_date": s.index[0].date().isoformat(),
        "prices": [{"date": d.date().isoformat(), "close": round(float(v), 2)} for d, v in s.items()],
    }


def _forecast_for(ds: Dataset) -> dict:
    with _forecast_lock:
        cached = _forecast_cache.get(ds.fingerprint)
        if cached:
            return cached
        try:
            result = run_forecast(ds.series)
        except ModelError as exc:
            raise _err(422, "insufficient_history", str(exc)) from exc
        result["data"] = {
            "source": ds.meta.get("source"),
            "source_kind": ds.meta.get("source_kind"),
            "instrument": ds.meta.get("instrument"),
            "latest_date": ds.series.index[-1].date().isoformat(),
        }
        _forecast_cache.clear()  # keep only the current dataset's forecast
        _forecast_cache[ds.fingerprint] = result
        return result


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/prices")
def get_prices(refresh: bool = False):
    return price_payload(_dataset(refresh))


@app.post("/api/prices/refresh")
def refresh_prices():
    return price_payload(_dataset(True))


@app.post("/api/prices/upload")
async def upload_prices(file: UploadFile = File(...)):
    raw = await file.read(config.MAX_UPLOAD_BYTES + 1)
    try:
        ds = store.save_upload(raw, file.filename or "upload.csv")
    except ValidationError as exc:
        raise _err(422, "invalid_csv", str(exc)) from exc
    return price_payload(ds)


@app.get("/api/forecast")
def get_forecast():
    return _forecast_for(_dataset())


@app.get("/api/forecast/download")
def download_forecast():
    ds = _dataset()
    result = _forecast_for(ds)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["horizon_sessions", "base_date", "target_date_approx", "base_price_usd", "forecast_price_usd", "expected_change_pct",
                "range_low_usd", "range_high_usd", "range_level", "model_mae_usd", "baseline_mae_usd", "verdict", "instrument", "source", "generated_at"])
    perf = {p["horizon"]: p for p in result["performance"]}
    for f in result["forecasts"]:
        p = perf[f["horizon"]]
        w.writerow([f["horizon"], f["base_date"], f["target_date"], round(f["base_price"], 2), round(f["forecast_price"], 2),
                    round(f["expected_change_pct"], 3), round(f["interval"]["low"], 2), round(f["interval"]["high"], 2), f["interval"]["level"],
                    round(p["mae_model"], 2), round(p["mae_baseline"], 2), p["verdict_label"],
                    (ds.meta.get("instrument") or {}).get("name", ""), ds.meta.get("source", ""), result["generated_at"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="gold-forecast-{result["forecasts"][0]["base_date"]}.csv"'})


@app.get("/api/news")
def get_news(refresh: bool = False):
    return news_service.get(force=refresh)


# Serve the built frontend (npm run build) from the same origin when present.
_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
