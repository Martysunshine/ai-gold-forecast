"""Trading-session calendar used for forecast dates.

Horizons are counted in *daily bars* of the price series, so forecast dates must follow the days on which a
bar is actually produced. Rule (verified against five years of GC=F bars, see ``verify``):

    CME Globex gold sessions  (pandas_market_calendars "CMEGlobex_Gold": no weekends, full-closure holidays)
    minus US exchange holidays (the "NYSE" calendar)

On US holidays such as Thanksgiving or Independence Day CME runs an abbreviated session, but the data
provider records no separate daily bar for most of them (33 of 35 in the verification window), so they are skipped.

Run ``python -m app.sessions`` from ``backend/`` to repeat the verification against live data.
"""
from __future__ import annotations

import functools

import pandas as pd

GOLD_CALENDAR = "CMEGlobex_Gold"
EXCHANGE_HOLIDAYS = "NYSE"


@functools.lru_cache(maxsize=1)
def _calendars():
    import pandas_market_calendars as mcal

    return mcal.get_calendar(GOLD_CALENDAR), mcal.get_calendar(EXCHANGE_HOLIDAYS), mcal.__version__


def _days(cal, start, end) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(cal.valid_days(start, end)).tz_localize(None).normalize()


def sessions_between(start, end) -> pd.DatetimeIndex:
    gold, nyse, _ = _calendars()
    return _days(gold, start, end).intersection(_days(nyse, start, end))


def next_sessions(last: pd.Timestamp, n: int) -> list[pd.Timestamp]:
    """The next ``n`` daily bars after ``last`` (``last`` itself excluded)."""
    start = pd.Timestamp(last).normalize() + pd.Timedelta(days=1)
    span = max(14, n * 2 + 14)
    days = sessions_between(start, start + pd.Timedelta(days=span))
    if len(days) < n:
        raise RuntimeError("calendar window too short")
    return list(days[:n])


def describe() -> dict:
    _, _, version = _calendars()
    return {
        "name": f"{GOLD_CALENDAR} excluding US exchange holidays",
        "library": f"pandas_market_calendars {version}",
        "rule": "CME Globex gold sessions minus US exchange holidays; weekends and holiday closures are skipped.",
    }


def verify(series: pd.Series) -> dict:
    """Compare the calendar rule with the bars actually present in ``series``."""
    obs = set(series.index.normalize())
    cal = set(sessions_between(series.index[0], series.index[-1]))
    gold, _, _ = _calendars()
    full_gold = set(_days(gold, series.index[0], series.index[-1]))
    return {
        "observed_bars": len(obs),
        "rule_sessions": len(cal),
        "rule_sessions_without_bar": sorted(d.date().isoformat() for d in cal - obs),
        "bars_not_in_rule": sorted(d.date().isoformat() for d in obs - cal),
        "unfiltered_gold_calendar_days_without_bar": len(full_gold - obs),
    }


if __name__ == "__main__":  # pragma: no cover - needs network
    from . import data

    series = data.fetch_yahoo().series
    print({"span": f"{series.index[0].date()} -> {series.index[-1].date()}", **verify(series)})
