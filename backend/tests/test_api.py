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
