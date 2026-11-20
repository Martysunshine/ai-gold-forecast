"""Regression tests for the forecast-timing, spot-session, calibration and frozen-forecast fixes."""
import time

import numpy as np
import pandas as pd
import pytest

from app import config, data, db, ledger, learning, main, market, nowcast, registry, worker
from conftest import make_series

NY = "America/New_York"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db._inited.discard(str(db.path()))
    registry.seed()


def et(s):
    return pd.Timestamp(s, tz=NY)


# ------------------------------------------------------------------ daily-bar completion (no unfinished bar is ever an observation)
def yahoo_payload(extra_next_day: bool, market_time: float, price: float):
    s = make_series(520, start="2024-01-02")
    days = list(s.index)
    closes = list(s.to_numpy())
    if extra_next_day:  # a partial bar for the NEXT session appears after 18:00 ET
        days.append(days[-1] + pd.offsets.BDay(1))
        closes.append(closes[-1] + 5)
    stamps = [int(pd.Timestamp(d).tz_localize(NY).timestamp()) for d in days]  # Yahoo stamps daily bars at 00:00 ET
    return ({"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [{"close": closes}]},
                                   "meta": {"exchangeTimezoneName": NY, "regularMarketTime": market_time, "regularMarketPrice": price,
                                            "currentTradingPeriod": {"regular": {"start": stamps[-1], "end": stamps[-1] + 86399}}}}], "error": None}}, s)


def test_a_session_is_in_progress_until_its_real_end_not_until_midnight():
    last = make_series(520, start="2024-01-02").index[-1]
    payload, s = yahoo_payload(False, et(f"{last.date()} 14:00").timestamp(), 100.0)
    r = data.parse_yahoo_chart(payload, now=et(f"{last.date()} 14:00").timestamp(), session_end="17:00")
    assert r.dropped_partial_bar and r.series.index[-1] < last and r.in_progress["price"] == 100.0
    r2 = data.parse_yahoo_chart(payload, now=et(f"{last.date()} 17:05").timestamp(), session_end="17:00")
    assert not r2.dropped_partial_bar and r2.series.index[-1] == last  # finished at 17:00, not at 23:59


def test_next_sessions_partial_bar_after_18_et_is_never_accepted():
    payload, s = yahoo_payload(True, 0, 100.0)
    last = s.index[-1]
    r = data.parse_yahoo_chart(payload, now=et(f"{last.date()} 18:30").timestamp(), session_end="17:00")
    assert r.series.index[-1] == last  # the new session's unfinished bar (stamped the next day) is excluded


def test_a_finished_session_whose_settlement_is_not_posted_is_not_a_completed_observation():
    s = make_series(520, start="2024-01-02")
    last, close = s.index[-1], float(s.iloc[-1])
    kept, pending = data.apply_settlement_check(s, {last: close + 8.0}, tolerance=2.0)  # bar still shows the last trade, $8 from settlement
    assert pending and pending["date"] == last.date().isoformat() and kept.index[-1] < last
    kept2, pending2 = data.apply_settlement_check(s, {last: close + 0.5}, tolerance=2.0)  # settlement posted: agrees within $2
    assert pending2 is None and kept2.index[-1] == last
    assert data.apply_settlement_check(s, {}, 2.0)[1] is None  # nothing to compare against: the time rule alone applies


def test_settlement_proxy_is_the_5_minute_bar_ending_at_13_30_et():
    day = pd.Timestamp("2026-11-12")
    stamps = [int((day.tz_localize(NY) + pd.Timedelta(hours=13, minutes=m)).timestamp()) for m in (15, 20, 25, 30, 35)]
    payload = {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [{"close": [1.0, 2.0, 3885.3, 4.0, 5.0]}]}}]}}
    assert data.settlement_proxies(payload) == {day: 3885.3}


# ------------------------------------------------------------------ spot session convention, DST and previous close
@pytest.mark.parametrize("now,offset_h,start_str", [
    ("2026-03-09 10:00", -4, "2026-03-08 17:00"),   # first weekday after spring-forward: EDT
    ("2026-11-02 10:00", -5, "2026-11-01 17:00"),   # first weekday after fall-back: EST
    ("2026-11-14 12:00", -5, "2026-11-13 17:00"),   # Saturday rolls back to Friday
])
def test_spot_session_start_is_17_00_wall_clock_across_dst(now, offset_h, start_str):
    st = market.spot_session_start(et(now))
    assert st == et(start_str) and st.hour == 17 and st.utcoffset() == pd.Timedelta(hours=offset_h)


def _tick(ts, price):
    db.connect().execute("INSERT INTO spot_ticks (ts, price, retrieved) VALUES (?,?,?)", (ts, price, ts))


def test_spot_previous_close_is_only_a_price_recorded_at_the_roll(monkeypatch):
    now = et("2026-11-17 14:00").timestamp()
    monkeypatch.setattr(market.time, "time", lambda: now)
    _tick(et("2026-11-16 16:30").timestamp(), 3900.0)   # 30 minutes before the roll: too far to be "the close"
    _tick(et("2026-11-17 13:59:50").timestamp(), 3850.0)
    s = market.spot_summary()
    assert s["previous_close"] is None and s["previous_close_status"] == "unavailable_from_provider" and s["change"] is None
    assert "Unavailable from provider" in s["previous_close_note"]
    _tick(et("2026-11-16 16:58").timestamp(), 3860.0)   # a tick inside the 10-minute window before 17:00
    s = market.spot_summary()
    assert s["previous_close"] == 3860.0 and s["previous_close_status"] == "recorded" and s["change"] == -10.0


def test_sunday_evening_open_uses_fridays_close(monkeypatch):
    now = et("2026-11-15 18:30").timestamp()
    monkeypatch.setattr(market.time, "time", lambda: now)
    _tick(et("2026-11-13 16:58").timestamp(), 3855.0)
    _tick(et("2026-11-15 18:29:50").timestamp(), 3858.0)
    assert market.spot_summary()["previous_close"] == 3855.0


# ------------------------------------------------------------------ calibration wording and the display threshold
def test_under_calibrated_label_only_when_materially_below_target():
    assert learning.calibration_label(70.3) == "Under-calibrated"
    assert learning.calibration_label(72.0) is None and learning.calibration_label(79.0) is None and learning.calibration_label(None) is None


def test_little_change_threshold_is_documented_and_scales_with_horizon():
    assert learning.little_change(0.05, 1) == (True, pytest.approx(0.10))
    assert learning.little_change(0.15, 1)[0] is False
    assert learning.little_change(0.30, 20)[0] is True and learning.little_change(0.50, 20)[0] is False  # 0.10% x sqrt(20) = 0.447%


# ------------------------------------------------------------------ frozen forecasts and completed targets
def _issue(task, version, base, pred, target_date, issued, horizon=None, role="champion", algo="unchanged", cutoff=None):
    t = et(f"{target_date} 13:30")
    assert ledger.issue(task=task, role=role, version=version, algorithm=algo, feature_schema="x", input_cutoff=cutoff or issued - 30, base_price=base,
                        base_time=str(int(issued)), target_date=target_date, target_time=t.isoformat(), predicted=pred, low=pred - 20, high=pred + 20,
                        level=0.8, horizon=horizon, issued_at=issued)


def _fake_nowcast(price, market_open=True):
    return {"target_date": "2099-01-02", "target_time": "2099-01-02T13:30:00-05:00", "market_open": market_open, "anchor_price": price, "anchor_time_epoch": time.time() - 600, "anchor_delay_seconds": 600,
            "calibration": {"coverage_by_session_pct": 84.0, "coverage_ci90_pct": [80.0, 87.0], "n_sessions": 121}, "typical_error_usd": 3.4,
            "phase": "pre_settlement", "intraday_models_available": True, "intraday_unavailable_reason": None, "unavailable_candidates": []}


def test_the_displayed_forecast_does_not_move_when_the_quote_moves(monkeypatch):
    series = make_series(60, start="2025-06-02")
    monkeypatch.setattr(main, "_dataset", lambda refresh=False: data.Dataset(series, {"source_kind": "provider"}))
    _issue("nowcast", "nowcast:unchanged:price_now_v1", 3860.40, 3860.40, "2099-01-02", time.time() - 3600)
    monkeypatch.setattr(main.nowcast, "get", lambda s, force=False: _fake_nowcast(3855.70))
    a = main.forecast_today()
    monkeypatch.setattr(main.nowcast, "get", lambda s, force=False: _fake_nowcast(3899.00))   # the quote moves a lot
    b = main.forecast_today()
    assert a["forecast"] == b["forecast"]                                  # identical: reference, prediction and range are frozen
    assert a["forecast"]["predicted"] == 3860.40 and a["forecast"]["reference_price"] == 3860.40
    assert a["live"]["price"] == 3855.70 and b["live"]["price"] == 3899.00  # the live quote is carried separately
    assert a["forecast"]["source"]["kind"] == "baseline" and a["forecast"]["source"]["label"] == "Unchanged-price baseline"
    assert a["forecast"]["little_change"] is True and a["status"] == "current"


def test_a_forward_forecast_past_its_target_is_completed_not_current_and_the_delay_is_explained(monkeypatch):
    series = make_series(60, start="2025-06-02")
    base_date = series.index[-1].date().isoformat()
    ds = data.Dataset(series, {"source_kind": "provider", "pending_settlement": {"date": base_date, "reason": "not posted"}})
    monkeypatch.setattr(main, "_dataset", lambda refresh=False: ds)
    past = "2025-07-01"
    _issue("fwd_1", "fwd_1:ridge:gold_v1", 3853.0, 3860.0, past, et(f"{past} 13:30").timestamp() - 86400, horizon=1, algo="ridge")
    out = main.forecast_forward(1)
    assert out["status"] == "completed" and out["result"]["matured"] is False
    assert out["next_forecast"]["state"] == "waiting_for_provider" and "official settlement" in out["next_forecast"]["message"]
    assert "unfinished bar" in out["next_forecast"]["message"]


def test_no_forward_forecast_explains_when_it_will_arrive(monkeypatch):
    series = make_series(60, start="2025-06-02")
    monkeypatch.setattr(main, "_dataset", lambda refresh=False: data.Dataset(series, {"source_kind": "provider"}))
    main._issue_attempted.add(data.Dataset(series, {"source_kind": "provider"}).key)  # skip on-demand issuance (needs network)
    out = main.forecast_forward(1)
    assert out["status"] == "none" and out["next_forecast"]["state"] == "scheduled" and "future" in out["message"]
