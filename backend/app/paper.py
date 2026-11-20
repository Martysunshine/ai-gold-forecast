"""Paper trading: a deterministic, gold-only SIMULATION. It has no broker integration and cannot place a real order.

Architecture (the idea of a risk gate that can only reduce activity, an append-only fill ledger and persisted pause/loss windows comes from
https://github.com/Martysunshine/ai-stock-trader, MIT licence, Copyright (c) 2026 Martin Matysek; no code was copied, this is an independent
implementation sized for one instrument):

  * A *book* is a simulated account: cash, at most one long gold position, fills, equity snapshots and its own frozen risk/cost parameters.
  * Entries follow a rule, not a model's mood: a stored, immutable forecast (default: the 5-session champion) must predict a return that beats the
    round-trip cost plus a configured margin. The forecast id is written on the fill. A model can only ever REDUCE what the limits allow.
  * Long or flat only. Exits: the forecast's target time, a stop-loss, or a risk-limit breach (which also pauses the book).
  * Fills are simulated at the latest delayed futures quote plus an ASSUMED spread/slippage (editable; not a fact about any broker). Each
    decision is posted at most once (unique order key).
  * Starting a fresh book archives the old one. Nothing is ever deleted: fills, equity snapshots, events and archived books stay.
  * Prediction accuracy and trading profitability are separate: this module reports P&L; accuracy comes from the forecast ledger.

SAFETY: this module imports no HTTP/socket/broker library and takes its quote from a caller-supplied function. ``tests/test_paper.py`` fails if
that ever changes. Nothing here can reach a live order endpoint.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import db, ledger

NY = ZoneInfo("America/New_York")
MIN_QTY, QTY_STEP = 0.01, 0.01          # troy ounces

DEFAULT_PARAMS = {
    "task": "fwd_5", "role": "champion",                 # which stored forecasts may open a position
    "min_net_edge_bps": 5.0,                              # predicted return must exceed round-trip cost + this, in basis points
    "spread_bps": 2.0, "slippage_bps": 1.0, "commission_per_oz": 0.0,   # ASSUMED costs: edit them to match a real broker's schedule
    "max_position_pct": 50.0,                             # notional as % of equity
    "stop_loss_pct": 3.0,
    "max_daily_loss_pct": 2.0, "max_drawdown_pct": 10.0,
    "max_trades_per_day": 2, "cooldown_minutes": 60,
    "quote_max_age_min": 25,                              # the delayed futures feed is ~10 min behind; older than this is stale
}
LIMITS = {"min_net_edge_bps": (0, 500), "spread_bps": (0, 100), "slippage_bps": (0, 100), "commission_per_oz": (0, 50), "max_position_pct": (1, 100),
          "stop_loss_pct": (0.1, 50), "max_daily_loss_pct": (0.1, 50), "max_drawdown_pct": (0.5, 90), "max_trades_per_day": (1, 20), "cooldown_minutes": (0, 1440),
          "quote_max_age_min": (1, 240)}
TASKS = ("fwd_1", "fwd_5", "fwd_20")
ROLES = ("champion", "shadow", "baseline")


class PaperError(ValueError):
    pass


def validate_params(p: dict) -> dict:
    out = {**DEFAULT_PARAMS, **p}
    if out["task"] not in TASKS or out["role"] not in ROLES:
        raise PaperError(f"task must be one of {TASKS} and role one of {ROLES}")
    for k, (lo, hi) in LIMITS.items():
        try:
            v = float(out[k])
        except (TypeError, ValueError):
            raise PaperError(f"{k} must be a number") from None
        if not (lo <= v <= hi):
            raise PaperError(f"{k} must be between {lo} and {hi}")
        out[k] = int(v) if k in ("max_trades_per_day", "cooldown_minutes", "quote_max_age_min") else v
    return {k: out[k] for k in DEFAULT_PARAMS}


def _event(book_id: int | None, kind: str, detail: str, forecast_id: str | None = None, now: float | None = None, once: bool = False) -> None:
    if once and db.rows("SELECT 1 FROM paper_events WHERE book_id IS ? AND kind=? AND detail=? AND forecast_id IS ?", (book_id, kind, detail, forecast_id)):
        return
    db.connect().execute("INSERT INTO paper_events (book_id, ts, kind, detail, forecast_id) VALUES (?,?,?,?,?)", (book_id, now or time.time(), kind, detail, forecast_id))


# ------------------------------------------------------------------------------------------------------------------------------ books
def current_book() -> dict | None:
    r = db.rows("SELECT * FROM paper_books WHERE status IN ('active','paused') ORDER BY id DESC LIMIT 1")
    return _load(r[0]) if r else None


def _load(b: dict) -> dict:
    return {**b, "params": json.loads(b["params"])}


def get_book(book_id: int) -> dict:
    r = db.rows("SELECT * FROM paper_books WHERE id=?", (book_id,))
    if not r:
        raise PaperError("no such book")
    return _load(r[0])


def new_book(name: str | None = None, initial_cash: float = 10_000.0, params: dict | None = None, now: float | None = None) -> dict:
    """Start a fresh book. The current one (if any) is archived with its final state kept; nothing is deleted."""
    now = now or time.time()
    if not (100 <= float(initial_cash) <= 10_000_000):
        raise PaperError("initial cash must be between 100 and 10,000,000 (simulated USD)")
    cur = current_book()
    base = validate_params(params if params is not None else (cur["params"] if cur else {}))
    with db.tx() as c:
        if cur:
            c.execute("UPDATE paper_books SET status='archived', archived_at=? WHERE id=?", (now, cur["id"]))
        n = c.execute("SELECT COUNT(*) AS n FROM paper_books").fetchone()["n"] + 1
        bid = c.execute("INSERT INTO paper_books (name, created_at, status, initial_cash, params) VALUES (?,?,?,?,?)",
                        (name or f"Paper book {n}", now, "active", float(initial_cash), json.dumps(base))).lastrowid
    _event(bid, "new_book" if not cur else "reset", f"started with {float(initial_cash):,.2f} simulated USD" + (f"; archived book {cur['id']}" if cur else ""), now=now)
    return get_book(bid)


def ensure_book() -> dict:
    return current_book() or new_book()


def update_params(book_id: int, patch: dict) -> dict:
    b = get_book(book_id)
    if b["status"] == "archived":
        raise PaperError("an archived book's parameters are frozen")
    new = validate_params({**b["params"], **patch})
    db.connect().execute("UPDATE paper_books SET params=? WHERE id=?", (json.dumps(new), book_id))
    changed = {k: (b["params"][k], new[k]) for k in new if new[k] != b["params"][k]}
    _event(book_id, "signal", "parameters changed (future decisions only): " + json.dumps(changed), None)
    return get_book(book_id)


def pause(book_id: int, reason: str = "paused by you", now: float | None = None) -> dict:
    b = get_book(book_id)
    if b["status"] == "archived":
        raise PaperError("an archived book cannot be paused")
    db.connect().execute("UPDATE paper_books SET status='paused', paused_reason=? WHERE id=?", (reason, book_id))
    _event(book_id, "pause", reason, now=now)
    return get_book(book_id)


def resume(book_id: int, now: float | None = None) -> dict:
    b = get_book(book_id)
    if b["status"] == "archived":
        raise PaperError("an archived book cannot be resumed")
    db.connect().execute("UPDATE paper_books SET status='active', paused_reason=NULL WHERE id=?", (book_id,))
    _event(book_id, "resume", "resumed", now=now)
    return get_book(book_id)


# ------------------------------------------------------------------------------------------------------------------------------ accounting
def fills(book_id: int) -> list[dict]:
    return db.rows("SELECT * FROM paper_fills WHERE book_id=? ORDER BY ts, id", (book_id,))


def cash_and_position(book_id: int) -> tuple[float, float, float]:
    """(cash, qty, average entry price) derived ONLY from posted fills (the books cannot disagree with their fills)."""
    b = get_book(book_id)
    cash, qty, cost_basis = b["initial_cash"], 0.0, 0.0
    for f in fills(book_id):
        if f["side"] == "buy":
            cash -= f["qty"] * f["price"] + f["cost"]
            qty += f["qty"]
            cost_basis += f["qty"] * f["price"]
        else:
            avg = cost_basis / qty if qty else 0.0
            cash += f["qty"] * f["price"] - f["cost"]
            qty -= f["qty"]
            cost_basis -= avg * f["qty"]
    qty = 0.0 if abs(qty) < 1e-9 else qty
    return cash, qty, (cost_basis / qty if qty else 0.0)


def _half_cost(p: dict) -> float:
    return (p["spread_bps"] / 2 + p["slippage_bps"]) / 1e4


def round_trip_bps(p: dict, price: float) -> float:
    return 2 * (p["spread_bps"] / 2 + p["slippage_bps"]) + 2 * p["commission_per_oz"] / price * 1e4


def _post(book_id: int, side: str, qty: float, quote_price: float, p: dict, now: float, forecast_id: str | None, rationale: str, reason: str, key: str) -> dict | None:
    price = quote_price * (1 + _half_cost(p)) if side == "buy" else quote_price * (1 - _half_cost(p))
    cur = db.connect().execute("INSERT OR IGNORE INTO paper_fills (book_id, ts, side, qty, price, cost, forecast_id, rationale, reason, order_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (book_id, now, side, qty, round(price, 4), round(p["commission_per_oz"] * qty, 4), forecast_id, rationale, reason, key))
    return db.rows("SELECT * FROM paper_fills WHERE id=?", (cur.lastrowid,))[0] if cur.rowcount else None


def snapshot(book_id: int, mark: float, now: float) -> dict:
    cash, qty, _ = cash_and_position(book_id)
    b = get_book(book_id)
    eq = cash + qty * mark
    ts = int(now // 60) * 60
    db.connect().execute("INSERT OR REPLACE INTO paper_equity (book_id, ts, cash, qty, mark, equity) VALUES (?,?,?,?,?,?)", (book_id, ts, cash, qty, mark, eq))
    if b["baseline_price"] is None:
        db.connect().execute("UPDATE paper_books SET baseline_price=?, baseline_at=? WHERE id=?", (mark, now, book_id))
    return {"cash": cash, "qty": qty, "mark": mark, "equity": eq}


def _day_start_equity(book_id: int, now: float, initial: float) -> float:
    d = datetime.fromtimestamp(now, NY).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    r = db.rows("SELECT equity FROM paper_equity WHERE book_id=? AND ts<? ORDER BY ts DESC LIMIT 1", (book_id, d))
    if r:
        return r[0]["equity"]
    first = db.rows("SELECT equity FROM paper_equity WHERE book_id=? AND ts>=? ORDER BY ts LIMIT 1", (book_id, d))
    return first[0]["equity"] if first else initial


def _peak_equity(book_id: int, initial: float) -> float:
    r = db.rows("SELECT MAX(equity) AS m FROM paper_equity WHERE book_id=?", (book_id,))[0]["m"]
    return max(initial, r or 0.0)


# ------------------------------------------------------------------------------------------------------------------------------ decisions
def evaluate(book_id: int, quote: dict, now: float | None = None) -> dict:
    """One deterministic pass: mark, apply risk limits, exit if required, otherwise consider one entry. ``quote`` = {price, as_of}."""
    now = now or time.time()
    b = get_book(book_id)
    p = b["params"]
    out = {"book": book_id, "status": b["status"], "actions": [], "blocked": None}
    if b["status"] != "active":
        out["blocked"] = f"book is {b['status']}" + (f": {b['paused_reason']}" if b["paused_reason"] else "")
        return out
    age_min = (now - quote["as_of"]) / 60
    if age_min > p["quote_max_age_min"]:
        out["blocked"] = f"quote is {age_min:.0f} min old (limit {p['quote_max_age_min']}): no decisions on a stale price"
        _event(book_id, "blocked", out["blocked"][:120], None, now, once=True)
        return out
    price = float(quote["price"])
    snap = snapshot(book_id, price, now)
    cash, qty, avg = cash_and_position(book_id)
    equity = snap["equity"]
    day_start, peak = _day_start_equity(book_id, now, b["initial_cash"]), _peak_equity(book_id, b["initial_cash"])
    daily_loss = (day_start - equity) / day_start * 100 if day_start else 0.0
    drawdown = (peak - equity) / peak * 100 if peak else 0.0
    breach = None
    if daily_loss >= p["max_daily_loss_pct"]:
        breach = f"daily loss {daily_loss:.2f}% reached the {p['max_daily_loss_pct']}% limit"
    elif drawdown >= p["max_drawdown_pct"]:
        breach = f"drawdown {drawdown:.2f}% reached the {p['max_drawdown_pct']}% limit"
    open_fill = db.rows("SELECT * FROM paper_fills WHERE book_id=? AND side='buy' ORDER BY id DESC LIMIT 1", (book_id,)) if qty > 0 else []
    if qty > 0:
        entry = open_fill[0]
        f = db.rows("SELECT * FROM forecasts WHERE id=?", (entry["forecast_id"],)) if entry["forecast_id"] else []
        target_ts = pd.Timestamp(f[0]["target_time"]).timestamp() if f else None
        reason = None
        if breach:
            reason = ("risk", breach)
        elif price <= avg * (1 - p["stop_loss_pct"] / 100):
            reason = ("stop", f"price {price:,.2f} is {p['stop_loss_pct']}% or more below the average entry {avg:,.2f}")
        elif target_ts and now >= target_ts:
            reason = ("target", "the forecast's target time has passed")
        if reason:
            fill = _post(book_id, "sell", qty, price, p, now, entry["forecast_id"], f"exit ({reason[0]}): {reason[1]}", reason[0], f"exit|{entry['order_key']}")
            if fill:
                out["actions"].append({"sold": qty, "reason": reason[0]})
                _event(book_id, "signal", f"EXIT {qty:.2f} oz ({reason[0]}): {reason[1]}", entry["forecast_id"], now)
        if breach:
            pause(book_id, breach, now)
            out["status"] = "paused"
        snapshot(book_id, price, now)
        return out
    if breach:
        pause(book_id, breach, now)
        out["status"], out["blocked"] = "paused", breach
        return out
    # --- flat: consider one entry from the latest stored forecast
    h = {"fwd_1": 1, "fwd_5": 5, "fwd_20": 20}[p["task"]]
    f = ledger.latest(p["task"], p["role"], horizon=h)
    if not f:
        out["blocked"] = f"no stored {p['task']} {p['role']} forecast yet"
        return out
    if pd.Timestamp(f["target_time"]).timestamp() <= now:
        out["blocked"] = "the latest forecast's target time has passed; waiting for a new one"
        return out
    r_bps = (f["predicted"] / f["base_price"] - 1) * 1e4
    cost_bps = round_trip_bps(p, price)
    need = cost_bps + p["min_net_edge_bps"]
    if r_bps <= need:
        _event(book_id, "signal", f"no trade: predicted {r_bps:+.1f} bps does not beat round-trip cost {cost_bps:.1f} + margin {p['min_net_edge_bps']:.1f} bps", f["id"], now, once=True)
        out["blocked"] = "forecast does not beat costs"
        return out
    if db.rows("SELECT 1 FROM paper_fills WHERE book_id=? AND order_key=?", (book_id, f"entry|{f['id']}")):
        out["blocked"] = "this forecast was already traded"
        return out
    today0 = datetime.fromtimestamp(now, NY).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    n_today = db.rows("SELECT COUNT(*) AS n FROM paper_fills WHERE book_id=? AND ts>=?", (book_id, today0))[0]["n"]
    last = db.rows("SELECT MAX(ts) AS t FROM paper_fills WHERE book_id=?", (book_id,))[0]["t"]
    if n_today >= p["max_trades_per_day"]:
        out["blocked"] = f"{n_today} fills today reached the daily limit of {p['max_trades_per_day']}"
    elif last and now - last < p["cooldown_minutes"] * 60:
        out["blocked"] = f"cooling down for {p['cooldown_minutes']} min after the last fill"
    if out["blocked"]:
        _event(book_id, "blocked", out["blocked"], f["id"], now, once=True)
        return out
    notional = min(equity * p["max_position_pct"] / 100, cash)
    buy_price = price * (1 + _half_cost(p))
    q = np.floor((notional / (buy_price + p["commission_per_oz"])) / QTY_STEP) * QTY_STEP
    if q < MIN_QTY:
        out["blocked"] = "position size rounds to less than the minimum quantity"
        return out
    why = (f"{p['task']} {p['role']} forecast ${f['predicted']:,.2f} for {f['target_date']} is {r_bps:+.1f} bps above its reference ${f['base_price']:,.2f}, "
           f"beating round-trip cost {cost_bps:.1f} bps + margin {p['min_net_edge_bps']:.1f} bps")
    fill = _post(book_id, "buy", float(q), price, p, now, f["id"], why, "entry", f"entry|{f['id']}")
    if fill:
        out["actions"].append({"bought": float(q), "forecast_id": f["id"]})
        _event(book_id, "signal", f"ENTER {q:.2f} oz: {why}", f["id"], now)
    snapshot(book_id, price, now)
    return out


def flatten(book_id: int, quote: dict, now: float | None = None) -> dict:
    """Close the simulated position at the last quote (a manual action in the simulation)."""
    now = now or time.time()
    b = get_book(book_id)
    _, qty, _ = cash_and_position(book_id)
    if qty <= 0:
        raise PaperError("the book is already flat")
    last = db.rows("SELECT * FROM paper_fills WHERE book_id=? AND side='buy' ORDER BY id DESC LIMIT 1", (book_id,))[0]
    fill = _post(book_id, "sell", qty, float(quote["price"]), b["params"], now, last["forecast_id"], "closed by you in the simulation", "manual", f"manual|{int(now)}")
    snapshot(book_id, float(quote["price"]), now)
    _event(book_id, "signal", f"manual close of {qty:.2f} oz", last["forecast_id"], now)
    return fill or {}


def run_all(quote_fn=None, now: float | None = None) -> dict:
    """Worker entry: evaluate the active book. ``quote_fn`` supplies the latest futures quote (this module never fetches anything itself)."""
    b = current_book()
    if b is None or quote_fn is None:
        return {"books": 0 if b is None else 1, "skipped": "no active book" if b is None else "no quote function supplied"}
    try:
        q = quote_fn()
    except Exception as exc:  # noqa: BLE001
        _event(b["id"], "error", f"no quote: {exc.__class__.__name__}", None, now)
        raise
    return evaluate(b["id"], q, now)


# ------------------------------------------------------------------------------------------------------------------------------ reporting
def trades(book_id: int) -> list[dict]:
    """Round trips (long-only, one position at a time): realised P&L net of the simulated spread/slippage (in the fill prices) and commissions."""
    out, entry = [], None
    for f in fills(book_id):
        if f["side"] == "buy":
            entry = f
        elif entry:
            gross = f["qty"] * (f["price"] - entry["price"])
            out.append({"forecast_id": entry["forecast_id"], "entry_ts": entry["ts"], "exit_ts": f["ts"], "qty": f["qty"], "entry": entry["price"], "exit": f["price"],
                        "pnl": gross - entry["cost"] - f["cost"], "exit_reason": f["reason"], "rationale": entry["rationale"]})
            entry = None
    return out


def performance(book_id: int) -> dict:
    b = get_book(book_id)
    p = b["params"]
    eq = db.rows("SELECT ts, cash, qty, mark, equity FROM paper_equity WHERE book_id=? ORDER BY ts", (book_id,))
    cash, qty, avg = cash_and_position(book_id)
    last_mark = eq[-1]["mark"] if eq else None
    equity = cash + qty * (last_mark or 0.0)
    tr = trades(book_id)
    realised = sum(t["pnl"] for t in tr)
    unreal = qty * (last_mark - avg) if qty and last_mark else 0.0
    fees = sum(f["cost"] for f in fills(book_id))
    init = b["initial_cash"]
    out = {"initial_cash": init, "cash": cash, "qty": qty, "avg_entry": avg or None, "mark": last_mark, "equity": equity, "return_pct": (equity / init - 1) * 100,
           "realised_pnl": realised, "unrealised_pnl": unreal, "fees": fees, "n_trades": len(tr), "n_fills": len(fills(book_id)),
           "win_rate_pct": (sum(1 for t in tr if t["pnl"] > 0) / len(tr) * 100) if tr else None,
           "max_drawdown_pct": None, "exposure_pct": None, "hold": None, "cash_only": {"equity": init, "return_pct": 0.0}}
    if eq:
        e = np.array([x["equity"] for x in eq])
        out["max_drawdown_pct"] = float(((np.maximum.accumulate(e) - e) / np.maximum.accumulate(e)).max() * 100)
        out["exposure_pct"] = float(np.mean([x["qty"] * x["mark"] / x["equity"] for x in eq if x["equity"]]) * 100)
        p0 = b["baseline_price"] or eq[0]["mark"]
        hc = _half_cost(p)
        oz = init / (p0 * (1 + hc) + p["commission_per_oz"])
        hold_end = oz * (last_mark * (1 - hc) - p["commission_per_oz"])    # liquidated at the last mark, with the same simulated costs
        out["hold"] = {"equity": hold_end, "return_pct": (hold_end / init - 1) * 100, "start_price": p0}
        out["curve"] = [{"ts": x["ts"], "book": round(x["equity"], 2), "hold": round(oz * (x["mark"] * (1 - hc) - p["commission_per_oz"]), 2), "cash": init} for x in eq][-1500:]
    else:
        out["curve"] = []
    return out


def accuracy(book_id: int) -> dict:
    """Forecast ACCURACY for the task driving this book: a different question from whether it made money. From the ledger only."""
    p = get_book(book_id)["params"]
    st = ledger.stats(p["task"], role=p["role"])
    return {"task": p["task"], "role": p["role"], **{k: st.get(k) for k in ("n_matured", "n_sessions", "mae", "baseline_mae", "mae_vs_baseline_pct", "coverage_pct")}}


def panel(now: float | None = None) -> dict:
    now = now or time.time()
    b = current_book()
    books = [{"id": x["id"], "name": x["name"], "status": x["status"], "created_at": x["created_at"], "archived_at": x["archived_at"], "initial_cash": x["initial_cash"]}
             for x in db.rows("SELECT * FROM paper_books ORDER BY id DESC")]
    for x in books:
        pf = performance(x["id"]) if db.rows("SELECT 1 FROM paper_equity WHERE book_id=? LIMIT 1", (x["id"],)) else None
        x["return_pct"] = pf["return_pct"] if pf else 0.0
        x["n_trades"] = pf["n_trades"] if pf else 0
    out = {"simulation_only": True, "notice": "Paper trading is a simulation. No broker is connected and no live order can be placed from this app. Costs are assumptions you can edit.",
           "defaults": DEFAULT_PARAMS, "limits": LIMITS, "books": books, "book": None}
    if b:
        out["book"] = {**{k: b[k] for k in ("id", "name", "status", "paused_reason", "created_at", "initial_cash", "params")},
                       "performance": performance(b["id"]), "accuracy": accuracy(b["id"]),
                       "fills": [{k: f[k] for k in ("id", "ts", "side", "qty", "price", "cost", "forecast_id", "rationale", "reason")} for f in fills(b["id"])][-60:][::-1],
                       "trades": trades(b["id"])[-30:][::-1],
                       "events": db.rows("SELECT ts, kind, detail, forecast_id FROM paper_events WHERE book_id=? ORDER BY id DESC LIMIT 25", (b["id"],))}
    return out
