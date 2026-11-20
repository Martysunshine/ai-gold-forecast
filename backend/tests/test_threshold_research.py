"""Threshold-research tests. Synthetic data verifies the MACHINERY (alignment, purging, no look-ahead, gates, metrics, immutability); it says
nothing about gold."""
import sqlite3

import numpy as np
import pandas as pd
import pytest

from app import config, db, threshold_research as tr
from conftest import make_series


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db._inited.discard(str(db.path()))
    yield


def ohlc_for(close: pd.Series, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"high": close * (1 + np.abs(rng.normal(0, 0.006, len(close)))), "close": close})


# ------------------------------------------------------------------------------------------------ data
def _payload(stamps, highs, closes):
    return {"chart": {"result": [{"timestamp": stamps, "meta": {"exchangeTimezoneName": "America/New_York"}, "indicators": {"quote": [{"high": highs, "close": closes}]}}]}}


def test_parse_ohlc_drops_bad_rows_and_the_unfinished_session():
    day = lambda d: int(pd.Timestamp(d, tz="America/New_York").timestamp()) + 3600   # noqa: E731
    stamps = [day("2026-11-12"), day("2026-11-13"), day("2026-11-16")]
    p = _payload(stamps, [3950.0, 3900.0, 3920.0], [3940.0, 3910.0, 3915.0])          # 13 Nov: high below the close = data error
    now_open = pd.Timestamp("2026-11-16 12:00", tz="America/New_York").timestamp()    # 16 Nov session still trading
    df = tr.parse_ohlc(p, now=now_open)
    assert list(df.index.strftime("%Y-%m-%d")) == ["2026-11-12"]
    now_closed = pd.Timestamp("2026-11-16 18:00", tz="America/New_York").timestamp()
    assert list(tr.parse_ohlc(p, now=now_closed).index.strftime("%Y-%m-%d")) == ["2026-11-12", "2026-11-16"]


# ------------------------------------------------------------------------------------------------ dataset alignment / look-ahead
def test_targets_use_the_NEXT_bars_high_and_only_matured_rows_are_used():
    close = make_series(700)
    oh = ohlc_for(close)
    ds = tr.build(close, oh["high"], None)
    i = 300
    row = np.where(ds["pos"] == i)[0][0]
    assert ds["y_hi"][row] == pytest.approx(np.log(oh["high"].iloc[i + 1] / close.iloc[i]))
    assert ds["y_cl"][row] == pytest.approx(np.log(close.iloc[i + 1] / close.iloc[i]))
    assert ds["pos"].max() == len(close) - 2           # the latest origin has no matured outcome: never trained on
    assert ds["x_now"] is not None and ds["now_date"] == close.index[-1]


def test_walk_forward_probabilities_ignore_the_future():
    """Changing highs/closes AFTER an origin must not change that origin's probability (no look-ahead, labels purged)."""
    close = make_series(800)
    oh = ohlc_for(close)
    a = tr.walk_forward(tr.build(close, oh["high"], None), tr.GOLD_COLS)["prob"]
    close2, oh2 = close.copy(), oh.copy()
    cut = 650
    close2.iloc[cut:] *= 1.15
    oh2["high"] = oh2["high"].where(oh2.index < close.index[cut], oh2["high"] * 1.15)
    b = tr.walk_forward(tr.build(close2, oh2["high"], None), tr.GOLD_COLS)["prob"]
    ds = tr.build(close, oh["high"], None)
    early = np.where(ds["pos"] < cut - 3)[0]
    early = early[~np.isnan(a[early, 0])]
    assert len(early) > 100
    np.testing.assert_allclose(a[early], b[early], rtol=1e-9, atol=1e-12)


def test_training_rows_are_purged_before_each_block(monkeypatch):
    close = make_series(700)
    ds = tr.build(close, ohlc_for(close)["high"], None)
    seen = []
    orig = tr._fit

    def spy(ds_, rows, cols, target):
        seen.append(rows.copy())
        return orig(ds_, rows, cols, target)
    monkeypatch.setattr(tr, "_fit", spy)
    tr.walk_forward(ds, tr.GOLD_COLS)
    n = len(ds["pos"])
    pos = ds["pos"]
    starts = [i for i in range(int(n * tr.OOS_START_FRAC), n, tr.REFIT) if (pos + 2 < pos[i]).sum() >= tr.MIN_TRAIN]   # blocks too early to fit are skipped
    assert len(starts) == len(seen) and seen and all(ds["pos"][r].max() + 2 < ds["pos"][s] for r, s in zip(seen, starts))     # label window ends before the block + 1-session embargo


# ------------------------------------------------------------------------------------------------ metrics
def test_confusion_precision_recall_false_alerts_missed_events():
    p = np.array([0.9, 0.8, 0.7, 0.1, 0.1, 0.1])
    y = np.array([True, True, False, True, False, False])
    c = tr.confusion(p, y, 0.5)
    assert (c["alerts"], c["true_alerts"], c["false_alerts"], c["missed_events"], c["events"]) == (3, 2, 1, 1, 3)
    assert c["precision"] == pytest.approx(2 / 3, abs=1e-3) and c["recall"] == pytest.approx(2 / 3, abs=1e-3)
    none = tr.confusion(np.array([0.1, 0.1]), np.array([False, False]), 0.5)
    assert none["precision"] is None and none["recall"] is None                                   # undefined, not 0 or 1


def test_calibration_of_a_calibrated_forecaster_has_small_error():
    rng = np.random.default_rng(0)
    p = rng.uniform(0, 1, 20000)
    y = rng.uniform(0, 1, 20000) < p
    assert tr.calibration(p, y)["ece"] < 0.02
    assert tr.calibration(np.full(1000, 0.9), np.zeros(1000, bool))["ece"] > 0.85


def test_cutoff_is_chosen_only_from_the_slice_it_is_given():
    rng = np.random.default_rng(1)
    p = rng.uniform(0, 1, 3000)
    y = rng.uniform(0, 1, 3000) < p
    assert 0.05 <= tr.pick_cut(p, y) <= 0.9
    assert tr.pick_cut(np.array([0.1, 0.2]), np.array([False, False])) == 0.5      # no events: falls back, never errors


# ------------------------------------------------------------------------------------------------ end to end + gate
def test_run_end_to_end_writes_a_result_with_the_required_reporting(tmp_path):
    close = make_series(1500, vol=0.012)
    oh = ohlc_for(close)
    r = tr.run(close, oh, None, [float(close.iloc[-1] * 1.03), float(close.iloc[-1] * 1.08)])
    t = r["touch"]
    assert t["selected"] in t["variants"] and "z_only" in t["variants"] and "gold" in t["variants"] and "gold_drivers" not in t["variants"]
    fin = t["variants"][t["selected"]]["final"]
    for k in ("brier", "brier_climatology", "log_loss", "ece", "reliability", "at_cut"):
        assert k in fin
    for k in ("precision", "recall", "false_alerts", "missed_events", "events"):
        assert k in fin["at_cut"]
    assert r["lead_time"]["min_h"] == 4.5 and "13:30" in r["lead_time"]["note"]
    assert (tmp_path / "threshold_research.json").exists() and tr.latest()["data_through"] == str(close.index[-1].date())
    assert t["selection"][1] < t["final"][0]                # the two slices are disjoint and chronological


def test_gate_keeps_the_target_experimental_without_evidence():
    res = {"touch": {"live_levels": [{"level": 4100, "z": 3.9, "final_slice": {"in_range": True, "events": 2, "gap": 0.005}}], "brier_skill_vs_climatology_final": 0.3}}
    g = tr.gate(res, 4100)
    assert g["display"] is False and "only 2 historical events" in g["reasons"][0]
    far = {"touch": {"live_levels": [{"level": 5000, "z": 9.0, "final_slice": {"in_range": False}}], "brier_skill_vs_climatology_final": 0.3}}
    assert tr.gate(far, 5000)["display"] is False
    ok = {"touch": {"live_levels": [{"level": 4000, "z": 2.3, "final_slice": {"in_range": True, "events": 55, "gap": 0.01}}], "brier_skill_vs_climatology_final": 0.3}}
    assert tr.gate(ok, 4000)["display"] is True
    miscal = {"touch": {"live_levels": [{"level": 4000, "z": 2.3, "final_slice": {"in_range": True, "events": 55, "gap": 0.09}}], "brier_skill_vs_climatology_final": 0.3}}
    assert tr.gate(miscal, 4000)["display"] is False


def test_band_counts_distinct_dates_not_correlated_rows():
    prob = np.full((10, len(tr.Z_GRID)), 0.1)
    lab = np.zeros((10, len(tr.Z_GRID)), bool)
    lab[2, :] = True                                    # ONE date that touched at every distance
    b = tr.band_stats(prob, lab, np.arange(10), 2.0)
    assert b["events"] == 1 and b["event_rows"] > 1


def test_richer_variant_needs_a_selection_margin_to_replace_the_distance_baseline():
    close = make_series(1500, vol=0.012)
    ds = tr.build(close, ohlc_for(close)["high"], None)
    out = tr.run_target(ds, "touch", [float(close.iloc[-1] * 1.03)])
    assert "selection slice" in out["selection_rule"] and out["selected"] in ("z_only", "gold")


# ------------------------------------------------------------------------------------------------ forward record
def test_forward_record_is_written_before_outcomes_and_never_edited():
    close = make_series(1500, vol=0.012)
    oh = ohlc_for(close)
    level = float(close.iloc[-1] * 1.03)
    research = tr.run(close, oh, None, [level])
    r = tr.record_forward(close, oh, None, [level], research, now=1_700_000_000.0)
    assert r["recorded"] == 1
    assert tr.record_forward(close, oh, None, [level], research, now=1_700_000_100.0)["recorded"] == 0       # idempotent
    row = db.rows("SELECT * FROM touch_forecasts")[0]
    assert 0 < row["probability"] < 1 and row["target_date"] > row["origin_date"] and row["displayed"] in (0, 1)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.connect().execute("UPDATE touch_forecasts SET probability=0.5")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.connect().execute("DELETE FROM touch_forecasts")
    assert tr.mature_forward(oh) == 0                    # the outcome does not exist yet
    # the next bar arrives: only now can it be scored, once
    nxt = pd.Timestamp(row["target_date"])
    oh2 = pd.concat([oh, pd.DataFrame({"high": [level + 5], "close": [level]}, index=[nxt])])
    assert tr.mature_forward(oh2) == 1 and tr.mature_forward(oh2) == 0
    out = db.rows("SELECT * FROM touch_outcomes")[0]
    assert out["hit"] == 1
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        db.connect().execute("UPDATE touch_outcomes SET hit=0")
    s = tr.forward_summary()
    assert (s["recorded"], s["scored"], s["hits"]) == (1, 1, 1)


def test_panel_without_a_research_run_is_experimental():
    p = tr.panel()
    assert p["status"] == "experimental" and "not run" in p["reason"]
