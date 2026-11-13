import pytest
from fastapi.testclient import TestClient

from app import data, main
from app.news import NewsService, build_digest, classify
from conftest import make_series

import datetime as dt


@pytest.fixture
def client(tmp_path, monkeypatch):
    s = make_series(800, start="2023-01-02")
    state = {"fail": False}

    def fetcher():
        if state["fail"]:
            raise data.DataUnavailable("simulated outage")
        return data.FetchResult(series=s)

    monkeypatch.setattr(main, "store", data.DataStore(tmp_path, fetcher=fetcher))
    monkeypatch.setattr(main, "news_service", NewsService(fetcher=lambda: ([], ["simulated"])))
    main._forecast_cache.clear()
    c = TestClient(main.app)
    c.state = state
    c.series = s
    return c


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_prices_payload_contract(client):
    r = client.get("/api/prices")
    assert r.status_code == 200
    j = r.json()
    assert j["source"] and j["instrument"]["kind"] == "futures" and "not spot" in j["instrument"]["note"]
    assert j["latest"]["date"] == j["prices"][-1]["date"] and j["count"] == len(j["prices"])
    assert j["freshness"]["status"] in {"fresh", "stale"}
    assert j["freshness"]["latest_date"] == j["latest"]["date"]


def test_status_and_session_fields(client):
    j = client.get("/api/prices").json()
    assert j["status"]["mode"] == "live" and j["session"]["latest_is_completed_session"] is True
    client.state["fail"] = True
    j = client.post("/api/prices/refresh").json()
    assert j["status"]["mode"] == "cached"


def test_stale_data_is_flagged(client):
    j = main.price_payload(main.store.get(), today=dt.date(2030, 1, 1))
    assert j["freshness"]["status"] == "stale" and "days old" in j["freshness"]["message"]


def test_predict_endpoint(client):
    j = client.get("/api/predict?horizon=5").json()
    assert j["horizon"] == 5 and j["forecast"]["model"] in {"ridge", "random_forest"}
    assert j["test"]["verdict"] and len(j["test_series"]) == j["test"]["n"]
    assert j["data"]["source_kind"] == "provider"


def test_predict_rejects_bad_horizon(client):
    r = client.get("/api/predict?horizon=3")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_horizon"


def test_predict_download_csv(client):
    r = client.get("/api/predict/download?horizon=20")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert len(lines) == 2 and lines[0].startswith("horizon_sessions") and lines[1].startswith("20,")


def test_no_data_returns_503_not_synthetic(tmp_path, monkeypatch):
    def boom():
        raise data.DataUnavailable("simulated outage")

    monkeypatch.setattr(main, "store", data.DataStore(tmp_path, fetcher=boom))
    c = TestClient(main.app)
    for path in ("/api/prices", "/api/predict?horizon=5", "/api/predict/download?horizon=5"):
        r = c.get(path)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "no_data"


def test_refresh_failure_keeps_cached_data_and_reports(client):
    client.get("/api/prices")
    client.state["fail"] = True
    j = client.post("/api/prices/refresh").json()
    assert j["provider_error"] and j["count"] == 800


def test_upload_valid_and_invalid(client):
    s = make_series(600, seed=3, start="2023-01-02")
    csv = "Date,Close\n" + "\n".join(f"{d:%Y-%m-%d},{v:.2f}" for d, v in s.items())
    r = client.post("/api/prices/upload", files={"file": ("gold.csv", csv, "text/csv")})
    assert r.status_code == 200 and r.json()["source_kind"] == "upload" and r.json()["count"] == 600
    assert client.get("/api/predict?horizon=1").json()["data"]["source_kind"] == "upload"

    bad = client.post("/api/prices/upload", files={"file": ("bad.csv", "Date,Close\n2024-01-01,-5\n", "text/csv")})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_csv"


def test_news_failure_is_honest(client):
    j = client.get("/api/news").json()
    assert j["status"] == "error" and j["items"] == [] and "could not be retrieved" in j["message"]


def test_news_digest_filters_and_themes():
    now = dt.datetime(2026, 11, 13, tzinfo=dt.timezone.utc)

    def item(title, days=1):
        return {"title": title, "source": "X", "link": "https://example.com/" + title[:5], "published": now - dt.timedelta(days=days)}

    items = [
        item("Gold rises as Fed rate cut bets lift demand and weaken the dollar"),
        item("Gold rises as Fed rate cut bets lift demand and weaken the dollar"),  # duplicate
        item("Central banks keep buying gold as geopolitical tensions persist"),
        item("Gold Coast beach festival opens"),
        item("Barrick (NYSE:GOLD) Q3 results"),
        item("Gold slips on inflation data", days=60),  # too old
    ]
    d = build_digest(items, now=now)
    assert d["n_items"] == 2
    themes = {t["id"]: t["count"] for t in d["themes"]}
    assert themes["rates"] == 1 and themes["dollar"] == 1 and themes["central_banks"] == 1 and themes["geopolitics"] == 1
    assert classify("no keywords here") == []


# ------------------------------------------------------------------ integration behaviours
def _csv(series):
    return "Date,Close\n" + "\n".join(f"{d:%Y-%m-%d},{v:.2f}" for d, v in series.items())


def test_all_horizons_are_internally_consistent(client):
    base = client.get("/api/prices").json()["latest"]["close"]
    for h in (1, 5, 20):
        j = client.get(f"/api/predict?horizon={h}").json()
        f = j["forecast"]
        i = f["interval"]
        assert j["horizon"] == h and j["base_price"] == pytest.approx(base, abs=0.01)
        assert f["price"] == pytest.approx(j["base_price"] * (1 + f["change_pct"] / 100))
        assert i["low"] < i["high"] and i["low_pct"] == pytest.approx((i["low"] / j["base_price"] - 1) * 100)
        assert i["high_pct"] == pytest.approx((i["high"] / j["base_price"] - 1) * 100)
        assert j["target_date"] > j["base_date"] and j["data_key"]
        assert i["calibrated_on"]["end"] < i["evaluated_on"]["start"]


def test_results_are_tied_to_their_data(client):
    key = client.get("/api/prices").json()["data_key"]
    assert client.get("/api/predict?horizon=5").json()["data_key"] == key
    other = make_series(600, seed=8, start="2023-01-02")
    up = client.post("/api/prices/upload", files={"file": ("a.csv", _csv(other), "text/csv")}).json()
    assert up["data_key"] != key
    assert client.get("/api/predict?horizon=5").json()["data_key"] == up["data_key"]


def test_repeat_requests_are_served_from_cache_without_retraining(client, monkeypatch):
    calls = {"n": 0}
    real = main.run_prediction

    def counting(series, h):
        calls["n"] += 1
        return real(series, h)

    monkeypatch.setattr(main, "run_prediction", counting)
    a = client.get("/api/predict?horizon=5").json()
    b = client.get("/api/predict?horizon=5").json()
    client.get("/api/prices")  # cosmetic/page reads do not retrain
    client.get("/api/news")
    assert calls["n"] == 1 and a["served_from_cache"] is False and b["served_from_cache"] is True
    assert a["forecast"]["price"] == b["forecast"]["price"]


def test_export_matches_displayed_forecast_exactly(client):
    import csv, io

    j = client.get("/api/predict?horizon=20").json()
    r = client.get(f"/api/predict/download?horizon=20&key={j['data_key']}")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    row, f = rows[0], j["forecast"]
    assert row["horizon_sessions"] == "20" and row["target_session_date"] == j["target_date"] and row["forecast_origin_date"] == j["base_date"]
    # same 2-decimal precision the interface shows
    assert row["forecast_price_usd"] == f"{f['price']:.2f}" and row["projected_change_pct"] == f"{f['change_pct']:.2f}"
    assert row["target_range_low_usd"] == f"{f['interval']['low']:.2f}" and row["target_range_high_usd"] == f"{f['interval']['high']:.2f}"
    assert row["origin_close_usd"] == f"{j['base_price']:.2f}" and row["data_key"] == j["data_key"]
    assert row["model"] == f["model_label"] and row["test_n"] == str(j["test"]["n"])


def test_export_refuses_stale_data(client):
    key = client.get("/api/prices").json()["data_key"]
    client.post("/api/prices/upload", files={"file": ("a.csv", _csv(make_series(600, seed=4, start="2023-01-02")), "text/csv")})
    r = client.get(f"/api/predict/download?horizon=5&key={key}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_data"


def test_failed_refresh_retains_last_valid_data_and_exposes_age(client):
    before = client.get("/api/prices").json()
    client.state["fail"] = True
    after = client.post("/api/prices/refresh").json()
    assert after["data_key"] == before["data_key"] and after["count"] == before["count"]
    assert after["provider_error"] and after["status"]["mode"] == "cached"
    assert after["fetched_at"] == before["fetched_at"] and after["age_minutes"] >= 0


def test_invalid_upload_leaves_active_dataset_unchanged(client):
    good = client.post("/api/prices/upload", files={"file": ("g.csv", _csv(make_series(600, seed=3, start="2023-01-02")), "text/csv")}).json()
    for payload in ("Date,Close\n2024-01-01,-5\n", "garbage", "Date,Close\n"):
        r = client.post("/api/prices/upload", files={"file": ("bad.csv", payload, "text/csv")})
        assert r.status_code == 422
    now = client.get("/api/prices").json()
    assert now["data_key"] == good["data_key"] and now["source_kind"] == "upload" and now["count"] == 600


def test_can_return_from_upload_to_provider_data(client):
    provider = client.get("/api/prices").json()
    client.post("/api/prices/upload", files={"file": ("g.csv", _csv(make_series(600, seed=3, start="2023-01-02")), "text/csv")})
    back = client.post("/api/prices/use-provider").json()
    assert back["source_kind"] == "provider" and back["data_key"] == provider["data_key"]
    assert client.get("/api/prices").json()["source_kind"] == "provider"


def test_use_provider_when_provider_is_down(client):
    client.get("/api/prices")  # saves a provider copy
    client.post("/api/prices/upload", files={"file": ("g.csv", _csv(make_series(600, seed=3, start="2023-01-02")), "text/csv")})
    client.state["fail"] = True
    j = client.post("/api/prices/use-provider").json()  # falls back to the saved provider copy, flagged
    assert j["source_kind"] == "provider" and j["provider_error"] and j["status"]["mode"] == "cached"


def test_refresh_with_upload_active_keeps_upload_when_provider_down(client):
    up = client.post("/api/prices/upload", files={"file": ("g.csv", _csv(make_series(600, seed=3, start="2023-01-02")), "text/csv")}).json()
    client.state["fail"] = True
    j = client.post("/api/prices/refresh").json()
    assert j["source_kind"] == "upload" and j["data_key"] == up["data_key"] and j["provider_error"]


def test_use_provider_without_any_provider_data_keeps_upload(tmp_path, monkeypatch):
    def boom():
        raise data.DataUnavailable("down")

    monkeypatch.setattr(main, "store", data.DataStore(tmp_path, fetcher=boom))
    main._forecast_cache.clear()
    c = TestClient(main.app)
    up = c.post("/api/prices/upload", files={"file": ("g.csv", _csv(make_series(600, seed=3, start="2023-01-02")), "text/csv")}).json()
    r = c.post("/api/prices/use-provider")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "no_provider_data"
    assert c.get("/api/prices").json()["data_key"] == up["data_key"]


def test_news_outage_does_not_affect_forecasting(client):
    assert client.get("/api/news").json()["status"] == "error"
    assert client.get("/api/predict?horizon=1").status_code == 200


def test_market_stats(client):
    j = client.get("/api/prices").json()
    assert 0 < j["stats"]["realised_vol_20d_annualised_pct"] < 200
