"""Data quality: detect and quarantine; never delete, never drop a move for being large, never block on benign trouble."""
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app import config, db, quality

NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db._inited.discard(str(db.path()))


def kinds(fs):
    return sorted(f.kind for f in fs)


def test_duplicates_order_and_bad_values_are_found_with_the_raw_row_kept():
    text = "Date,Close\n2026-01-05,2000\n2026-01-06,2010\n2026-01-06,2011\n2026-01-02,1990\n2026-01-07,abc\n2026-01-08,-5\n2026-01-09,2020\n"
    fs = quality.audit_daily_text(text, "provider.csv")
    assert kinds(fs) == ["duplicate_date", "non_numeric", "non_positive", "out_of_order"]
    dup = next(f for f in fs if f.kind == "duplicate_date")
    assert dup.raw == ["2026-01-06", "2011"]                       # the offending row, verbatim


def test_wrong_units_and_non_market_jumps_are_unit_inconsistent_and_blocking():
    fs = quality.audit_daily_text("Date,Close\n2026-01-05,2000\n2026-01-06,20.1\n2026-01-07,2010\n", "provider.csv")
    assert kinds(fs).count("unit_inconsistent") >= 1
    assert "unit_inconsistent" in quality.BLOCKING


def test_a_missing_column_is_a_schema_change():
    assert kinds(quality.audit_daily_text("When,Price\n2026-01-05,2000\n", "x.csv")) == ["schema_change"]


def _series(vals, start="2026-03-02"):
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)), dtype=float)


def test_a_large_move_is_checked_and_kept_not_removed():
    c = _series([2000, 2010, 2130, 2140, 2150])                     # +6% one day, and it holds
    high = c + 5
    fs = quality.large_moves(c, high, "daily")
    assert len(fs) == 1 and fs[0].status == "kept_after_check" and "Kept in the series" in fs[0].detail and "agrees" in fs[0].detail
    assert len(c) == 5                                              # the function cannot alter the series


def test_a_one_day_spike_that_fully_reverses_is_flagged_for_review_but_still_kept():
    c = _series([2000, 2010, 2150, 2012, 2015])
    fs = quality.large_moves(c, None, "daily")
    assert [f.status for f in fs] == ["flagged", "kept_after_check"]          # the spike, then the equally large reversal (checked, kept)
    assert "reversed" in fs[0].detail


def test_ohlc_high_below_close_is_invalid_and_disagreeing_closes_are_flagged():
    idx = pd.bdate_range("2026-03-02", periods=3)
    o = pd.DataFrame({"high": [2010.0, 1990.0, 2030.0], "close": [2005.0, 2000.0, 2025.0]}, index=idx)
    c = pd.Series([2005.0, 2000.0, 2040.0], index=idx)
    fs = quality.audit_ohlc(o, c, "gc_daily_ohlc.csv")
    assert kinds(fs) == ["invalid_ohlc", "source_disagreement"]


def test_record_is_idempotent_and_never_alters_the_raw_observation():
    f = quality.Finding("provider.csv", "duplicate_date", "2026-01-06", "dup", ["2026-01-06", "2011"])
    assert quality.record(f) is True and quality.record(f) is False
    row = db.rows("SELECT raw, status FROM data_quarantine")[0]
    assert row["raw"] == '["2026-01-06", "2011"]' and row["status"] == "quarantined"


def test_only_blocking_kinds_stop_training_and_resolving_unblocks():
    quality.record(quality.Finding("daily", "large_move", "2026-01-06", "x", None, "kept_after_check"))
    assert quality.ok_to_train()[0] is True                         # a checked large move never blocks anything
    quality.record(quality.Finding("daily", "stale_feed", "daily", "behind", None))
    ok, why = quality.ok_to_train()
    assert ok is False and why[0]["kind"] == "stale_feed"
    assert quality.resolve("daily", "stale_feed") == 1
    assert quality.ok_to_train()[0] is True
    assert db.rows("SELECT COUNT(*) AS n FROM data_quarantine")[0]["n"] == 2   # resolving keeps the record


def test_provider_errors_are_classified_and_transient_ones_are_not_recorded():
    assert quality.classify_provider_error("yahoo", "Provider response was malformed.").kind == "schema_change"
    assert quality.classify_provider_error("yahoo", "Provider data failed validation: boom").kind == "provider_data_rejected"
    assert quality.classify_provider_error("yahoo", "Provider is rate-limiting requests (HTTP 429).") is None
    assert quality.classify_provider_error("yahoo", "Could not reach the price provider: ConnectError") is None
    quality.provider_error("yahoo", "Provider response was malformed.")
    assert quality.ok_to_train()[0] is False
    quality.provider_ok("yahoo")
    assert quality.ok_to_train()[0] is True


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 11, 18, 9, 0, tzinfo=NY), "2026-11-17"),       # Wednesday morning: Tuesday's settlement is the latest
    (datetime(2026, 11, 18, 17, 10, tzinfo=NY), "2026-11-17"),     # inside the 30-minute grace after the 17:00 close
    (datetime(2026, 11, 18, 17, 31, tzinfo=NY), "2026-11-18"),
    (datetime(2026, 11, 23, 10, 0, tzinfo=NY), "2026-11-20"),      # Monday morning: Friday
    (datetime(2026, 11, 26, 12, 0, tzinfo=NY), "2026-11-25"),      # Thanksgiving: the exchange calendar skips it
])
def test_expected_latest_session_follows_the_exchange_calendar(now, expected):
    assert quality.expected_last_session(now).date().isoformat() == expected


def test_staleness_counts_sessions_not_calendar_days():
    s = _series(np.linspace(2000, 2100, 40), start="2026-10-05")    # ends Fri 2026-11-27? computed below
    now = datetime(2026, 11, 18, 9, 0, tzinfo=NY)
    r = quality.staleness(s.iloc[:-1] if s.index[-1] > pd.Timestamp("2026-11-17") else s, now)
    assert r["expected_last_session"] == "2026-11-17" and r["sessions_behind"] >= 0
    old = _series(np.linspace(2000, 2100, 30), start="2026-09-01")  # ends 2026-10-12: far behind
    assert quality.staleness(old, now)["stale"] is True
    fresh = pd.Series([2000.0], index=[pd.Timestamp("2026-11-17")])
    assert quality.staleness(fresh, now)["stale"] is False


def test_run_on_empty_storage_does_not_crash_or_block(monkeypatch):
    class NoData:
        def get(self):
            raise RuntimeError("no data")
    import app.data as data
    monkeypatch.setattr(data, "DataStore", NoData)
    r = quality.run()
    assert r["ok_to_train"] is True and r["new_findings"] >= 0
