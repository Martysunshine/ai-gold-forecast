import pandas as pd
import pytest

from app import sessions


def d(s):
    return pd.Timestamp(s)


def test_skips_weekends_and_full_closures():
    # Fri 2026-12-24 -> next bar is Mon 2026-12-28 (Christmas Fri 25th closed, weekend skipped)
    assert sessions.next_sessions(d("2026-12-24"), 1)[0] == d("2026-12-28")
    # Thu 2026-12-31 -> Fri 2027-01-01 is closed
    assert sessions.next_sessions(d("2026-12-31"), 1)[0] == d("2027-01-04")
    # Good Friday 2027-03-26 is a full closure
    assert d("2027-03-26") not in sessions.sessions_between(d("2027-03-22"), d("2027-03-30"))


def test_us_holiday_sessions_without_a_daily_bar_are_skipped():
    # Thanksgiving 2026-11-26: CME runs an abbreviated session but no separate daily bar is produced
    days = sessions.sessions_between(d("2026-11-23"), d("2026-11-30"))
    assert d("2026-11-26") not in days and d("2026-11-27") in days  # day after Thanksgiving does have a bar


def test_known_horizons_from_a_real_origin():
    seq = sessions.next_sessions(d("2026-11-12"), 20)
    assert [x.date().isoformat() for x in (seq[0], seq[4], seq[19])] == ["2026-11-13", "2026-11-19", "2026-12-11"]
    assert all(x.day_of_week < 5 for x in seq)
    assert all(b > a for a, b in zip(seq, seq[1:]))


def test_origin_itself_is_excluded():
    assert sessions.next_sessions(d("2026-11-12"), 1)[0] > d("2026-11-12")


def test_verify_reports_counts_against_observed_bars():
    full = sessions.sessions_between(d("2024-01-02"), d("2024-12-31"))
    series = pd.Series(1.0, index=full)
    v = sessions.verify(series)
    assert v["bars_not_in_rule"] == [] and v["rule_sessions_without_bar"] == []
    assert v["unfiltered_gold_calendar_days_without_bar"] > 0  # US holiday sessions the rule drops
    thinner = series.drop(series.index[10])
    assert sessions.verify(thinner)["rule_sessions_without_bar"] == [series.index[10].date().isoformat()]
