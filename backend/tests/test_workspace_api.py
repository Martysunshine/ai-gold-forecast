"""The Learning and Paper-trading endpoints, and the guard that keeps state-changing calls local."""
import time

import pytest
from fastapi.testclient import TestClient

from app import alerts, config, db, ledger, main, registry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "API_TOKEN", "")
    db._inited.discard(str(db.path()))
    registry.seed()


@pytest.fixture
def client():
    return TestClient(main.app)


# ------------------------------------------------------------------------------------------------------------- the guard
def test_state_changing_calls_need_a_local_client_and_this_apps_own_origin():
    local = TestClient(main.app)
    assert local.post("/api/paper/book", json={}).status_code == 200                                   # loopback, no Origin header
    assert local.post("/api/paper/book", json={}, headers={"Origin": "http://localhost:5183"}).status_code == 200   # a configured dev origin
    r = local.post("/api/paper/book", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"                          # another site in the same browser
    assert local.put("/api/alerts/settings", json={"levels": [4000]}, headers={"Origin": "https://evil.example"}).status_code == 403
    remote = TestClient(main.app, client=("203.0.113.9", 50000))
    assert remote.post("/api/paper/book", json={}).status_code == 403 and remote.get("/api/paper").status_code == 200   # reading stays open, changing does not


def test_with_a_token_configured_only_the_token_opens_state_changes(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "s3cret")
    c = TestClient(main.app)
    assert c.post("/api/paper/book", json={}).status_code == 403
    assert c.post("/api/paper/book", json={}, headers={"X-Gold-Token": "wrong"}).status_code == 403
    assert c.post("/api/paper/book", json={}, headers={"X-Gold-Token": "s3cret"}).status_code == 200


# ------------------------------------------------------------------------------------------------------------- paper API
def test_paper_endpoints_create_pause_resume_params_and_keep_history(client):
    assert client.get("/api/paper").json()["book"] is None and client.get("/api/paper").json()["simulation_only"] is True
    b1 = client.post("/api/paper/book", json={"name": "A", "initial_cash": 5000}).json()
    assert b1["status"] == "active" and b1["initial_cash"] == 5000
    assert client.post(f"/api/paper/book/{b1['id']}/pause", json={"reason": "testing"}).json()["status"] == "paused"
    assert client.post(f"/api/paper/book/{b1['id']}/resume").json()["status"] == "active"
    assert client.put(f"/api/paper/book/{b1['id']}/params", json={"stop_loss_pct": 2.5}).json()["params"]["stop_loss_pct"] == 2.5
    assert client.put(f"/api/paper/book/{b1['id']}/params", json={"stop_loss_pct": 0}).status_code == 422
    b2 = client.post("/api/paper/book", json={"name": "B"}).json()
    j = client.get("/api/paper").json()
    assert j["book"]["id"] == b2["id"] and {x["status"] for x in j["books"]} == {"archived", "active"}
    assert client.post(f"/api/paper/book/{b1['id']}/pause", json={}).status_code == 422             # an archived book is frozen
    assert client.post("/api/paper/book", json={"initial_cash": 1}).status_code == 422


def test_paper_has_no_route_that_places_a_live_order():
    paths = {r.path for r in main.app.routes if "paper" in getattr(r, "path", "")}
    assert paths and not any("order" in p or "broker" in p or "live" in p for p in paths)


# ------------------------------------------------------------------------------------------------------------- Learning workspace
def _fc(task, version, base, pred, target, issued, role="champion", h=5):
    t = f"{target}T13:30:00-05:00"
    ledger.issue(task=task, role=role, version=version, algorithm=version.split(":")[1], feature_schema=version.split(":")[2], input_cutoff=issued - 10, base_price=base,
                 base_time=target[:7] + "-01", target_date=target, target_time=t, predicted=pred, low=pred - 20, high=pred + 20, level=0.8, horizon=h, issued_at=issued)


def test_workspace_payload_separates_development_from_forward_and_reports_honestly_when_empty(client):
    j = client.get("/api/learning/workspace").json()
    assert set(j["tasks"]) == {"fwd_1", "fwd_5", "fwd_20", "nowcast"}
    t = j["tasks"]["fwd_5"]
    assert t["active"]["version"].startswith("fwd_5:") and t["forward"]["n_matured"] == 0 and t["forward"]["distinct_origins"] == 0
    assert "Genuine forward" in t["forward"]["label"] and "development evidence" in t["development"]["label"].lower()
    assert t["development"]["trend"]["verdict"] == "first_run" and t["forward"]["blocks_needed"] == 30
    assert j["tasks"]["nowcast"]["active"]["is_unchanged_baseline"] is True and "unchanged price is the production" in j["tasks"]["nowcast"]["production_note"]
    assert j["worker"]["daemon_running"] in (True, False) and "runtime_note" in j["worker"] and j["worker"]["next_runs"]
    assert j["feature_groups"]["evaluated"] is False and j["quality"]["by_status"] == {} and j["attempts"]["failed"] == 0


def test_workspace_counts_distinct_matured_origins_not_forecasts(client):
    v = registry.champion("fwd_5")["version"]
    import pandas as pd
    now = time.time()
    for i, d in enumerate(["2026-03-03", "2026-03-04"]):
        when = pd.Timestamp(f"2026-03-1{i}").timestamp() - 7 * 86400                       # issued a week before its target
        for k in range(3):   # three versions-worth of rows for the same origin must not count as three origins
            ledger.issue(task="fwd_5", role=["champion", "shadow", "baseline"][k], version=[v, "fwd_5:ridge:patterns_v1", "fwd_5:unchanged:price_now_v1"][k], algorithm="ridge",
                         feature_schema="gold_v1", input_cutoff=when - 10, base_price=4000, base_time=d, target_date=f"2026-03-1{i}", target_time=f"2026-03-1{i}T13:30:00-05:00",
                         predicted=4010, low=3900, high=4100, level=0.8, horizon=5, issued_at=when)
    daily = pd.Series([4005.0, 4020.0], index=pd.to_datetime(["2026-03-10", "2026-03-11"]))
    assert ledger.mature(daily, now=now) == 6
    t = TestClient(main.app).get("/api/learning/workspace").json()["tasks"]["fwd_5"]
    assert t["forward"]["distinct_origins"] == 2 and t["forward"]["n_matured"] == 2 and t["forward"]["history"][0]["actual"] in (4005.0, 4020.0)


def test_trend_says_improved_worsened_or_unchanged_using_a_noise_tolerance(client):
    import json
    v = registry.champion("fwd_5")["version"]

    def put(day, mae):
        db.connect().execute("INSERT OR REPLACE INTO dev_evals VALUES (?,?,?,?,?)", (day, "fwd_5", v, time.time(), json.dumps({"mae_vs_baseline_pct": mae, "coverage_pct": 79, "mean_width_pct": 8, "n_origins": 500, "task": "fwd_5"})))
    put("2026-11-14", 1.00); put("2026-11-15", 0.90)
    assert client.get("/api/learning/workspace").json()["tasks"]["fwd_5"]["development"]["trend"]["verdict"] == "unchanged"       # 0.10 points is noise
    put("2026-11-16", 0.20)
    assert client.get("/api/learning/workspace").json()["tasks"]["fwd_5"]["development"]["trend"]["verdict"] == "improved"
    put("2026-11-17", 1.50)
    tr = client.get("/api/learning/workspace").json()["tasks"]["fwd_5"]["development"]["trend"]
    assert tr["verdict"] == "worsened" and "not proof" not in tr["text"] and len(tr["points"]) == 4


def test_quality_endpoint_reports_and_runs(client, monkeypatch):
    import app.data as data
    monkeypatch.setattr(data, "DataStore", type("S", (), {"get": lambda self: (_ for _ in ()).throw(RuntimeError("none"))}))
    r = client.post("/api/quality/run").json()
    assert r["ok_to_train"] is True
    assert "by_status" in client.get("/api/quality").json()


def test_the_alerts_panel_reports_the_closed_position(client):
    p = client.get("/api/alerts").json()
    assert p["position"]["status"] == "closed_unconfirmed" and p["position"]["exit_price"] is None
    assert alerts.get_settings()["entry_price"] == 4039.96                           # kept as history, as entered
