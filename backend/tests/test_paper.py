"""Paper trading is a deterministic simulation: rule-driven entries tied to stored forecasts, hard limits, no way to reach a broker."""
import ast
import time
from pathlib import Path

import numpy as np
import pytest

from app import config, db, ledger, paper
from conftest import block_network

NOW = 1_800_000_000.0   # fixed "now" for deterministic tests


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db._inited.discard(str(db.path()))
    block_network(monkeypatch, "paper trading must never touch the network")


def issue(bps=50.0, base=4000.0, target_in_h=24 * 7, task="fwd_5", role="champion", version="fwd_5:ridge:gold_v1", now=NOW, base_time="2027-01-15"):
    target = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + target_in_h * 3600))
    assert ledger.issue(task=task, role=role, version=version, algorithm="ridge", feature_schema="gold_v1", input_cutoff=now - 3600, base_price=base,
                        base_time=base_time, target_date=target[:10], target_time=target, predicted=base * (1 + bps / 1e4), low=base * 0.95, high=base * 1.05,
                        level=0.8, horizon=5, data_key="k", issued_at=now - 60)
    return db.rows("SELECT * FROM forecasts ORDER BY issued_at DESC, rowid DESC LIMIT 1")[0]


def q(price, at=NOW):
    return {"price": price, "as_of": at}


def test_paper_module_cannot_reach_a_broker_or_the_network():
    tree = ast.parse(Path(paper.__file__).read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods |= {(n.module or "").split(".")[0]} | ({a.name for a in n.names} if n.level else set())
    banned = {"httpx", "requests", "socket", "urllib", "http", "aiohttp", "websockets", "alpaca", "quote", "alerts", "n8n_link", "alpaca_trade_api"}
    assert not (mods & banned), mods & banned


def test_entry_requires_a_stored_forecast_that_beats_costs_and_cites_it():
    b = paper.new_book(now=NOW)
    f = issue(bps=50)
    out = paper.evaluate(b["id"], q(4000.0), NOW)
    fl = paper.fills(b["id"])
    assert len(fl) == 1 and fl[0]["side"] == "buy" and fl[0]["forecast_id"] == f["id"] and "bps above its reference" in fl[0]["rationale"]
    assert out["actions"] and 1.2 <= fl[0]["qty"] <= 1.25                      # 50% of 10,000 / ~4,001 per oz, rounded down to 0.01 oz
    assert fl[0]["price"] > 4000.0                                           # buys above the quote: the assumed half-spread and slippage


def test_a_forecast_that_does_not_beat_round_trip_costs_is_not_traded_and_says_why_once():
    b = paper.new_book(now=NOW)
    issue(bps=3)
    for t in (NOW, NOW + 120):
        out = paper.evaluate(b["id"], q(4000.0, t), t)
    assert paper.fills(b["id"]) == [] and out["blocked"] == "forecast does not beat costs"
    ev = [e for e in db.rows("SELECT * FROM paper_events") if "no trade" in e["detail"]]
    assert len(ev) == 1 and "round-trip cost" in ev[0]["detail"]               # recorded once, not every pass


def test_a_negative_forecast_never_opens_a_short():
    b = paper.new_book(now=NOW)
    issue(bps=-80)
    paper.evaluate(b["id"], q(4000.0), NOW)
    assert paper.fills(b["id"]) == []


def test_a_decision_is_posted_at_most_once():
    b = paper.new_book(now=NOW)
    issue(bps=50)
    paper.evaluate(b["id"], q(4000.0), NOW)
    paper.evaluate(b["id"], q(4000.0, NOW + 130), NOW + 130)
    assert len(paper.fills(b["id"])) == 1
    assert paper._post(b["id"], "buy", 1.0, 4000.0, b["params"], NOW, None, "dup", "entry", paper.fills(b["id"])[0]["order_key"]) is None


def test_position_exits_when_the_forecast_target_passes():
    b = paper.new_book(now=NOW)
    f = issue(bps=50, target_in_h=2)
    paper.evaluate(b["id"], q(4000.0), NOW)
    later = NOW + 3 * 3600
    out = paper.evaluate(b["id"], q(4010.0, later), later)
    fl = paper.fills(b["id"])
    assert [x["side"] for x in fl] == ["buy", "sell"] and fl[1]["reason"] == "target" and fl[1]["forecast_id"] == f["id"]
    assert paper.cash_and_position(b["id"])[1] == 0 and out["actions"]


def test_stop_loss_exits_deterministically():
    b = paper.new_book(params={"stop_loss_pct": 1.0, "max_position_pct": 20, "max_daily_loss_pct": 10}, now=NOW)
    issue(bps=60)
    paper.evaluate(b["id"], q(4000.0), NOW)
    t = NOW + 600
    paper.evaluate(b["id"], q(3950.0, t), t)                                   # 1.25% below the entry
    assert paper.fills(b["id"])[-1]["reason"] == "stop"


def test_a_risk_limit_breach_exits_and_pauses_the_book_and_a_paused_book_does_not_trade():
    b = paper.new_book(params={"max_position_pct": 100, "stop_loss_pct": 10, "max_daily_loss_pct": 2.0}, now=NOW)
    issue(bps=60)
    paper.evaluate(b["id"], q(4000.0), NOW)
    t = NOW + 600
    out = paper.evaluate(b["id"], q(3900.0, t), t)                             # -2.5% on a fully invested book
    assert paper.fills(b["id"])[-1]["reason"] == "risk" and out["status"] == "paused"
    book = paper.get_book(b["id"])
    assert book["status"] == "paused" and "daily loss" in book["paused_reason"]
    issue(bps=70, now=NOW + 700, base_time="2027-01-16")                       # a new forecast does nothing while paused
    n = len(paper.fills(b["id"]))
    paper.evaluate(b["id"], q(3900.0, t + 100), t + 100)
    assert len(paper.fills(b["id"])) == n
    paper.resume(b["id"])
    assert paper.get_book(b["id"])["status"] == "active"


def test_a_stale_quote_blocks_all_decisions():
    b = paper.new_book(now=NOW)
    issue(bps=60)
    out = paper.evaluate(b["id"], q(4000.0, NOW - 3600), NOW)
    assert "stale" in out["blocked"] and paper.fills(b["id"]) == []


def test_a_forecast_whose_target_has_passed_is_never_used():
    b = paper.new_book(now=NOW)
    issue(bps=60, target_in_h=1)
    out = paper.evaluate(b["id"], q(4000.0, NOW + 7200), NOW + 7200)
    assert paper.fills(b["id"]) == [] and "target time has passed" in out["blocked"]


def test_starting_a_fresh_book_archives_and_keeps_the_old_one_with_its_fills():
    b1 = paper.new_book("first", 5000.0, now=NOW)
    issue(bps=60)
    paper.evaluate(b1["id"], q(4000.0), NOW)
    b2 = paper.new_book("second", 8000.0, now=NOW + 10)
    assert paper.get_book(b1["id"])["status"] == "archived" and paper.current_book()["id"] == b2["id"]
    assert len(paper.fills(b1["id"])) == 1 and paper.fills(b2["id"]) == []
    assert b2["params"] == b1["params"]                                      # same limits unless changed
    panel = paper.panel()
    assert {x["status"] for x in panel["books"]} == {"archived", "active"} and panel["simulation_only"] is True
    with pytest.raises(paper.PaperError):
        paper.pause(b1["id"])
    with pytest.raises(paper.PaperError):
        paper.update_params(b1["id"], {"stop_loss_pct": 2})


def test_profitability_is_reported_against_holding_and_cash_and_is_separate_from_accuracy():
    b = paper.new_book(now=NOW)
    issue(bps=50, target_in_h=6)
    paper.evaluate(b["id"], q(4000.0), NOW)
    paper.evaluate(b["id"], q(4040.0, NOW + 3600), NOW + 3600)
    paper.evaluate(b["id"], q(4040.0, NOW + 8 * 3600), NOW + 8 * 3600)            # target passed: exit
    pf = paper.performance(b["id"])
    assert pf["n_trades"] == 1 and pf["realised_pnl"] > 0 and pf["hold"]["return_pct"] > 0 and pf["cash_only"]["return_pct"] == 0.0
    assert pf["qty"] == 0 and abs(pf["equity"] - (b["initial_cash"] + pf["realised_pnl"])) < 1e-6          # equity is derived from fills only
    acc = paper.accuracy(b["id"])
    assert acc["n_matured"] in (0, None) and "realised_pnl" not in acc                 # no outcome matured: profit says nothing about accuracy
    assert len(pf["curve"]) >= 3 and set(pf["curve"][0]) == {"ts", "book", "hold", "cash"}


def test_parameters_are_validated_and_apply_to_future_decisions_only():
    with pytest.raises(paper.PaperError):
        paper.validate_params({"stop_loss_pct": 0})
    with pytest.raises(paper.PaperError):
        paper.validate_params({"task": "nowcast"})
    with pytest.raises(paper.PaperError):
        paper.new_book(initial_cash=5)
    b = paper.new_book(now=NOW)
    b2 = paper.update_params(b["id"], {"max_position_pct": 10})
    assert b2["params"]["max_position_pct"] == 10.0 and b2["params"]["task"] == "fwd_5"


def test_fills_are_the_only_source_of_cash_and_position():
    b = paper.new_book(now=NOW)
    issue(bps=50)
    paper.evaluate(b["id"], q(4000.0), NOW)
    cash, qty, avg = paper.cash_and_position(b["id"])
    f = paper.fills(b["id"])[0]
    assert abs(cash - (b["initial_cash"] - f["qty"] * f["price"] - f["cost"])) < 1e-6 and abs(qty - f["qty"]) < 1e-9 and abs(avg - f["price"]) < 1e-6


def test_run_all_needs_a_quote_function_and_never_fetches_by_itself():
    paper.new_book(now=NOW)
    assert "skipped" in paper.run_all(None, NOW)
    issue(bps=50)
    assert paper.run_all(lambda: q(4000.0), NOW)["actions"]
