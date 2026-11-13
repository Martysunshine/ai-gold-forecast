import json
from datetime import datetime, timezone

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import alpaca, config, data, main
from app.news import NewsService
from conftest import make_series

KEY, SECRET = "PKTESTKEYID12345", "SECRETVALUE98765"


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_KEY_ID", KEY)
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", SECRET)
    monkeypatch.setattr(config, "ALPACA_DATA_FEED", "iex")


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_KEY_ID", "")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "")


def bars_for(days, start_price=180.0):
    """Alpaca-style daily bars: timestamp = midnight New York time expressed in UTC (04:00Z in summer, 05:00Z in winter)."""
    out = []
    for i, d in enumerate(days):
        t = pd.Timestamp(d).tz_localize("America/New_York").tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"t": t, "o": 1, "h": 1, "l": 1, "c": round(start_price * (1 + 0.0004 * i + 0.003 * np.sin(i)), 2), "v": 1000})
    return out


def client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


AFTER_CLOSE = datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc)  # 18:00 New York


def test_pagination_headers_and_dst_correct_dates(keys):
    days = pd.bdate_range("2024-01-02", periods=600)
    bars = bars_for(days)
    seen = []

    def handler(req):
        seen.append(req)
        assert req.headers["APCA-API-KEY-ID"] == KEY and req.headers["APCA-API-SECRET-KEY"] == SECRET
        assert req.url.params["symbols"] == "GLD" and req.url.params["timeframe"] == "1Day" and req.url.params["feed"] == "iex"
        if "page_token" not in req.url.params:
            return httpx.Response(200, json={"bars": {"GLD": bars[:350]}, "next_page_token": "abc"})
        assert req.url.params["page_token"] == "abc"
        return httpx.Response(200, json={"bars": {"GLD": bars[350:]}, "next_page_token": None})

    s = alpaca.fetch_gld(client_with(handler), now=AFTER_CLOSE)
    assert len(seen) == 2 and len(s) == 600
    assert list(s.index) == list(days)  # summer (04:00Z) and winter (05:00Z) bars both keep their New York date
    assert not any(str(r.url).find(SECRET) >= 0 or str(r.url).find(KEY) >= 0 for r in seen)  # keys only in headers


def test_in_progress_session_bar_is_dropped(keys):
    days = pd.bdate_range(end="2026-11-13", periods=600)  # last bar dated today (Fri 13 Nov 2026)
    bars = bars_for(days)
    during = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)  # 12:00 New York, session open
    assert len(alpaca.parse_bars(bars, now=during)) == 599
    assert len(alpaca.parse_bars(bars, now=AFTER_CLOSE)) == 600


@pytest.mark.parametrize("status,needle", [(401, "rejected the API keys"), (403, "rejected the API keys"), (429, "rate-limiting"), (500, "HTTP 500"), (422, "ALPACA_DATA_FEED")])
def test_http_errors_map_to_fixed_messages_without_keys(keys, status, needle):
    with pytest.raises(alpaca.AlpacaError) as e:
        alpaca.fetch_gld(client_with(lambda req: httpx.Response(status, json={"message": f"bad key {KEY}"})), now=AFTER_CLOSE)
    msg = str(e.value)
    assert needle in msg and KEY not in msg and SECRET not in msg


def test_network_and_parse_errors(keys):
    def boom(req):
        raise httpx.ConnectError("connection refused", request=req)

    with pytest.raises(alpaca.AlpacaError, match="Could not reach Alpaca"):
        alpaca.fetch_gld(client_with(boom), now=AFTER_CLOSE)
    with pytest.raises(alpaca.AlpacaError, match="unreadable"):
        alpaca.fetch_gld(client_with(lambda r: httpx.Response(200, content=b"not json")), now=AFTER_CLOSE)
    with pytest.raises(alpaca.AlpacaError, match="no GLD bars"):
        alpaca.fetch_gld(client_with(lambda r: httpx.Response(200, json={"bars": {}, "next_page_token": None})), now=AFTER_CLOSE)


def test_not_configured_never_calls_alpaca(no_keys):
    assert alpaca.configured() is False
    with pytest.raises(alpaca.AlpacaError, match="not configured"):
        alpaca.fetch_gld(client_with(lambda r: pytest.fail("Alpaca must not be called without keys")))


def test_store_keeps_last_valid_data_on_failure(tmp_path, keys):
    good = pd.Series(np.linspace(180, 200, 600), index=pd.bdate_range("2024-01-02", periods=600))
    state = {"fail": False}

    def fetcher():
        if state["fail"]:
            raise alpaca.AlpacaError("Alpaca is rate-limiting requests (HTTP 429). Try again shortly.")
        return good

    store = alpaca.GldStore(tmp_path, fetcher=fetcher)
    first = store.get()
    assert len(first.series) == 600 and "provider_error" not in first.meta
    state["fail"] = True
    again = store.get(force=True)
    assert len(again.series) == 600 and "429" in again.meta["provider_error"]
    with pytest.raises(alpaca.AlpacaError):
        alpaca.GldStore(tmp_path / "empty", fetcher=fetcher).get()


def test_compare_maths():
    idx = pd.bdate_range("2024-01-02", periods=400)
    gc = make_series(400, seed=2).set_axis(idx)
    same = gc * 0.092  # a scaled copy: identical indexed path, correlation 1
    out = alpaca.compare(gc, same)
    assert out["indexed"][0]["gc"] == 100 and out["indexed"][0]["gld"] == 100
    assert all(abs(r["gc"] - r["gld"]) < 0.01 for r in out["indexed"])
    w = out["window"]
    assert w["return_correlation"] == pytest.approx(1.0) and w["gc_return_pct"] == pytest.approx(w["gld_return_pct"])
    assert w["sessions"] == 252
    noisy = same * np.exp(np.random.default_rng(0).normal(0, 0.01, 400))
    assert -1 <= alpaca.compare(gc, noisy)["window"]["return_correlation"] < 0.99
    with pytest.raises(alpaca.AlpacaError, match="overlap"):
        alpaca.compare(gc.iloc[:30], same.iloc[200:])


# ------------------------------------------------------------------ API
@pytest.fixture
def api(tmp_path, monkeypatch):
    gold = make_series(800, start="2023-01-02")
    monkeypatch.setattr(main, "store", data.DataStore(tmp_path / "gold", fetcher=lambda: data.FetchResult(series=gold)))
    monkeypatch.setattr(main, "news_service", NewsService(fetcher=lambda: ([], ["x"])))
    state = {"fail": False}

    def gld_fetch():
        if state["fail"]:
            raise alpaca.AlpacaError("Alpaca rejected the API keys (HTTP 401). Check ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env.")
        return (gold * 0.092 * np.exp(np.random.default_rng(1).normal(0, 0.002, len(gold)))).rename("gld")

    monkeypatch.setattr(main, "gld_store", alpaca.GldStore(tmp_path / "gld", fetcher=gld_fetch))
    main._forecast_cache.clear()
    c = TestClient(main.app)
    c.state = state
    return c


def test_endpoint_not_configured_is_guidance_not_error(api, no_keys):
    r = api.get("/api/compare/gld")
    assert r.status_code == 200 and r.json()["configured"] is False and len(r.json()["setup"]) == 3


def test_endpoint_configured_and_never_leaks_keys(api, keys):
    r = api.get("/api/compare/gld")
    assert r.status_code == 200
    j = r.json()
    assert j["configured"] and j["instrument"]["kind"] == "etf" and "not used by the forecast" in j["note"]
    assert j["source"] == "Alpaca market data (iex feed)" and j["status"]["mode"] == "live"
    assert j["comparison"]["indexed"][0] == {"date": j["comparison"]["start"], "gc": 100.0, "gld": 100.0}
    assert 0.9 < j["comparison"]["window"]["return_correlation"] <= 1
    for path in ("/api/compare/gld", "/api/prices", "/api/predict?horizon=5", "/api/news"):
        body = api.get(path).text
        assert KEY not in body and SECRET not in body


def test_endpoint_failure_with_and_without_cache(api, keys):
    api.state["fail"] = True
    r = api.get("/api/compare/gld")
    assert r.status_code == 502 and r.json()["detail"]["code"] == "alpaca_error" and SECRET not in r.text
    api.state["fail"] = False
    assert api.get("/api/compare/gld").status_code == 200  # populates the cache
    api.state["fail"] = True
    j = api.get("/api/compare/gld?refresh=true").json()
    assert j["status"]["mode"] == "cached" and "401" in j["provider_error"] and KEY not in json.dumps(j)


def test_forecast_does_not_depend_on_gld(api, keys):
    before = api.get("/api/predict?horizon=5").json()["forecast"]["price"]
    api.get("/api/compare/gld")
    main._forecast_cache.clear()
    assert api.get("/api/predict?horizon=5").json()["forecast"]["price"] == pytest.approx(before)
