import io

import numpy as np
import pandas as pd
import pytest

from app import data
from app.data import ValidationError, parse_csv_bytes, validate_prices
from conftest import make_series


def frame(s: pd.Series, date="Date", close="Close"):
    return pd.DataFrame({date: s.index.strftime("%Y-%m-%d"), close: s.to_numpy()})


def test_valid_frame_passes(series):
    out, warnings = validate_prices(frame(series))
    assert len(out) == len(series) and out.index.is_monotonic_increasing and warnings == []


def test_accepts_alternate_column_names(series):
    out, _ = validate_prices(frame(series, date="timestamp", close="Adj Close"))
    assert len(out) == len(series)


def test_missing_columns_rejected(series):
    with pytest.raises(ValidationError, match="required columns"):
        validate_prices(pd.DataFrame({"foo": [1], "bar": [2]}))


@pytest.mark.parametrize("bad", [0.0, -5.0])
def test_non_positive_price_rejected(series, bad):
    df = frame(series)
    df.loc[10, "Close"] = bad
    with pytest.raises(ValidationError, match="positive"):
        validate_prices(df)


def test_unparseable_date_rejected(series):
    df = frame(series)
    df.loc[5, "Date"] = "not-a-date"
    with pytest.raises(ValidationError, match="unparseable date"):
        validate_prices(df)


def test_too_few_rows_rejected():
    with pytest.raises(ValidationError, match="at least"):
        validate_prices(frame(make_series(50)))


def test_future_dates_rejected(series):
    df = frame(series)
    df.loc[len(df) - 1, "Date"] = "2999-01-01"
    with pytest.raises(ValidationError, match="future"):
        validate_prices(df)


def test_implausible_jump_rejected(series):
    df = frame(series)
    df.loc[300, "Close"] = df.loc[299, "Close"] * 3
    with pytest.raises(ValidationError, match="Implausible"):
        validate_prices(df)


def test_duplicates_and_order_are_repaired_with_warnings(series):
    df = frame(series)
    df = pd.concat([df.iloc[::-1], df.iloc[[5]]], ignore_index=True)
    out, warnings = validate_prices(df)
    assert out.index.is_unique and out.index.is_monotonic_increasing
    assert any("duplicate" in w for w in warnings) and any("sorted" in w for w in warnings)


def test_many_missing_prices_rejected(series):
    df = frame(series)
    df.loc[:100, "Close"] = np.nan
    with pytest.raises(ValidationError, match="missing"):
        validate_prices(df)


def test_few_missing_prices_dropped_with_warning(series):
    df = frame(series)
    df.loc[[3, 4], "Close"] = np.nan
    out, warnings = validate_prices(df)
    assert len(out) == len(series) - 2 and any("Dropped 2" in w for w in warnings)


def test_csv_with_thousands_separators_and_dollar_signs(series):
    df = frame(series)
    df["Close"] = df["Close"].map(lambda v: f"${v:,.2f}")
    out, _ = parse_csv_bytes(df.to_csv(index=False).encode())
    assert np.allclose(out.to_numpy(), np.round(series.to_numpy(), 2), atol=0.01)


def test_csv_rejects_empty_and_binary():
    with pytest.raises(ValidationError):
        parse_csv_bytes(b"   ")
    with pytest.raises(ValidationError):
        parse_csv_bytes(b"\xff\xfe\x00garbage")


def test_yahoo_payload_parsing_and_partial_bar_drop(series):
    s = series.iloc[-420:]
    stamps = [int(t.timestamp()) + 14 * 3600 for t in s.index]  # midday UTC
    payload = {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [{"close": s.tolist()}]},
                                     "meta": {"gmtoffset": 0, "regularMarketTime": stamps[-1],
                                              "currentTradingPeriod": {"regular": {"end": stamps[-1] + 3600}}}}], "error": None}}
    res = data.parse_yahoo_chart(payload, now=stamps[-1] + 60)  # session still open -> last bar dropped
    assert res.dropped_partial_bar and len(res.series) == len(s) - 1
    res2 = data.parse_yahoo_chart(payload, now=stamps[-1] + 7200)  # session closed -> kept
    assert not res2.dropped_partial_bar and len(res2.series) == len(s)


def test_yahoo_error_payload_raises():
    with pytest.raises(data.DataUnavailable):
        data.parse_yahoo_chart({"chart": {"result": None, "error": {"description": "nope"}}})


def test_store_fallbacks(tmp_path, series):
    calls = {"n": 0}

    def ok():
        calls["n"] += 1
        return data.FetchResult(series=series.iloc[-500:])

    def boom():
        raise data.DataUnavailable("rate limited")

    # no cache + provider failure -> explicit error, never synthetic data
    with pytest.raises(data.DataUnavailable, match="Upload a CSV"):
        data.DataStore(tmp_path / "a", fetcher=boom).get()

    store = data.DataStore(tmp_path / "b", fetcher=ok)
    first = store.get()
    assert first.meta["source_kind"] == "provider" and calls["n"] == 1
    store.get()
    assert calls["n"] == 1  # served from cache

    # failed refresh keeps the cached real data and flags it
    store._fetcher = boom
    stale = store.get(force_refresh=True)
    assert stale.meta["provider_error"] and len(stale.series) == 500 and any("failed" in w for w in stale.meta["warnings"])


def test_upload_is_persisted_and_labelled(tmp_path, series):
    store = data.DataStore(tmp_path, fetcher=lambda: (_ for _ in ()).throw(data.DataUnavailable("x")))
    ds = store.save_upload(frame(series).to_csv(index=False).encode(), "my gold.csv")
    assert ds.meta["source_kind"] == "upload" and "my gold.csv" in ds.meta["source"]
    assert store.get().meta["source_kind"] == "upload"  # uploads stay active without hitting the provider
