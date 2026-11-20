"""The JSON export other programs read (``python -m app.export``): champion rows from the ledger, one call per session, read-only."""
import json
import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from app import config, db, export, ledger, learning, registry, worker
from app.data import DataStore
from conftest import block_network, make_series

META = {"source_kind": "provider", "source": "test data", "symbol": "GC=F", "instrument": {"name": "Gold futures"},
        "fetched_at": "2026-11-16T05:59:00+00:00", "fetched_at_epoch": 0}

#: The fields the trading program reads from every forecast (trader.practice.forecast.parse_call and record_calls).
TRADER_FIELDS = {"horizon_sessions", "direction", "change_pct", "base_date", "target_date", "base_price", "model", "model_label",
                 "valid_from", "valid_until", "evaluation"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db._inited.discard(str(db.path()))
    yield


@pytest.fixture
def close():
    s = make_series(700, start="2022-01-03")
    s.index = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.offsets.BDay(1), periods=700)   # a session that has closed
    return s


def _seed_ledger(close):
    """What the worker leaves behind: saved prices and the champion, shadow and baseline forecasts for the latest session."""
    registry.seed()
    DataStore(config.DATA_DIR)._save("provider", close, META)
    out = learning.issue_forward(close, None, "k")
    assert out["issued"] > 0
    return out


def _export(tmp_path, capsys=None):
    out = tmp_path / "out" / "gold_forecast.json"
    assert export.main(["--out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_the_export_holds_the_champion_forecast_of_every_horizon(close, tmp_path):
    _seed_ledger(close)
    doc = _export(tmp_path)
    assert doc["schema"] == export.SCHEMA == "ai-gold-forecast.forecast/1"
    assert doc["data"]["latest_close_date"] == close.index[-1].date().isoformat() and doc["data"]["latest_close"] == pytest.approx(close.iloc[-1], abs=1e-3)
    assert [f["horizon_sessions"] for f in doc["forecasts"]] == [1, 5, 20]
    for f in doc["forecasts"]:
        task = f"fwd_{f['horizon_sessions']}"
        row = ledger.latest(task, "champion", horizon=f["horizon_sessions"])
        assert TRADER_FIELDS <= set(f)
        assert f["forecast_id"] == row["id"] and f["model"] == row["model_version"] == registry.champion(task)["version"]
        assert f["price"] == pytest.approx(row["predicted"], abs=1e-3) and f["base_date"] == close.index[-1].date().isoformat()
        assert (f["change_pct"] > 0) == (f["direction"] == "up") and (f["change_pct"] < 0) == (f["direction"] == "down")
        assert f["range"]["low"] <= f["price"] <= f["range"]["high"]
        assert f["valid_until"] == export.valid_until(f["target_date"]) and f["valid_from"] == doc["generated_at"]
        e = f["evaluation"]
        assert e["advantage"] is False and "No demonstrated advantage" in e["verdict"] and e["forward"] == {"n_matured": 0}
    assert json.loads(json.dumps(doc)) == doc   # plain JSON types only


def test_the_call_is_issued_once_and_every_export_returns_the_same_numbers(close, tmp_path):
    _seed_ledger(close)
    first = _export(tmp_path)
    time.sleep(1.1)
    second = _export(tmp_path)
    strip = lambda d: [{k: v for k, v in f.items() if k != "valid_from"} for f in d["forecasts"]]  # noqa: E731
    assert strip(first) == strip(second) and first["generated_at"] != second["generated_at"]


def test_the_advantage_flag_is_earned_by_a_promotion_and_lost_by_a_rollback(close, tmp_path):
    _seed_ledger(close)
    challenger = "fwd_1:hgb:gold_v1"
    registry.promote("fwd_1", challenger, "beat the unchanged price on both tests")
    learning.issue_forward(close, None, "k")                 # the new champion's call is issued under the champion role
    one = _export(tmp_path)["forecasts"][0]
    assert one["model"] == challenger and one["evaluation"]["advantage"] is True and one["evaluation"]["verdict"].startswith("Promoted")
    registry.rollback("fwd_1")
    learning.issue_forward(close, None, "k")
    one = _export(tmp_path)["forecasts"][0]
    assert one["model"] == "fwd_1:ridge:gold_v1" and one["evaluation"]["advantage"] is False


def test_the_learning_block_says_whether_the_model_learned_and_when(close, tmp_path):
    _seed_ledger(close)
    conn = db.connect()
    now = time.time()
    conn.execute("INSERT INTO job_runs (job,started_at,finished_at,status,detail) VALUES ('learn',?,?,'ok',?)", (now - 7200, now - 7100, '{"data_through": "x"}'))
    conn.execute("INSERT INTO job_runs (job,started_at,finished_at,status,detail) VALUES ('learn',?,?,'ok',?)",
                 (now - 60, now - 50, '{"skipped": "blocked by a data-quality finding: stale_feed"}'))
    conn.execute("INSERT INTO cycle_runs (started_at,finished_at,trigger,runtime,data_through,steps,status) VALUES (?,?,?,?,?,?,?)",
                 (now - 7300, now - 7000, "scheduled", "windows", "2026-11-19", "[]", "ok"))
    (tmp_path / "worker_heartbeat.json").write_text(json.dumps({"state": "running", "runtime": "windows", "at": now}))
    lb = _export(tmp_path)["learning"]
    assert lb["worker"] == {"running": True, "runtime": "windows", "heartbeat_at": export._iso(now)}
    assert lb["last_learned_at"] == export._iso(now - 7100)
    assert lb["last_learn"]["result"] == "blocked" and "stale_feed" in lb["last_learn"]["note"]
    assert lb["last_scheduled_cycle"]["status"] == "ok" and lb["last_scheduled_cycle"]["data_through"] == "2026-11-19"
    assert lb["champions"]["fwd_1"] == {"version": "fwd_1:ridge:gold_v1", "since": lb["champions"]["fwd_1"]["since"], "promoted": False}
    assert lb["promotions"] == 0 and lb["matured_forecasts"] == 0
    assert lb["next_cycle_at"].endswith("T23:30:00Z")


def test_without_a_heartbeat_no_worker_is_reported_running(close, tmp_path):
    _seed_ledger(close)
    lb = _export(tmp_path)["learning"]
    assert lb["worker"]["running"] is False and lb["last_learned_at"] is None and lb["last_learn"] is None


def test_the_export_is_read_only_and_never_uses_the_network(close, tmp_path, monkeypatch):
    _seed_ledger(close)
    counts = lambda: {t: db.rows(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("forecasts", "models", "decisions", "job_runs", "cycle_runs")}  # noqa: E731
    before = counts()
    block_network(monkeypatch, "the export must not use the network")
    _export(tmp_path)
    assert counts() == before


def test_a_failed_run_leaves_the_last_file_alone_and_creates_no_ledger(close, tmp_path, capsys):
    out = tmp_path / "gold.json"
    out.write_text('{"kept": true}', encoding="utf-8")
    assert export.main(["--out", str(out)]) == 1                             # nothing saved yet
    assert "has not collected data yet" in capsys.readouterr().err
    DataStore(config.DATA_DIR)._save("provider", close, META)
    assert export.main(["--out", str(out)]) == 1                             # prices, but the worker never ran
    assert "has not run yet" in capsys.readouterr().err and not (tmp_path / "gold.db").exists()
    registry.seed()                                                          # a ledger without any forecast
    assert export.main(["--out", str(out)]) == 1
    assert "no champion forecast" in capsys.readouterr().err
    assert json.loads(out.read_text(encoding="utf-8")) == {"kept": True}


def test_a_forecast_applies_until_its_target_day_ends_where_the_bars_are_dated():
    assert export.valid_until("2026-11-16") == "2026-11-17T05:00:00Z"  # New York winter time
    assert export.valid_until("2026-07-01") == "2026-07-02T04:00:00Z"  # and summer time
    assert export.valid_until("2026-10-30") == "2026-10-31T04:00:00Z"  # the Friday before the clocks go back
    assert export.valid_until("2026-11-01") == "2026-11-02T05:00:00Z"  # the day they do: local midnight, not +24 h


def test_direction_words_and_the_next_cycle():
    assert [export.direction_of(v) for v in (0.2, -0.2, 0.0)] == ["up", "down", "flat"]
    assert export.next_cycle(datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 20, 23, 30, tzinfo=timezone.utc)
    assert export.next_cycle(datetime(2026, 11, 20, 23, 45, tzinfo=timezone.utc)) == datetime(2026, 11, 21, 23, 30, tzinfo=timezone.utc)


def test_the_constants_the_export_repeats_match_their_sources():
    assert export.CYCLE_UTC == worker.CYCLE_UTC and export.FALLBACK_KIND == ledger.FALLBACK_KIND
