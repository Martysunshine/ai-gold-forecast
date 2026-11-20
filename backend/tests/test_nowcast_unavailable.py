"""The intraday models must never present a zero move as an estimate.

Bug found: after today's 13:30 ET settlement and before the next session opens, the next-settlement target is TOMORROW. The intraday models
had no valid input for it and silently returned a zero return, so the baseline and every "experimental estimate" were the same number (the price
at issue time). These tests pin the fixed behaviour: such models are UNAVAILABLE, are not issued, and any old rows are flagged, not scored."""
import json
import time

import numpy as np
import pandas as pd
import pytest

from app import config, db, ledger, nowcast, quote, registry, worker
from app.nowcast import FEATS, NY


class StubModel:
    def __init__(self, mu):
        self.mu = mu

    def predict(self, X):
        return np.array([self.mu])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db._inited.discard(str(db.path()))
    registry.seed()
    yield


def _daily():
    idx = pd.bdate_range("2026-10-01", "2026-11-17")
    return pd.Series(np.linspace(3900, 3864.2, len(idx)), index=idx)


def _state(last_session: str, champion: str = "unchanged"):
    row = {f: 0.001 for f in FEATS}
    row.update({"session": pd.Timestamp(last_session), "vol_24": 0.002, "prev_close": 3864.2, "first": 3855.2, "c": 3733.9})
    frame = pd.DataFrame([row])
    cal = {"q_lo": -1.0, "q_hi": 1.0, "coverage_pct": 80.0, "n_test": 100, "n_sessions": 40, "coverage_by_session_pct": 80.0,
           "coverage_ci90_pct": [75.0, 85.0], "test_period": {"start": "a", "end": "b"}, "error_by_hours_left": []}
    models = {"ridge": StubModel(0.004), "hgb": StubModel(-0.003), "extra_trees": StubModel(0.002)}
    return nowcast._State(time.time(), "2026-11-17|1", frame, cal, models, champion)


def _run(monkeypatch, tick_utc: str, last_session: str, price=3733.9, champion="unchanged"):
    tick = pd.Timestamp(tick_utc, tz="UTC")
    monkeypatch.setattr(nowcast, "_state", None)
    monkeypatch.setattr(nowcast, "_build_state", lambda daily: _state(last_session, champion))
    monkeypatch.setattr(quote, "_fetch_yahoo", lambda: {"price": price, "tick_epoch": tick.timestamp()})
    monkeypatch.setattr(nowcast.time, "time", lambda: tick.timestamp() + 60)
    return nowcast.get(_daily())


def test_after_settlement_the_intraday_models_are_unavailable_not_zero(monkeypatch):
    p = _run(monkeypatch, "2026-11-18T21:10:00", "2026-11-18")           # 16:10 New York, after the 13:30 settlement; target = Thu 19 Nov
    assert p["phase"] == "next_session" and p["target_date"] == "2026-11-19"
    assert p["intraday_models_available"] is False and "no valid input" in p["intraday_unavailable_reason"]
    assert p["candidates"] == []                                          # nothing is presented as a model estimate
    assert sorted(c["algorithm"] for c in p["unavailable_candidates"]) == ["extra_trees", "hgb", "ridge"]
    assert all(c["available"] is False and "predicted_close" not in c for c in p["unavailable_candidates"])
    assert p["predicted_close"] == p["baseline_forecast"]["predicted_close"] == 3733.9      # the baseline, labelled as such
    assert p["method"] == "baseline" and "assumes no change" in p["message"]


def test_inside_the_session_the_models_give_their_own_nonzero_estimates(monkeypatch):
    p = _run(monkeypatch, "2026-11-19T03:00:00", "2026-11-19")           # 22:00 New York Wed: the Thu session is open
    assert p["phase"] == "pre_settlement" and p["intraday_models_available"] is True and p["unavailable_candidates"] == []
    est = {c["algorithm"]: c["predicted_close"] for c in p["candidates"]}
    assert est["ridge"] > 3733.9 > est["hgb"] and est["extra_trees"] > 3733.9
    assert len(set(est.values())) == 3 and 3733.9 not in est.values()    # distinct, none forced to equal the baseline


def test_an_unavailable_champion_falls_back_to_the_baseline_and_says_so(monkeypatch):
    monkeypatch.setattr(nowcast, "_champion_algo", lambda: "ridge")
    p = _run(monkeypatch, "2026-11-18T21:10:00", "2026-11-18", champion="ridge")
    assert p["fallback_to_baseline"] is True and p["method"] == "baseline" and p["predicted_close"] == 3733.9
    assert "cannot predict this target yet" in p["message"]


def test_the_worker_does_not_issue_unavailable_models_into_the_ledger(monkeypatch):
    p = _run(monkeypatch, "2026-11-18T21:10:00", "2026-11-18")
    monkeypatch.setattr(nowcast, "get", lambda daily, force=False: p)
    out = worker.issue_nowcast(_daily())
    rows = db.rows("SELECT role, algorithm FROM forecasts WHERE task='nowcast'")
    assert out["issued"] == 1 and rows == [{"role": "champion", "algorithm": "unchanged"}]


def _old_fallback_row(tick_utc, target_date, algo="ridge"):
    tick = pd.Timestamp(tick_utc, tz="UTC")
    ledger.issue(task="nowcast", role="shadow", version=registry.version_id("nowcast", algo, "intraday_v1"), algorithm=algo, feature_schema="intraday_v1",
                 input_cutoff=tick.timestamp(), base_price=3733.9, base_time=tick.floor("h").isoformat(), target_date=target_date,
                 target_time=f"{target_date}T13:30:00-05:00", predicted=3733.9, low=3636.0, high=3825.0, level=0.8, horizon=None, hours_left=21.6,
                 data_key="k", issued_at=tick.timestamp())


def test_old_fallback_rows_are_flagged_not_deleted_and_never_scored():
    _old_fallback_row("2026-11-18T20:00:00", "2026-11-19")               # after settlement: not a model output
    _old_fallback_row("2026-11-18T14:00:00", "2026-11-18", "hgb")        # 09:00 New York, same session: a genuine shadow row
    assert ledger.flag_fallbacks() == 1 and ledger.flag_fallbacks() == 0   # idempotent
    assert len(db.rows("SELECT 1 FROM forecasts")) == 2                    # nothing deleted
    flagged = db.rows("SELECT f.algorithm FROM ledger_audit a JOIN forecasts f ON f.id=a.forecast_id WHERE a.kind=?", (ledger.FALLBACK_KIND,))
    assert flagged == [{"algorithm": "ridge"}]
    daily = pd.Series([3800.0, 3864.2, 3700.0], index=pd.to_datetime(["2026-11-17", "2026-11-18", "2026-11-19"]))
    ledger.mature(daily, now=pd.Timestamp("2026-11-20", tz="UTC").timestamp())
    scored = {r["algorithm"] for r in db.rows("SELECT f.algorithm FROM outcomes o JOIN forecasts f ON f.id=o.forecast_id")}
    assert scored == {"hgb"}                                               # the flagged ridge row was never scored


# --------------------------------------------------------------------------- API routing
from app import data, main                                    # noqa: E402
from conftest import make_series                              # noqa: E402


def _next_session_payload():
    return {"target_date": "2099-01-02", "target_time": "2099-01-02T13:30:00-05:00", "market_open": True, "anchor_price": 3733.9, "anchor_time_epoch": time.time() - 600,
            "anchor_delay_seconds": 600, "phase": "next_session", "intraday_models_available": False,
            "intraday_unavailable_reason": "no valid input", "unavailable_candidates": [{"algorithm": "ridge", "available": False, "reason": "no valid input"}],
            "calibration": {"coverage_by_session_pct": 84.0, "coverage_ci90_pct": [80.0, 87.0], "n_sessions": 121}, "typical_error_usd": 3.4}


def _put(task, role, algo, schema, base, pred, target, issued, horizon=None, base_time="2098-12-31"):
    ledger.issue(task=task, role=role, version=registry.version_id(task, algo, schema), algorithm=algo, feature_schema=schema, input_cutoff=issued, base_price=base,
                 base_time=base_time, target_date=target, target_time=f"{target}T13:30:00-05:00", predicted=pred, low=pred - 50, high=pred + 50, level=0.8,
                 horizon=horizon, hours_left=None, data_key="k", issued_at=issued)


def _setup(monkeypatch):
    series = make_series(60, start="2025-06-02")
    monkeypatch.setattr(main, "_dataset", lambda refresh=False: data.Dataset(series, {"source_kind": "provider"}))
    monkeypatch.setattr(main.nowcast, "get", lambda s, force=False: _next_session_payload())
    monkeypatch.setattr(main.worker, "issue_nowcast", lambda s: {})


def test_next_session_without_a_daily_forecast_shows_the_baseline_and_says_why(monkeypatch):
    _setup(monkeypatch)
    _put("nowcast", "champion", "unchanged", "price_now_v1", 3733.9, 3733.9, "2099-01-02", time.time() - 600, base_time="2098-12-31T20:00:00+00:00")
    out = main.forecast_today()
    assert out["route"] == "baseline_reference" and out["forecast"]["source"]["kind"] == "baseline"
    assert "assumes no change" in out["daily_pending"] and out["experimental"] == []
    assert out["intraday"]["available"] is False and out["intraday"]["unavailable_models"][0]["algorithm"] == "ridge"


def test_next_session_routes_to_the_stored_daily_model_with_its_own_reference(monkeypatch):
    _setup(monkeypatch)
    _put("nowcast", "champion", "unchanged", "price_now_v1", 3733.9, 3733.9, "2099-01-02", time.time() - 600, base_time="2098-12-31T20:00:00+00:00")
    _put("fwd_1", "champion", "ridge", "gold_v1", 3864.2, 3869.4, "2099-01-02", time.time() - 7200, horizon=1)
    out = main.forecast_today()
    f = out["forecast"]
    assert out["route"] == "daily_model" and f["source"]["kind"] == "model" and f["source"]["version"].startswith("fwd_1:ridge")
    assert f["reference_price"] == 3864.2 and f["predicted"] == 3869.4 and f["horizon"] == 1       # the official settlement, not the live quote
    assert out["daily_pending"] is None


def test_todays_settlement_is_a_separate_record_and_is_pending_until_the_actual_is_posted(monkeypatch):
    _setup(monkeypatch)
    _put("nowcast", "champion", "unchanged", "price_now_v1", 3733.9, 3733.9, "2099-01-02", time.time() - 600, base_time="2098-12-31T20:00:00+00:00")
    _put("nowcast", "champion", "unchanged", "price_now_v1", 3860.0, 3860.0, "2098-12-31", time.time() - 90000, base_time="2098-12-31T10:00:00+00:00")
    out = main.forecast_today()
    r = out["last_result"]
    assert r["target_date"] == "2098-12-31" and r["predicted"] == 3860.0 and r["actual"] is None and r["status"] == "pending"
    assert r["forecast_id"] != out["forecast"]["forecast_id"]
    assert not any(k in r for k in ("experimental",))


def test_api_and_csv_show_the_same_stored_forecast(monkeypatch):
    _setup(monkeypatch)
    _put("fwd_1", "champion", "ridge", "gold_v1", 3864.2, 3869.4, "2099-01-02", time.time() - 7200, horizon=1)
    _put("nowcast", "champion", "unchanged", "price_now_v1", 3733.9, 3733.9, "2099-01-02", time.time() - 600, base_time="2098-12-31T20:00:00+00:00")
    f = main.forecast_today()["forecast"]
    body = main.export_forecast(f["forecast_id"]).body.decode()
    import csv, io
    row = next(csv.DictReader(io.StringIO(body)))
    assert row["predicted_price_usd"] == f"{f['predicted']:.2f}" and row["reference_price_usd"] == f"{f['reference_price']:.2f}"
    assert row["range_low_usd"] == f"{f['low']:.2f}" and row["range_high_usd"] == f"{f['high']:.2f}" and row["target_date"] == f["target_date"]


def test_the_independent_trace_reproduces_a_freshly_issued_forward_forecast(monkeypatch):
    from app import forecast_trace, learning
    from test_learning import _driver_frame

    series = make_series(420, start="2024-01-02")
    drv = _driver_frame(series)
    monkeypatch.setattr(main, "_dataset", lambda refresh=False: data.Dataset(series, {"source_kind": "provider"}))
    out = learning.issue_forward(series, drv, "k", now=learning._origin_epoch(series.index[-1]) + 7200)
    assert out["issued"] > 0
    for h in (1, 5, 20):
        t = forecast_trace.trace_horizon(h, series, drv)
        failed = [k for k, v in t["checks"].items() if not v]
        assert not failed, (h, failed)
        assert t["stored"]["dataset_hash"] and t["training"]["cutoff"] == series.index[-1].date().isoformat()
        assert all(forecast_trace.compare_surfaces(h)["checks"].values())
