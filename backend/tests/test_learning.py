"""Learning-loop integrity tests. Synthetic data here verifies MACHINERY (ordering, locking, gates, scoring) only; it says nothing
about gold-market predictability."""
import sqlite3
import time

import numpy as np
import pandas as pd
import pytest

from app import config, db, drivers, ledger, learning, market, news_store, nowcast, registry, worker
from app import research_drivers as rd
from conftest import make_series

NY = "America/New_York"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db._inited.discard(str(db.path()))
    registry.seed()
    yield


def _issue(task="fwd_1", role="champion", version=None, base_time="2025-03-03", target="2025-03-04", predicted=100.0, base=100.0, when=None, **kw):
    version = version or registry.champion(task)["version"]
    t_time = (pd.Timestamp(target).tz_localize(NY) + pd.Timedelta(hours=13, minutes=30))
    when = when if when is not None else t_time.timestamp() - 86400
    return ledger.issue(task=task, role=role, version=version, algorithm="ridge", feature_schema="gold_v1", input_cutoff=when - 10, base_price=base,
                        base_time=base_time, target_date=target, target_time=t_time.isoformat(), predicted=predicted, low=predicted - 5, high=predicted + 5,
                        level=0.8, horizon=1, issued_at=when, **kw)


# ----------------------------------------------------------------------------------- ledger integrity
def test_forecasts_cannot_be_edited_or_deleted():
    assert _issue()
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.connect().execute("UPDATE forecasts SET predicted=1")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.connect().execute("DELETE FROM forecasts")


def test_issuing_the_same_forecast_twice_changes_nothing():
    assert _issue(predicted=100.0)
    assert not _issue(predicted=999.0)  # same id: ignored, original kept
    assert db.rows("SELECT predicted FROM forecasts")[0]["predicted"] == 100.0


def test_forecast_issued_after_its_target_time_is_rejected():
    late = pd.Timestamp("2025-03-04").tz_localize(NY).timestamp() + 20 * 3600  # after 13:30 ET on the target day
    assert not _issue(when=late)
    assert db.rows("SELECT COUNT(*) AS n FROM forecasts")[0]["n"] == 0


def test_outcome_comes_only_from_a_completed_close_and_is_write_once():
    _issue(predicted=104.0, base=100.0, target="2025-03-04")
    assert ledger.mature(pd.Series([100.0], index=pd.DatetimeIndex(["2025-03-03"]))) == 0  # target close does not exist yet
    daily = pd.Series([100.0, 97.0], index=pd.DatetimeIndex(["2025-03-03", "2025-03-04"]))
    assert ledger.mature(daily) == 1
    o = db.rows("SELECT * FROM outcomes")[0]
    assert o["actual"] == 97.0 and o["error"] == 7.0 and o["baseline_abs_error"] == 3.0 and o["in_range"] == 0
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        db.connect().execute("UPDATE outcomes SET actual=1")
    assert ledger.mature(daily) == 0  # scoring again changes nothing


def test_a_late_forecast_is_never_scored():
    # Insert a forecast that was (illegally) issued after its target by bypassing issue(): it must still not be scored.
    t = (pd.Timestamp("2025-03-04").tz_localize(NY) + pd.Timedelta(hours=13, minutes=30))
    db.connect().execute("INSERT INTO forecasts (id,task,instrument,target_def,role,model_version,algorithm,feature_schema,issued_at,input_cutoff,base_price,base_time,"
                         "target_date,target_time,horizon,predicted) VALUES ('x','fwd_1','futures','d','champion','v','ridge','gold_v1',?,?,?,?,?,?,?,?)",
                         (t.timestamp() + 3600, t.timestamp() + 3000, 100.0, "2025-03-03", "2025-03-04", t.isoformat(), 1, 100.0))
    daily = pd.Series([100.0, 97.0], index=pd.DatetimeIndex(["2025-03-03", "2025-03-04"]))
    assert ledger.mature(daily) == 0


# ----------------------------------------------------------------------------------- no leakage
def _driver_frame(index):
    rows = []
    rng = np.random.default_rng(0)
    for key, (provider, *_rest) in drivers.SERIES.items():
        rule = drivers.SERIES[key][4]
        vals = 100 + np.cumsum(rng.normal(0, 1, len(index)))
        d = pd.DataFrame({"series": key, "obs_date": index, "value": vals})
        d["avail_time"] = drivers._avail(d["obs_date"], rule)
        d["retrieved_at"], d["source"], d["unit"] = "t", provider, "u"
        rows.append(d)
    return pd.concat(rows, ignore_index=True)


def test_driver_values_are_never_used_before_they_were_available():
    close = make_series(300, start="2024-01-02")
    df = _driver_frame(close.index)
    origins = pd.Series([pd.Timestamp(d).tz_localize(NY) + pd.Timedelta(hours=13, minutes=30) for d in close.index], index=close.index)
    out = drivers.asof_features(origins, df)
    for c in [c for c in out.columns if c.startswith("__avail")]:
        avail = pd.to_datetime(out[c], utc=True)
        ok = avail.notna()
        assert (avail[ok] <= origins.dt.tz_convert("UTC")[ok]).all()
    # A same-date value is NOT usable at 13:30 (published 18:00): the join must be at least one session stale.
    first = out.filter(like="__avail_dxy").iloc[:, 0].dropna()
    assert (pd.to_datetime(first, utc=True).dt.tz_convert(NY).dt.normalize().dt.tz_localize(None) < origins.dt.tz_convert(NY).dt.normalize().dt.tz_localize(None).reindex(first.index)).all()


def test_only_matured_labels_train_a_model():
    close = make_series(700, start="2022-01-03")
    df = _driver_frame(close.index)
    for h in (1, 5, 20):
        ds = rd.build_dataset(close, h, df)
        assert ds["pos"].max() + h <= len(close) - 1  # every training target already exists
        assert ds["x_now"] is not None  # the latest origin is forecast-only: it has inputs but no label
        assert close.index[ds["pos"].max()] < close.index[-1]


# ----------------------------------------------------------------------------------- promotion rule
def test_holm_correction_is_stricter_with_more_candidates():
    assert learning.holm_pass({"a": 0.02})["a"]
    many = learning.holm_pass({"a": 0.02, **{f"n{i}": 0.5 for i in range(5)}})
    assert not many["a"]  # 0.02 survives alone but not when six candidates were compared


def _dev(version, task, mae, p, thirds=(-3, -3, -3), cov=80.0, width=3.0):
    return {"task": task, "mae": mae, "baseline_mae": 100.0, "mae_vs_baseline_pct": mae - 100.0, "p_value_vs_baseline": p,
            "by_period_vs_baseline_pct": list(thirds), "coverage_pct": cov, "mean_width_pct": width, "direction_pct": 55.0, "n_origins": 500}


def _all_dev(good_p=0.001):
    dev = {registry.champion("fwd_1")["version"]: _dev("inc", "fwd_1", 99.0, 0.5, (0, 0, 0))}
    for v in registry.versions("fwd_1"):
        if v["algorithm"] != "unchanged" and v["status"] != "champion":
            dev[v["version"]] = _dev(v["version"], "fwd_1", 99.0, 0.5, (1, 1, 1))
    dev["fwd_1:hgb:gold_v1"] = _dev("c", "fwd_1", 90.0, good_p)  # clearly better than baseline (100) and incumbent (99)
    return dev


def test_development_evidence_alone_never_promotes():
    out = learning.decide("fwd_1", _all_dev())
    assert out["decision"] == "retain"
    assert registry.champion("fwd_1")["version"] == "fwd_1:ridge:gold_v1"
    assert "effective blocks" in out["reason"]


def test_promotion_needs_forward_outcomes_then_happens_and_can_be_rolled_back():
    cand = "fwd_1:hgb:gold_v1"
    days = pd.bdate_range("2025-01-06", periods=40)
    for i in range(len(days) - 1):
        base = 100.0 + i
        actual = base + (3 if i % 2 else -3)
        _issue(version=cand, role="shadow", base_time=days[i].date().isoformat(), target=days[i + 1].date().isoformat(), predicted=actual, base=base)
    daily = pd.Series([100.0 + i + (3 if (i - 1) % 2 else -3) if i else 100.0 for i in range(len(days))], index=days)
    # make the "actual" close on each target equal what the forecasts above were built from
    daily = pd.Series([100.0] + [100.0 + (i - 1) + (3 if (i - 1) % 2 else -3) for i in range(1, len(days))], index=days)
    assert ledger.mature(daily) == len(days) - 1
    assert ledger.stats("fwd_1", version=cand)["mae"] == 0.0
    out = learning.decide("fwd_1", _all_dev())
    assert out["decision"] == "promote" and registry.champion("fwd_1")["version"] == cand
    assert registry.rollback("fwd_1") == "fwd_1:ridge:gold_v1"
    assert registry.champion("fwd_1")["version"] == "fwd_1:ridge:gold_v1"
    kinds = [d["decision"] for d in registry.decisions(10, "fwd_1")]
    assert "rollback" in kinds and "promote" in kinds


def test_a_failing_retrain_keeps_the_working_champion(monkeypatch):
    before = {t: registry.champion(t)["version"] for t in registry.TASKS}

    def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(learning, "dev_evaluate_forward", boom)
    monkeypatch.setattr(learning, "dev_evaluate_nowcast", boom)
    close = make_series(700, start="2022-01-03")
    summary = learning.run_learning(close, _driver_frame(close.index), "k")
    assert set(summary["errors"]) == {"forward", "nowcast"}
    assert {t: registry.champion(t)["version"] for t in registry.TASKS} == before


# ----------------------------------------------------------------------------------- worker
def test_job_locks_are_exclusive_so_retraining_cannot_overlap():
    with worker.file_lock("learn") as a:
        assert a
        with worker.file_lock("learn") as b:
            assert not b
    with worker.file_lock("learn") as c:
        assert c  # released afterwards


def test_a_failing_job_is_recorded_and_never_raises():
    def bad():
        raise ValueError("outage")
    assert worker.run_job("collect", bad) is None
    r = db.rows("SELECT status, detail FROM job_runs WHERE job='collect'")[0]
    assert r["status"] == "error" and "outage" in r["detail"]


def test_overdue_detection_triggers_catch_up_after_sleep():
    assert worker.cycle_overdue()
    worker.run_job("learn", lambda: {"ok": 1})
    assert not worker.cycle_overdue()
    assert worker.cycle_overdue(now=time.time() + 30 * 3600)


# ----------------------------------------------------------------------------------- news / market data
def test_syndicated_stories_are_collapsed_and_timestamps_kept():
    pub = pd.Timestamp("2026-11-16 12:00", tz="UTC").to_pydatetime()
    items = [{"title": "Gold rises as dollar slips", "source": "A", "link": "https://a.example/1?x=1", "published": pub},
             {"title": "Gold rises as dollar slips!", "source": "B", "link": "https://b.example/2", "published": pub}]
    out = news_store.ingest(items, now=1000.0)
    assert out == {"new": 1, "duplicates": 1}
    assert news_store.ingest(items, now=2000.0) == {"new": 0, "duplicates": 0}  # re-seeing an item changes nothing
    r = db.rows("SELECT published_at, first_seen_at, duplicate_of FROM news_items ORDER BY first_seen_at")
    assert r[0]["published_at"].startswith("2026-11-16T12:00") and r[0]["first_seen_at"] == 1000.0
    assert sum(1 for x in r if x["duplicate_of"]) == 1


def _tick(ts, price):
    db.connect().execute("INSERT INTO spot_ticks (ts, price, retrieved) VALUES (?,?,?)", (ts, price, ts))


def test_spot_summary_uses_the_fx_session_convention(monkeypatch):
    et = lambda s: pd.Timestamp(s, tz=NY).timestamp()  # noqa: E731
    _tick(et("2026-11-16 16:55"), 100.0)   # last price before the 17:00 ET roll = previous close
    _tick(et("2026-11-16 17:01"), 101.0)   # first price of the new session = open
    _tick(et("2026-11-17 11:00"), 99.0)
    _tick(et("2026-11-17 13:59:50"), 105.0)
    monkeypatch.setattr(market.time, "time", lambda: et("2026-11-17 14:00"))
    s = market.spot_summary()
    assert (s["previous_close"], s["session"]["open"], s["session"]["high"], s["session"]["low"], s["price"]) == (100.0, 101.0, 105.0, 99.0, 105.0)
    assert s["change"] == 5.0 and s["change_pct"] == 5.0
    assert s["status"]["state"] == "live"
    monkeypatch.setattr(market.time, "time", lambda: et("2026-11-17 14:05"))  # the same tick, 5 minutes later, is no longer "live"
    assert market.spot_summary()["status"]["state"] == "delayed"


def test_spot_candles_come_only_from_recorded_ticks_and_gaps_stay_gaps(monkeypatch):
    now = pd.Timestamp("2026-11-17 14:00", tz=NY).timestamp()
    monkeypatch.setattr(market.time, "time", lambda: now)
    for off, price in [(-3600 * 5, 100.0), (-3600 * 5 + 60, 102.0), (-300, 101.0)]:  # a 5-hour hole in between
        _tick(now + off, price)
    bars = market.spot_ohlc("1D")["bars"]
    assert len(bars) == 2  # no candles invented for the hole
    assert (bars[0]["open"], bars[0]["high"], bars[0]["low"], bars[0]["close"]) == (100.0, 102.0, 100.0, 102.0)


def test_status_labels_never_call_delayed_data_real_time():
    assert market._status(30, True, 60)["state"] == "live"
    assert market._status(600, True, 600)["state"] == "delayed"
    assert market._status(4000, True, 60)["state"] == "stale"
    assert market._status(4000, False, 60)["state"] == "closed"
    assert market._status(None, True, 60)["state"] == "unavailable"


# ----------------------------------------------------------------------------------- nowcast (no look-ahead)
def synthetic_hourly(daily: pd.Series, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for day, close in daily.items():
        start = (day - pd.Timedelta(days=1)).tz_localize(NY) + pd.Timedelta(hours=18)
        times = [start + pd.Timedelta(hours=i) for i in range(20)]
        path = close * np.exp(rng.normal(0, 0.002, len(times)))
        path[-1] = close
        rows += list(zip(times, path))
    return pd.DataFrame(rows, columns=["t", "c"]).sort_values("t").reset_index(drop=True)


def test_session_label_and_target_session_skip_weekends():
    assert nowcast.session_label(pd.Timestamp("2026-11-15 19:00", tz=NY)) == pd.Timestamp("2026-11-16")
    assert nowcast.session_label(pd.Timestamp("2026-11-14 12:00", tz=NY)) is None
    assert nowcast.target_session(pd.Timestamp("2026-11-14 12:00", tz=NY)) == pd.Timestamp("2026-11-16")  # Saturday -> Monday
    assert nowcast.target_session(pd.Timestamp("2026-11-17 10:00", tz=NY)) == pd.Timestamp("2026-11-17")  # before settlement: today
    assert nowcast.target_session(pd.Timestamp("2026-11-17 14:00", tz=NY)) == pd.Timestamp("2026-11-18")  # after settlement: next session


def test_intraday_frame_uses_only_earlier_information_and_audits_sessions():
    daily = make_series(320, start="2024-01-08")
    rows = nowcast.training_rows(nowcast.build_frame(synthetic_hourly(daily), daily))
    assert (rows["prev_close"] != rows["session_close"]).mean() > 0.99
    np.testing.assert_allclose(np.log(rows["session_close"]) - np.log(rows["c"]), rows["target"])
    cal = nowcast.calibrate(rows)
    assert cal["n_sessions"] < cal["n_test"] / 5  # many rows per session: sessions, not rows, are the honest sample size
    lo, hi = cal["coverage_ci90_pct"]
    assert lo <= cal["coverage_by_session_pct"] <= hi


def test_fresh_completed_session_triggers_learning_but_only_once():
    close = make_series(30, start="2025-01-06")
    assert worker.needs_learning(close)  # nothing issued yet
    base = close.index[-1].date().isoformat()
    _issue(task="fwd_5", base_time=base, target=(close.index[-1] + pd.offsets.BDay(5)).date().isoformat())
    assert not worker.needs_learning(close)  # already issued for this session
    assert worker.needs_learning(make_series(31, start="2025-01-06"))  # a new completed session arrived
