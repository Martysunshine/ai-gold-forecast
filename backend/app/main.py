"""FastAPI application: prices, forecasts, news, CSV upload and forecast export."""
from __future__ import annotations

import asyncio  # noqa: F401
import csv
import io
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import alerts, alpaca, config, n8n_link, paper, quality, threshold_research, workspace, db, drivers, ledger, learning, market, news_store, nowcast, quote, registry, research_drivers, worker
from .data import DataStore, DataUnavailable, Dataset, ValidationError
from .model import ModelError, run_prediction
from .news import NewsService

store = DataStore()
news_service = NewsService()
gld_store = alpaca.GldStore()
_forecast_cache: dict[str, dict] = {}
_issue_attempted: set[str] = set()
_issue_lock = threading.Lock()
_forecast_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start an in-process worker ONLY if no standalone worker is installed (launchd) or holding the daemon lock. The jobs themselves do not
    depend on this web process: ``python -m app.worker run`` runs them with no browser or server open."""
    stop = threading.Event()
    thread = None
    if not config.OFFLINE and os.environ.get("GOLD_EMBEDDED_WORKER", "1") != "0" and not worker.standalone_installed():
        thread = threading.Thread(target=worker.run_loop, args=(stop, True, "web-embedded"), name="embedded-worker", daemon=True)
        thread.start()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="AI Gold Forecast API", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["GET", "POST", "PUT"], allow_headers=["*"])


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _mutation_allowed(request: Request) -> tuple[bool, str]:
    """State-changing calls: with GOLD_API_TOKEN set, the token is required. Otherwise only loopback clients, and (if the browser sent an Origin)
    only this app's own origins, so another website open in the same browser cannot drive the local API."""
    if config.API_TOKEN:
        import hmac
        ok = hmac.compare_digest(request.headers.get("x-gold-token", ""), config.API_TOKEN)
        return ok, "a valid X-Gold-Token header is required"
    if (request.client.host if request.client else "") not in _LOOPBACK:
        return False, "state-changing calls are accepted from this machine only (set GOLD_API_TOKEN to allow others)"
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        allowed = set(config.CORS_ORIGINS) | {f"http://{host}", f"https://{host}"}
        if origin not in allowed:
            return False, "cross-origin request refused"
    return True, ""


@app.middleware("http")
async def guard_mutations(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
        ok, why = _mutation_allowed(request)
        if not ok:
            return JSONResponse(status_code=403, content={"detail": {"code": "forbidden", "message": why}})
    return await call_next(request)


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
    "Restart ./start.sh. Only market data is read; no orders are placed.",
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


@app.get("/api/nowcast")
def get_nowcast():
    """Forecast of today's official close (13:30 ET settlement) from the live futures price."""
    ds = _dataset()
    try:
        return nowcast.get(ds.series)
    except nowcast.NowcastUnavailable as exc:
        raise _err(503, "no_nowcast", str(exc)) from exc


# ----------------------------------------------------------------------------- #
# Chart data, price summary and the two forecast cards
# ----------------------------------------------------------------------------- #
def _instrument(v: str) -> str:
    if v not in ("spot", "futures"):
        raise _err(422, "bad_instrument", "instrument must be 'spot' or 'futures'.")
    return v


@app.get("/api/market/summary")
def market_summary(instrument: str = "futures"):
    try:
        return market.futures_summary(_dataset().series) if _instrument(instrument) == "futures" else market.spot_summary()
    except market.MarketUnavailable as exc:
        raise _err(503, "no_market_data", str(exc)) from exc


@app.get("/api/market/ohlc")
def market_ohlc(instrument: str = "futures", range: str = "1D", since: float | None = None):
    try:
        return market.ohlc(_instrument(instrument), range, since)
    except ValueError as exc:
        raise _err(422, "bad_range", str(exc)) from exc
    except market.MarketUnavailable as exc:
        raise _err(503, "no_market_data", str(exc)) from exc


def _next_hour_epoch(now: float) -> float:
    return (int(now // 3600) + 1) * 3600.0


ALGO_LABEL = {"unchanged": "Unchanged-price baseline", "ridge": "Ridge model", "hgb": "Gradient-boosting model",
              "random_forest": "Random-forest model", "extra_trees": "Extra-trees model"}


def _frozen(f: dict, horizon_sessions: float) -> dict:
    """A ledger forecast exactly as it was issued. Nothing here changes when quotes move."""
    change = f["predicted"] - f["base_price"]
    change_pct = (f["predicted"] / f["base_price"] - 1) * 100
    little, thr = learning.little_change(change_pct, horizon_sessions)
    return {"forecast_id": f["id"], "predicted": round(f["predicted"], 2), "low": round(f["low"], 2), "high": round(f["high"], 2), "level": f["level"],
            "reference_price": round(f["base_price"], 2), "change": round(change, 2), "change_pct": round(change_pct, 3),
            "little_change": little, "little_change_threshold_pct": round(thr, 3), "issued_at": f["issued_at"], "input_cutoff": f["input_cutoff"],
            "target_date": f["target_date"], "target_time": f["target_time"], "horizon": f["horizon"],
            "source": {"kind": "baseline" if f["algorithm"] == "unchanged" else "model", "label": ALGO_LABEL.get(f["algorithm"], f["algorithm"]),
                       "version": f["model_version"], "algorithm": f["algorithm"], "feature_schema": f["feature_schema"], "role": f["role"],
                       "target_def": f["target_def"]}}


def _result(f: dict) -> dict:
    o = ledger.with_outcome(f["id"])
    return {"matured": o is not None, "actual": None if o is None else round(o["actual"], 2), "abs_error": None if o is None else round(o["abs_error"], 2),
            "in_range": None if o is None or o["in_range"] is None else bool(o["in_range"])}


def _pending(ds) -> dict | None:
    return ds.meta.get("pending_settlement")


def _experimental(f: dict) -> list[dict]:
    """Shadow (non-production) forecasts for exactly the same target and base as ``f``: recorded before the outcome, not in control of anything."""
    rows = db.rows("SELECT * FROM forecasts f WHERE f.task=? AND f.role='shadow' AND f.target_date=? AND f.base_time=? AND " + ledger.NOT_FALLBACK + " ORDER BY f.algorithm, f.feature_schema",
                   (f["task"], f["target_date"], f["base_time"]))
    return [{"forecast_id": r["id"], "algorithm": r["algorithm"], "label": ALGO_LABEL.get(r["algorithm"], r["algorithm"]), "feature_schema": r["feature_schema"],
             "version": r["model_version"], "predicted": round(r["predicted"], 2), "low": None if r["low"] is None else round(r["low"], 2),
             "high": None if r["high"] is None else round(r["high"], 2)} for r in rows]


def _settlement_record(target_before: str) -> dict | None:
    """The most recent settlement whose forecast is finished, SEPARATE from the forecast for the next one: what was stored before it, and the actual (or pending).
    Only stored rows are read; nothing is reconstructed after the outcome is known."""
    prev = db.rows("SELECT f.*, o.actual, o.abs_error, o.in_range FROM forecasts f LEFT JOIN outcomes o ON o.forecast_id=f.id "
                   "WHERE f.task='nowcast' AND f.algorithm='unchanged' AND f.target_date<? ORDER BY f.target_date DESC, f.issued_at DESC LIMIT 1", (target_before,))
    if not prev:
        return None
    r = prev[0]
    return {"forecast_id": r["id"], "target_date": r["target_date"], "target_time": r["target_time"], "predicted": round(r["predicted"], 2), "low": r["low"], "high": r["high"],
            "reference_price": round(r["base_price"], 2), "issued_at": r["issued_at"], "input_cutoff": r["input_cutoff"],
            "source": {"kind": "baseline", "label": ALGO_LABEL["unchanged"], "version": r["model_version"]},
            "actual": None if r["actual"] is None else round(r["actual"], 2), "abs_error": None if r["abs_error"] is None else round(r["abs_error"], 2),
            "in_range": None if r["in_range"] is None else bool(r["in_range"]), "status": "scored" if r["actual"] is not None else "pending"}


@app.get("/api/forecast/today")
def forecast_today():
    """The next-settlement forecast exactly as stored (frozen), with an explicit ``route``:

    * ``intraday``: the target settlement belongs to the session that is open now, so the hourly intraday forecast applies;
    * ``daily_model``: the next settlement is a LATER session (today's has passed), which the intraday models cannot forecast, so the stored 1-session daily forecast
      for that exact target is shown (with the previous official settlement as its reference);
    * ``baseline_reference``: that daily forecast is not issued yet, so the stored unchanged-price baseline is shown, labelled as such.
    A fallback is never presented as a model estimate."""
    ds = _dataset()
    try:
        n = nowcast.get(ds.series)
    except nowcast.NowcastUnavailable:
        n = None
    f, route, daily_pending = None, "intraday", None

    def _nowcast_row(tdate: str, baseline_only: bool = False):
        q = "SELECT * FROM forecasts WHERE task='nowcast' AND target_date=? AND " + ("algorithm='unchanged'" if baseline_only else "role='champion'") + " ORDER BY issued_at DESC LIMIT 1"
        r = db.rows(q, (tdate,))
        return r[0] if r else None

    if n:
        if n["phase"] == "next_session":
            d1 = db.rows("SELECT * FROM forecasts WHERE task='fwd_1' AND role='champion' AND target_date=? ORDER BY issued_at DESC LIMIT 1", (n["target_date"],))
            if d1:
                f, route = d1[0], "daily_model"
            else:
                route = "baseline_reference"
                daily_pending = (f"The 1-session model forecast for {n['target_date']} is issued once today's official settlement is final (the daily bar completes after 17:00 New York). "
                                 "Until then this is the reference estimate: it assumes no change.")
                f = _nowcast_row(n["target_date"], baseline_only=True)
                if f is None and n["market_open"]:
                    try:
                        worker.issue_nowcast(ds.series)
                    except Exception:  # noqa: BLE001
                        pass
                    f = _nowcast_row(n["target_date"], baseline_only=True)
        else:
            f = _nowcast_row(n["target_date"])
            if f is None and n["market_open"]:
                try:
                    worker.issue_nowcast(ds.series)  # idempotent; normally the worker has already done this
                except Exception:  # noqa: BLE001
                    pass
                f = _nowcast_row(n["target_date"])
    if f is None:  # nothing issued for the upcoming settlement yet: keep showing the last valid forecast, marked by its real state
        r = db.rows("SELECT * FROM forecasts WHERE task='nowcast' AND role='champion' ORDER BY issued_at DESC LIMIT 1")
        f = r[0] if r else None
    if f is None:
        return {"status": "none", "message": "No settlement forecast has been issued yet. One is issued each hour while the market is open."}
    now = time.time()
    completed = pd.Timestamp(f["target_time"]).timestamp() <= now
    horizon_sessions = float(f["horizon"]) if f["task"] != "nowcast" else 1.0
    out = {"status": "completed" if completed else "current", "route": route, "forecast": _frozen(f, horizon_sessions), "result": _result(f) if completed else None,
           "market_open": bool(n and n["market_open"]), "next_update_epoch": _next_hour_epoch(now) if n and n["market_open"] else None, "pending_settlement": _pending(ds),
           "experimental": _experimental(f) if route != "baseline_reference" else [], "daily_pending": daily_pending,
           "intraday": {"available": bool(n and n["intraday_models_available"]), "reason": (n or {}).get("intraday_unavailable_reason") if n else "No live quote is available.",
                        "unavailable_models": [{"algorithm": c["algorithm"], "label": ALGO_LABEL.get(c["algorithm"], c["algorithm"]), "reason": c["reason"]} for c in (n or {}).get("unavailable_candidates", [])]},
           "base_date": f["base_time"] if f["task"] != "nowcast" else None}
    if f["task"] != "nowcast":
        out["calibration"] = learning.calibration_summary(f["task"], f["base_price"])
    if n:
        live = n["anchor_price"]
        out["live"] = {"price": live, "as_of_epoch": n["anchor_time_epoch"], "delay_seconds": n["anchor_delay_seconds"], "label": "Latest futures quote",
                       "vs_forecast": round(live - f["predicted"], 2), "vs_forecast_pct": round((live / f["predicted"] - 1) * 100, 3)}
        if f["task"] == "nowcast":
            cal = n["calibration"]
            width = (f["high"] - f["low"]) / f["predicted"] * 100
            out["calibration"] = {"target_pct": 80.0, "historical_pct": cal["coverage_by_session_pct"], "ci90_pct": cal["coverage_ci90_pct"], "n_dates": cal["n_sessions"],
                                  "historical_basis": "held-out sessions (not hourly rows)", "mean_width_pct": round(width, 3), "mean_width_usd": round(f["high"] - f["low"], 2),
                                  "label": learning.calibration_label(cal["coverage_by_session_pct"]), "typical_error_usd": n.get("typical_error_usd")}
        out["next_target"] = {"target_date": n["target_date"], "target_time": n["target_time"]}
    if completed:
        out["message"] = ("This settlement has passed; its official figure is not posted yet." if not out["result"]["matured"] else "This settlement has passed.") + \
                         (" The next forecast is issued while the market is open." if not (n and n["market_open"]) else "")
    # Today's (or the last) finished settlement, kept apart from the forecast for the next one. Pending until the official close is posted.
    out["last_result"] = _settlement_record(n["target_date"] if n else f["target_date"])
    return out


@app.get("/api/forecast/forward")
def forecast_forward(horizon: int = 5):
    """The latest valid forward forecast as issued. If its target has passed it is reported as completed, never as current."""
    h = _horizon(horizon)
    task = f"fwd_{h}"
    ds = _dataset()
    f = ledger.latest(task, "champion", horizon=h)
    base_date = ds.series.index[-1].date().isoformat()
    if (f is None or f["base_time"] != base_date) and ds.key not in _issue_attempted:
        # The worker issues these after every completed session. If it has not yet, try ONCE per data version (idempotent,
        # ledger-backed). A horizon that cannot be issued must not trigger repeated heavy refits.
        with _issue_lock:
            if ds.key not in _issue_attempted:
                _issue_attempted.add(ds.key)
                try:
                    d = drivers.load()
                except drivers.DriversUnavailable:
                    d = None
                if d is not None:
                    learning.issue_forward(ds.series, d, ds.key)
        f = ledger.latest(task, "champion", horizon=h)
    if f is None:
        pend = _pending(ds)
        nxt = ({"state": "waiting_for_provider", "message": f"The {pend['date']} session has ended, but Yahoo's free daily series has not posted its official settlement yet. "
                "The first forecast is issued as soon as it does."} if pend else
               {"state": "scheduled", "message": f"No {h}-session forecast has been issued yet. It is issued after the next session completes: the target must lie in the future, "
                "and the base price must be a finished session's official settlement."})
        return {"status": "none", "horizon": h, "message": nxt["message"], "next_forecast": nxt}
    now = time.time()
    completed = pd.Timestamp(f["target_time"]).timestamp() <= now
    out = {"status": "completed" if completed else "current", "horizon": h, "forecast": _frozen(f, float(h)), "result": _result(f) if completed else None,
           "base_date": f["base_time"], "calibration": learning.calibration_summary(task, f["base_price"]), "experimental": _experimental(f),
           "evaluation": {"development": learning.status_payload()["tasks"][task]["development"], "forward": learning.status_payload()["tasks"][task]["forward"]}}
    newer_session = f["base_time"] != base_date  # a completed session exists that this forecast does not know about
    pend = _pending(ds)
    if completed or newer_session:
        if pend:
            out["next_forecast"] = {"state": "waiting_for_provider", "message": (f"The {pend['date']} session has ended, but Yahoo's free daily series has not posted its official settlement "
                                    "(its bar still shows the last trade). The next forecast is issued as soon as it does; an unfinished bar is never used as a completed observation.")}
        elif newer_session:
            out["next_forecast"] = {"state": "issuing", "message": "A new completed session is available; the worker is issuing the next forecast (usually within minutes)."}
        else:
            out["next_forecast"] = {"state": "scheduled", "message": "The next forecast is issued after the next session completes."}
    return out


FC_EXPORT_COLUMNS = ["forecast_id", "instrument", "forecast_type", "horizon_sessions", "source", "model_version", "role", "reference_price_usd", "predicted_price_usd",
                     "change_usd", "change_pct", "range_level_pct", "range_low_usd", "range_high_usd", "target_date", "target_time_new_york", "issued_at_utc",
                     "input_cutoff_utc", "actual_usd", "inside_range"]


@app.get("/api/forecast/export")
def export_forecast(forecast_id: str):
    """CSV of one stored forecast. Built from the same frozen record the screen shows (same rounding), so a download can never disagree with the display."""
    r = db.rows("SELECT * FROM forecasts WHERE id=?", (forecast_id,))
    if not r:
        raise _err(404, "unknown_forecast", "No stored forecast has that id. Run Predict again, then export.")
    f = r[0]
    fr = _frozen(f, 1.0 if f["task"] == "nowcast" else float(f["horizon"] or 1))
    res = _result(f)
    iso = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(FC_EXPORT_COLUMNS)
    w.writerow([f["id"], "COMEX gold futures (GC=F), USD per troy ounce", "next settlement" if f["task"] == "nowcast" else "forward", f["horizon"] or "",
                fr["source"]["label"], f["model_version"], f["role"], f"{fr['reference_price']:.2f}", f"{fr['predicted']:.2f}", f"{fr['change']:.2f}", f"{fr['change_pct']:.2f}",
                round(fr["level"] * 100), f"{fr['low']:.2f}", f"{fr['high']:.2f}", f["target_date"], f["target_time"], iso(f["issued_at"]), iso(f["input_cutoff"]),
                "" if res["actual"] is None else f"{res['actual']:.2f}", "" if res["in_range"] is None else int(res["in_range"])])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="gold-forecast-{f["task"]}-{f["target_date"]}.csv"'})


@app.get("/api/learning")
def learning_status():
    return {**learning.status_payload(), "worker": worker.status(), "news": news_store.summary(),
            "drivers_research": research_drivers.latest()}


@app.get("/api/learning/workspace")
def learning_workspace():
    """Everything the Learning page shows: worker/runtime, per-horizon active model, candidates, forward vs development evidence, trend, quality, attempts."""
    return workspace.payload()


@app.get("/api/worker")
def worker_status():
    """Which runtime is operating, its heartbeat, last runs and next scheduled jobs (read-only; the worker itself exposes nothing)."""
    return worker.status()


@app.get("/api/quality")
def data_quality():
    return quality.summary(40)


@app.post("/api/quality/run")
def data_quality_run():
    return quality.run()


def _paper_err(exc: Exception) -> HTTPException:
    return _err(422, "paper_error", str(exc))


@app.get("/api/paper")
def paper_panel():
    """The simulated gold book(s). Simulation only: there is no broker connection anywhere in this app's paper trading."""
    return paper.panel()


@app.post("/api/paper/book")
def paper_new_book(body: dict = Body(default={})):
    """Start a fresh book (archiving, never deleting, the current one). Optional: name, initial_cash, params."""
    try:
        return paper.new_book(body.get("name"), float(body.get("initial_cash", 10_000.0)), body.get("params"))
    except (paper.PaperError, TypeError, ValueError) as exc:
        raise _paper_err(exc) from exc


@app.put("/api/paper/book/{book_id}/params")
def paper_params(book_id: int, patch: dict = Body(...)):
    try:
        return paper.update_params(book_id, patch)
    except paper.PaperError as exc:
        raise _paper_err(exc) from exc


@app.post("/api/paper/book/{book_id}/pause")
def paper_pause(book_id: int, body: dict = Body(default={})):
    try:
        return paper.pause(book_id, str(body.get("reason") or "paused by you")[:200])
    except paper.PaperError as exc:
        raise _paper_err(exc) from exc


@app.post("/api/paper/book/{book_id}/resume")
def paper_resume(book_id: int):
    try:
        return paper.resume(book_id)
    except paper.PaperError as exc:
        raise _paper_err(exc) from exc


@app.post("/api/paper/book/{book_id}/flatten")
def paper_flatten(book_id: int):
    try:
        return paper.flatten(book_id, alerts.fetch_quote("futures"))
    except paper.PaperError as exc:
        raise _paper_err(exc) from exc
    except Exception as exc:  # noqa: BLE001 - no quote: the simulated position is left as it is
        raise _err(503, "no_quote", f"No quote is available to price the close: {exc.__class__.__name__}") from exc


@app.post("/api/paper/evaluate")
def paper_evaluate_now():
    return worker.run_job("paper", worker.job_paper, force=True) or {"error": "the paper job failed; see the Learning page's job status"}


@app.get("/api/drivers")
def drivers_panel():
    return drivers.panel()


@app.get("/api/news")
def get_news(refresh: bool = False):
    return news_service.get(force=refresh)


# --------------------------------------------------------------------------------------------------------------- alerts
@app.get("/api/alerts")
def alerts_panel():
    """Settings, last forecast check, last alert and its delivery status, and whether a touch probability may be shown."""
    return {**alerts.panel(), "touch": threshold_research.panel()}


@app.put("/api/alerts/settings")
def alerts_settings(patch: dict = Body(...)):
    try:
        alerts.save_settings(patch)
    except alerts.SettingsError as exc:
        raise _err(400, "bad_settings", str(exc)) from exc
    try:  # the schedule, levels and morning text live in n8n: push the change now (also works in simulate mode: it is just logged)
        n8n_link.push_snapshot(force=True)
    except Exception as exc:  # noqa: BLE001
        alerts.log("handoff", "error", exc.__class__.__name__)
    return {**alerts.panel(), "touch": threshold_research.panel()}


@app.get("/api/alerts/basis")
def alerts_basis():
    """What the review levels apply to: instrument, currency, unit and quote basis, with live quotes and any warnings about the entry price."""
    return alerts.verify_basis()


@app.post("/api/alerts/check")
def alerts_check_now():
    return worker.run_job("alerts", worker.job_alerts)


@app.get("/api/alerts/morning-preview")
def alerts_morning_preview():
    """What the morning SMS would say if it were sent right now (stale or unavailable forecasts are said to be so)."""
    snap = n8n_link.build_snapshot(fetch_quote=False)
    return {"schedule": snap["schedule"], **n8n_link.decide_morning(snap, time.time()), "status": snap["morning"]["status"], "valid_until": snap["morning"]["valid_until"]}


@app.get("/review", response_class=HTMLResponse)
def review_page(t: str = ""):
    """Bearer-token review page opened from an SMS link. Read-only; it places no order and changes nothing."""
    page = n8n_link.local_review(t)
    hdr = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"}
    if page is None:
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Link expired</title><p style='font:16px sans-serif;padding:24px'>This review link is invalid or has expired.</p>", status_code=403, headers=hdr)
    return HTMLResponse(page, headers=hdr)


# Serve the built frontend (npm run build) from the same origin when present.
_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
