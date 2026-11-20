"""Operations: durable job state and back-off, the recorded daily sequence, quality gating, runtime reporting, ledger/feature-group integrity."""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import config, db, ledger, learning, quality, registry, worker
from conftest import make_series
from test_learning import _driver_frame, _issue


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db._inited.discard(str(db.path()))
    registry.seed()
    yield


# ------------------------------------------------------------------------------------------------------------- jobs: durable state, back-off, limits
def test_a_failing_job_backs_off_instead_of_hammering_and_a_success_resets_it():
    calls = []

    def bad():
        calls.append(1)
        raise ValueError("provider down")
    assert worker.run_job("collect", bad) is None and len(calls) == 1
    st = worker._state("collect")
    assert st["consecutive_failures"] == 1 and st["next_retry_at"] > time.time() + 20 and "provider down" in st["last_error"]
    assert worker.run_job("collect", bad) is None and len(calls) == 1          # inside the back-off window: not even attempted
    worker.run_job("collect", bad, force=True)
    assert len(calls) == 2 and worker._state("collect")["consecutive_failures"] == 2
    assert worker._state("collect")["next_retry_at"] > time.time() + 100         # the wait grew
    assert worker.run_job("collect", lambda: {"ok": 1}, force=True) == {"ok": 1}
    st = worker._state("collect")
    assert st["consecutive_failures"] == 0 and st["next_retry_at"] is None and st["last_success_at"]


def test_the_retry_limit_is_reached_after_repeated_failures_and_is_bounded_not_permanent():
    def bad():
        raise ValueError("x")
    for _ in range(worker.MAX_FAILURES):
        worker.run_job("spot", bad, force=True)
    st = worker._state("spot")
    assert st["gave_up"] == 1 and st["next_retry_at"] - time.time() <= worker.BACKOFF[-1] + 5   # retried hourly at most, never never
    worker.reset_state("spot")
    st = worker._state("spot")
    assert st["gave_up"] == 0 and st["consecutive_failures"] == 0


def test_job_state_is_durable_across_connections():
    worker.run_job("collect", lambda: (_ for _ in ()).throw(ValueError("boom")))
    db._local.__dict__.clear()                                                  # a "restart": new connection, same file
    assert worker._state("collect")["consecutive_failures"] == 1


# ------------------------------------------------------------------------------------------------------------- the recorded daily sequence
def _stub_all(monkeypatch, fail=()):
    order = []
    for name in worker.STEP_ORDER:
        def fn(n=name):
            order.append(n)
            if n in fail:
                raise RuntimeError(f"{n} failed")
            return {"data_through": "2026-11-17"} if n == "collect" else {"ok": True}
        monkeypatch.setattr(worker, f"job_{name}", fn)
    return order


def test_the_cycle_runs_the_sequence_in_order_and_records_every_step(monkeypatch):
    order = _stub_all(monkeypatch)
    out = worker.full_cycle("scheduled")
    assert order == list(worker.STEP_ORDER) and out["status"] == "ok"
    assert order.index("collect") < order.index("validate") < order.index("score") < order.index("learn") < order.index("alerts")
    c = worker.last_cycle()
    assert c["trigger"] == "scheduled" and c["status"] == "ok" and [s["step"] for s in c["steps"]] == list(worker.STEP_ORDER) and c["data_through"] == "2026-11-17"


def test_cycle_history_counts_a_scheduled_cycle_only_when_one_is_recorded(monkeypatch):
    assert "No scheduled cycle has been recorded" in worker.cycle_history()["verdict"]
    _stub_all(monkeypatch)
    worker.full_cycle("manual")
    h = worker.cycle_history()
    assert h["scheduled_ok"] == 0 and h["counts"] == {"manual": 1} and "No scheduled cycle has been recorded" in h["verdict"]   # a hand-run is not the schedule
    worker.full_cycle("catch-up")
    assert worker.cycle_history()["scheduled_ok"] == 0                                                                              # nor is a catch-up
    worker.full_cycle("scheduled")
    h = worker.cycle_history()
    assert h["scheduled_ok"] == 1 and "1 scheduled cycle(s) recorded with status ok" in h["verdict"]
    assert h["recent"][0]["trigger"] == "scheduled" and [s["step"] for s in h["recent"][0]["steps"]] == list(worker.STEP_ORDER)


def test_one_failing_step_is_recorded_and_the_rest_still_run(monkeypatch):
    order = _stub_all(monkeypatch, fail={"collect"})
    out = worker.full_cycle("catch-up")
    assert out["status"] == "partial" and order == list(worker.STEP_ORDER)       # alerts and paper still ran
    steps = {s["step"]: s["status"] for s in worker.last_cycle()["steps"]}
    assert steps["collect"] == "failed" and steps["learn"] == "ok"


def test_a_scheduled_cycle_starts_every_job_fresh_even_after_failures(monkeypatch):
    worker.run_job("collect", lambda: (_ for _ in ()).throw(ValueError("x")))
    assert worker._state("collect")["next_retry_at"]
    order = _stub_all(monkeypatch)
    worker.full_cycle("scheduled")
    assert "collect" in order and worker._state("collect")["consecutive_failures"] == 0


# ------------------------------------------------------------------------------------------------------------- quality gates learning
def test_learning_is_blocked_by_a_blocking_data_finding_and_keeps_models_untouched(monkeypatch):
    called = []
    monkeypatch.setattr(learning, "run_learning", lambda *a, **k: called.append(1) or {})
    quality.record(quality.Finding("daily", "stale_feed", "daily", "behind by 5 sessions", None))
    out = worker.job_learn()
    assert "skipped" in out and "stale_feed" in out["skipped"] and called == []
    quality.resolve("daily", "stale_feed")
    class Store:
        def get(self, **k):
            class D: series = make_series(700, start="2022-01-03"); key = "k"
            return D
    monkeypatch.setattr(worker, "_store", Store())
    monkeypatch.setattr(worker.drivers, "load", lambda *a, **k: _driver_frame(make_series(700, start="2022-01-03").index))
    worker.job_learn()
    assert called == [1]


# ------------------------------------------------------------------------------------------------------------- runtime, schedule, recovery
@pytest.mark.parametrize("runtime,word", [("launchd", "launchd agent"), ("container", "container"), ("web-embedded", "embedded"), ("manual", "manually")])
def test_the_status_says_which_runtime_is_actually_operating(runtime, word):
    assert word in worker.runtime_note({"runtime": runtime}, True)
    assert "No worker is running" in worker.runtime_note(None, False)


def test_heartbeat_carries_the_runtime(monkeypatch):
    monkeypatch.setattr(worker, "RUNTIME", "launchd")
    worker._heartbeat({"state": "running"})
    hb = worker.heartbeat()
    assert hb["runtime"] == "launchd" and hb["pid"] and hb["host"]
    st = worker.status()
    assert st["daemon_running"] and st["runtime"] == "launchd" and "launchd agent" in st["runtime_note"]


def test_next_runs_follow_cadence_and_show_backoff():
    worker.run_job("alerts", lambda: {"ok": 1})
    nr = {r["job"]: r for r in worker.next_runs()}
    assert nr["alerts"]["next_at"] >= time.time() + worker.ALERT_EVERY - 5 and "daily cycle" in nr
    assert nr["nowcast"]["next_at"] % 3600 == 0
    worker.run_job("score", lambda: (_ for _ in ()).throw(ValueError("x")))
    assert {r["job"]: r for r in worker.next_runs()}["score"]["backing_off"] is True


def test_the_launchd_agent_is_detected_so_the_web_app_does_not_start_a_second_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert worker.standalone_installed() is False
    p = tmp_path / "Library" / "LaunchAgents"
    p.mkdir(parents=True)
    (p / "ai-gold-forecast.worker.plist").write_text("x")
    assert worker.standalone_installed() is True


def test_a_second_daemon_cannot_run_while_one_holds_the_lock():
    with worker.file_lock("daemon") as got:
        assert got
        assert worker.run_loop(stop=__import__("threading").Event(), wait_for_lock=False) is False


def test_session_gap_uses_the_exchange_calendar(monkeypatch):
    class Store:
        def get(self, **k):
            class D: series = pd.Series([2000.0, 2001.0], index=pd.to_datetime(["2026-11-12", "2026-11-13"]))
            return D
    monkeypatch.setattr(worker, "_store", Store())
    g = worker.session_gap()
    assert g["data_through"] == "2026-11-13" and g["sessions_behind"] >= 1


# ------------------------------------------------------------------------------------------------------------- ledger and feature-group integrity
def test_reissuing_from_a_different_snapshot_is_ignored_but_never_silently():
    t0 = pd.Timestamp("2025-03-04 13:30", tz="America/New_York").timestamp() - 86400          # a day before the target
    assert _issue(predicted=100.0, data_key="2025-03-03|700", dataset_hash="aaaa", when=t0)
    assert not _issue(predicted=101.5, data_key="2025-03-03|701", dataset_hash="bbbb", when=t0 + 100)
    row = db.rows("SELECT predicted, dataset_hash, issued_at FROM forecasts")[0]
    assert row["predicted"] == 100.0 and row["dataset_hash"] == "aaaa"            # the original stays, with its original timestamp
    aud = db.rows("SELECT kind, detail FROM ledger_audit")
    assert len(aud) == 1 and aud[0]["kind"] == "snapshot_conflict" and "bbbb" in aud[0]["detail"]
    assert not _issue(predicted=101.5, data_key="2025-03-03|701", dataset_hash="bbbb", when=t0 + 200)
    assert len(db.rows("SELECT * FROM ledger_audit")) == 1                        # the same conflict is not logged twice
    assert not _issue(predicted=100.0, data_key="2025-03-03|700", dataset_hash="aaaa", when=t0 + 300)
    assert len(db.rows("SELECT * FROM ledger_audit")) == 1                        # an identical re-issue is not a conflict


def test_the_dataset_hash_changes_when_any_input_changes():
    s = make_series(200)
    d = _driver_frame(s.index)
    h = ledger.dataset_hash(s, d)
    assert h == ledger.dataset_hash(s.copy(), d.copy()) and h != ledger.dataset_hash(s.iloc[:-1], d) and h != ledger.dataset_hash(s * 1.0001, d) and h != ledger.dataset_hash(s)


def _fe(delta, ci, n=590):
    return {"a": "x", "b": "y", "n_origins": n, "delta_pct_of_baseline_mae": delta, "ci90": ci, "a_vs_unchanged_pct": 0, "b_vs_unchanged_pct": 0}


def test_the_fibonacci_rule_supports_only_consistent_reliable_improvement():
    win = _fe(-1.0, [-2.0, -0.2])
    flat = _fe(0.0, [-0.5, 0.5])
    worse = _fe(1.0, [0.2, 1.8])
    sup = learning.fib_verdict({"fib_vs_patterns": {"1": {"ridge": win}, "5": {"ridge": win}, "20": {"ridge": flat}}})
    assert sup["status"] == "supported"
    one = learning.fib_verdict({"fib_vs_patterns": {"1": {"ridge": win}, "5": {"ridge": flat}, "20": {"ridge": flat}}})
    assert one["status"] == "not_supported"                                      # one win is not "consistent": 6 comparisons will produce one by luck
    hurt = learning.fib_verdict({"fib_vs_patterns": {"1": {"ridge": win}, "5": {"ridge": win}, "20": {"hgb": worse}}})
    assert hurt["status"] == "not_supported" and hurt["worse"]                   # reliably worse anywhere vetoes it
    few = learning.fib_verdict({"fib_vs_patterns": {"1": {"ridge": _fe(-5, [-9, -1], n=50)}, "5": {"ridge": _fe(-5, [-9, -1], n=50)}}})
    assert few["status"] == "insufficient"                                       # too few identical origins to decide either way


def test_rejecting_fibonacci_keeps_all_history_and_stops_new_forecasts_only():
    done = registry.reject_schema("fib_v1", "not supported")
    assert len(done) == 6 and all(v.endswith(":fib_v1") for v in done)
    assert not [v for v in registry.versions("fwd_5") if v["feature_schema"] == "fib_v1"]
    assert len([v for v in registry.versions("fwd_5", include_rejected=True) if v["feature_schema"] == "fib_v1"]) == 2
    assert {d["decision"] for d in registry.decisions(30)} >= {"reject"}
    registry.restore_schema("fib_v1")
    assert len([v for v in registry.versions("fwd_5") if v["feature_schema"] == "fib_v1"]) == 2
    assert registry.champion("fwd_5")["feature_schema"] != "fib_v1"


def test_candidates_are_bounded_new_groups_use_ridge_and_one_tree_model_only():
    specs = registry.candidate_specs("fwd_5")
    assert {a for a, s in specs if s in ("patterns_v1", "fib_v1")} == {"ridge", "hgb"} and len(specs) == 10


def test_every_attempt_including_failures_is_logged_and_the_others_still_run(monkeypatch):
    close = make_series(700, start="2022-01-03")
    real = learning.rd.walk_forward

    def flaky(ds, cols, algo, h):
        if algo == "hgb" and "fib20_pos" in cols:
            raise RuntimeError("model blew up")
        return real(ds, cols, algo, h)
    monkeypatch.setattr(learning.rd, "walk_forward", flaky)
    res = learning.dev_evaluate_forward(close, _driver_frame(close.index), "2024-06-14")
    fe = res.pop("_feature_eval")
    assert "fwd_5:ridge:fib_v1" in res and "fwd_5:hgb:fib_v1" not in res and "fwd_5:hgb:patterns_v1" in res
    failed = db.rows("SELECT version, stage, detail FROM training_attempts WHERE status='failed'")
    assert {f["version"] for f in failed} == {"fwd_1:hgb:fib_v1", "fwd_5:hgb:fib_v1", "fwd_20:hgb:fib_v1"} and "model blew up" in failed[0]["detail"]
    assert db.rows("SELECT COUNT(*) AS n FROM training_attempts WHERE status='ok'")[0]["n"] > 20
    # identical origins and outcomes in every paired comparison: that is what makes "with vs without Fibonacci" fair
    for h, per in fe["patterns_vs_gold"].items():
        for algo, r in per.items():
            assert r["n_origins"] == res[f"fwd_{h}:{algo}:gold_v1"]["n_origins"] == res[f"fwd_{h}:ridge:gold_v1"]["n_origins"]
    assert any(k in res and "series" in res[k] for k in res if k.endswith(":gold_v1"))   # the champion's walk-forward predicted-vs-actual is kept


def test_publishing_happens_even_when_evaluation_fails_so_a_new_session_always_gets_forecasts(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(learning, "dev_evaluate_forward", boom)
    monkeypatch.setattr(learning, "dev_evaluate_nowcast", boom)
    close = make_series(700, start="2022-01-03")
    # ends on the previous business day, a session that has always closed (a bar dated today would be "from the future" before
    # today's close and the ledger would refuse it); the targets still lie in the (real) future, so forecasts are loggable
    close.index = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.offsets.BDay(1), periods=700)
    before = {t: registry.champion(t)["version"] for t in registry.TASKS}
    summary = learning.run_learning(close, _driver_frame(close.index), "k")
    assert set(summary["errors"]) == {"forward", "nowcast"} and summary["forward_issue"]["issued"] > 0
    assert {t: registry.champion(t)["version"] for t in registry.TASKS} == before
    assert db.rows("SELECT dataset_hash FROM forecasts WHERE task='fwd_5' LIMIT 1")[0]["dataset_hash"] == summary["forward_issue"]["dataset_hash"]
    assert db.rows("SELECT COUNT(*) AS n FROM training_attempts WHERE status='failed' AND stage='dev_eval'")[0]["n"] >= 2


def test_the_gold_only_champion_still_forecasts_when_the_driver_series_are_missing():
    # a day the driver downloads failed (None) or one series is missing (FRED down): the seeded gold-only champions still issue a call,
    # the driver candidates sit the session out, and nothing is invented for them
    close = make_series(700, start="2022-01-03")
    close.index = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.offsets.BDay(1), periods=700)
    out = learning.issue_forward(close, None, "k")
    assert out["issued"] > 0 and set(out["gold_only"]) == {"fwd_1", "fwd_5", "fwd_20"}
    assert any(s.endswith(":drivers_v1: driver data unavailable") for s in out["skipped"])
    for h in (1, 5, 20):
        row = ledger.latest(f"fwd_{h}", "champion", horizon=h)
        assert row and row["model_version"] == registry.champion(f"fwd_{h}")["version"] and row["base_time"] == close.index[-1].date().isoformat()
    assert not db.rows("SELECT 1 FROM forecasts WHERE feature_schema='drivers_v1'")

    frame = _driver_frame(close.index)
    no_fred = frame[frame["series"] != "real10"]
    assert len(no_fred) < len(frame)
    out2 = learning.issue_forward(close, no_fred, "k2")
    assert out2.get("gold_only") == ["fwd_1", "fwd_5", "fwd_20"]
