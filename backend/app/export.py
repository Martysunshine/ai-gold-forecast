"""The forecast as a file for other programs: ``python -m app.export --out forecast.json``.

One JSON document holds the active (champion) forecast of every horizon, read from the immutable forecast ledger that
the learning worker (``python -m app.worker run``) fills. The export never fits a model, never contacts a provider and
never writes to the ledger: the same forecast id gives the same numbers on every run, so a reader sees one call per
session, not a number that moves each time it asks.

Each forecast states the period it applies to (``valid_from`` to ``valid_until``) and how its model has done, so a
reader can tell a current forecast from an old one and a proven model from an unproven one without knowing how the
models work. ``evaluation.advantage`` is ``true`` only while the champion is a model the learning loop promoted
(it beat the unchanged price on the walk-forward test and on its own live record); the seeded model never has it.

Nothing here places or suggests a trade. Run from ``backend/``. The exit code is 0 when the file was written and 1 when
it was not; a failed run leaves an existing file as it was.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import config
from .data import DataStore, Dataset

SCHEMA = "ai-gold-forecast.forecast/1"
#: Daily bars are dated by the exchange's local calendar day. A forecast for a target date is about that day's
#: settlement, and stops applying when the day ends there.
BAR_TIMEZONE = "America/New_York"
HORIZONS = (1, 5, 20)
#: The worker's daily full cycle (``worker.CYCLE_UTC``; a test keeps the two equal without importing the worker here).
CYCLE_UTC = (23, 30)
#: A worker heartbeat older than this means no worker is running (the worker writes one every 20 s; ``worker.status`` uses 90 s).
HEARTBEAT_STALE_S = 90
#: Ledger rows flagged as "not a model output" (``ledger.FALLBACK_KIND``), left out of every record.
FALLBACK_KIND = "fallback_not_a_model"

ALGO_LABEL = {"unchanged": "Unchanged price", "ridge": "Ridge regression", "hgb": "Gradient boosting",
              "random_forest": "Random Forest", "extra_trees": "Extra trees"}
SCHEMA_LABEL = {"gold_v1": "gold prices", "drivers_v1": "gold and market drivers", "patterns_v1": "gold and price patterns",
                "fib_v1": "gold, patterns and Fibonacci levels"}


class NoForecast(Exception):
    """Nothing usable to export (no ledger yet, no saved prices, or no champion forecast)."""


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_until(target_date: str, tz: str = BAR_TIMEZONE) -> str:
    """The instant the target date ends in the bars' timezone, in UTC."""
    next_day = pd.Timestamp(target_date) + pd.Timedelta(days=1)  # local midnight, so no daylight-saving drift
    return next_day.tz_localize(tz).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def direction_of(change_pct: float) -> str:
    return "up" if change_pct > 0 else "down" if change_pct < 0 else "flat"


def next_cycle(now: datetime) -> datetime:
    t = now.replace(hour=CYCLE_UTC[0], minute=CYCLE_UTC[1], second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _open(db_path: Path) -> sqlite3.Connection:
    """Read-only: the export must never create, migrate or change the worker's database."""
    if not db_path.exists():
        raise NoForecast(f"no forecast ledger at {db_path.name}: the learning worker has not run yet")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _champion(conn: sqlite3.Connection, task: str) -> dict | None:
    r = _rows(conn, "SELECT * FROM models WHERE task=? AND status='champion'", (task,))
    return r[0] if r else None


def _promoted(conn: sqlite3.Connection, task: str, version: str) -> dict | None:
    """The decision that made ``version`` champion, when that decision was a promotion (not the seed, not a rollback)."""
    r = _rows(conn, "SELECT * FROM decisions WHERE task=? AND champion_after=? AND decision IN ('promote','seed','rollback') "
                    "ORDER BY id DESC LIMIT 1", (task, version))
    return r[0] if r and r[0]["decision"] == "promote" else None


def _development(conn: sqlite3.Connection, task: str, version: str) -> dict | None:
    r = _rows(conn, "SELECT metrics, data_through FROM dev_evals WHERE task=? AND version=? ORDER BY data_through DESC LIMIT 1", (task, version))
    if not r:
        return None
    try:
        return {**json.loads(r[0]["metrics"]), "data_through": r[0]["data_through"]}
    except ValueError:
        return None


def _forward(conn: sqlite3.Connection, task: str, version: str) -> dict:
    """The live record of ``version``: forecasts whose outcome exists (fallback rows flagged in the audit are left out)."""
    r = _rows(conn, "SELECT COUNT(*) AS n, AVG(o.abs_error) AS mae, AVG(o.baseline_abs_error) AS base FROM forecasts f "
                    "JOIN outcomes o ON o.forecast_id=f.id WHERE f.task=? AND f.model_version=? "
                    "AND f.id NOT IN (SELECT forecast_id FROM ledger_audit WHERE kind=?)", (task, version, FALLBACK_KIND))[0]
    out = {"n_matured": int(r["n"] or 0)}
    if r["n"] and r["base"]:
        out["mae_vs_unchanged_pct"] = round((r["mae"] / r["base"] - 1) * 100, 4)
    return out


def _evaluation(conn: sqlite3.Connection, task: str, champ: dict) -> dict:
    version = champ["version"]
    dev = _development(conn, task, version)
    promo = _promoted(conn, task, version)
    ev: dict = {"basis": "walk-forward test of the champion, each origin fitted on earlier data only", "forward": _forward(conn, task, version)}
    if dev:
        ev.update({"data_through": dev.get("data_through"), "n": dev.get("n_origins"),
                   "mae_model_usd": None if dev.get("mae") is None else round(dev["mae"], 4),
                   "mae_unchanged_usd": None if dev.get("baseline_mae") is None else round(dev["baseline_mae"], 4),
                   "vs_unchanged_pct": None if dev.get("mae_vs_baseline_pct") is None else round(dev["mae_vs_baseline_pct"], 4),
                   "directional_accuracy_pct": None if dev.get("direction_pct") is None else round(dev["direction_pct"], 2)})
    ev["advantage"] = promo is not None
    if promo:
        ev["verdict"] = f"Promoted {_iso(promo['at'])[:10]}: beat the unchanged price on the walk-forward test and on its live record"
    else:
        ev["verdict"] = "No demonstrated advantage over the unchanged-price baseline (seeded model, not yet beaten by a challenger)"
    return ev


def forecast_entry(row: dict, evaluation: dict, generated_at: str) -> dict:
    """One champion ledger row in the export's shape."""
    base, price = float(row["base_price"]), float(row["predicted"])
    change = round((price / base - 1) * 100, 6)
    low, high = float(row["low"]), float(row["high"])
    algo, schema = row["algorithm"], row["feature_schema"]
    label = ALGO_LABEL.get(algo, algo) + (f", {SCHEMA_LABEL[schema]}" if schema in SCHEMA_LABEL else "")
    return {
        "horizon_sessions": int(row["horizon"]),
        "base_date": row["base_time"],
        "target_date": row["target_date"],
        "target_time": row["target_time"],
        "base_price": round(base, 4),
        "price": round(price, 4),
        "change_pct": change,
        "direction": direction_of(change),
        "range": {"level": row["level"], "low": round(low, 4), "high": round(high, 4),
                  "low_pct": round((low / base - 1) * 100, 6), "high_pct": round((high / base - 1) * 100, 6)},
        "model": row["model_version"],
        "model_label": label,
        "forecast_id": row["id"],
        "issued_at": _iso(row["issued_at"]),
        "valid_from": generated_at,
        "valid_until": valid_until(row["target_date"]),
        "evaluation": evaluation,
    }


def _learn_state(detail: str | None) -> str:
    return "blocked" if (detail or "").startswith('{"skipped"') else "learned"


def learning_block(conn: sqlite3.Connection, data_dir: Path, now: datetime) -> dict:
    """What the learning loop did last, so a reader can tell a model that learns every day from one that stopped."""
    try:
        hb = json.loads((data_dir / "worker_heartbeat.json").read_text())
    except (OSError, ValueError):
        hb = None
    age = None if not hb else now.timestamp() - float(hb.get("at", 0))
    last = _rows(conn, "SELECT finished_at, status, detail FROM job_runs WHERE job='learn' AND finished_at IS NOT NULL "
                       "AND status IN ('ok','error') ORDER BY id DESC LIMIT 1")
    learned = _rows(conn, "SELECT finished_at FROM job_runs WHERE job='learn' AND status='ok' AND (detail IS NULL OR detail NOT LIKE '{\"skipped\"%') "
                          "ORDER BY id DESC LIMIT 1")
    sched = _rows(conn, "SELECT id, started_at, finished_at, status, data_through FROM cycle_runs WHERE trigger='scheduled' ORDER BY id DESC LIMIT 1")
    cycle = _rows(conn, "SELECT id, trigger, started_at, finished_at, status, data_through FROM cycle_runs ORDER BY id DESC LIMIT 1")

    def cyc(r: list[dict]) -> dict | None:
        if not r:
            return None
        c = r[0]
        return {k: c[k] for k in c if k not in ("started_at", "finished_at")} | {"started_at": _iso(c["started_at"]), "finished_at": _iso(c["finished_at"])}

    champions = {}
    for task in ("fwd_1", "fwd_5", "fwd_20", "nowcast"):
        ch = _champion(conn, task)
        if ch:
            champions[task] = {"version": ch["version"], "since": _iso(ch["status_changed_at"]), "promoted": _promoted(conn, task, ch["version"]) is not None}
    last_learn = None
    if last:
        state = "error" if last[0]["status"] == "error" else _learn_state(last[0]["detail"])
        last_learn = {"at": _iso(last[0]["finished_at"]), "result": state, "note": None if state == "learned" else (last[0]["detail"] or "")[:200]}
    return {
        "worker": {"running": age is not None and age < HEARTBEAT_STALE_S, "runtime": (hb or {}).get("runtime"),
                   "heartbeat_at": _iso((hb or {}).get("at"))},
        "last_learned_at": _iso(learned[0]["finished_at"]) if learned else None,
        "last_learn": last_learn,
        "last_cycle": cyc(cycle),
        "last_scheduled_cycle": cyc(sched),
        "next_cycle_at": next_cycle(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "champions": champions,
        "promotions": int(_rows(conn, "SELECT COUNT(*) AS n FROM decisions WHERE decision='promote'")[0]["n"]),
        "matured_forecasts": int(_rows(conn, "SELECT COUNT(*) AS n FROM outcomes")[0]["n"]),
    }


def build_export(ds: Dataset, conn: sqlite3.Connection, data_dir: Path, now: datetime | None = None) -> dict:
    """The export document. Raises ``NoForecast`` when the ledger holds no champion forecast."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    forecasts = []
    for h in HORIZONS:
        task = f"fwd_{h}"
        champ = _champion(conn, task)
        if champ is None:
            continue
        rows = _rows(conn, "SELECT * FROM forecasts WHERE task=? AND role='champion' AND horizon=? AND model_version=? "
                           "ORDER BY base_time DESC, issued_at DESC LIMIT 1", (task, h, champ["version"]))
        if rows:
            forecasts.append(forecast_entry(rows[0], _evaluation(conn, task, champ), generated_at))
    if not forecasts:
        raise NoForecast("the ledger holds no champion forecast yet")
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "symbol": ds.meta.get("symbol"),
        "instrument": (ds.meta.get("instrument") or {}).get("name"),
        "source": ds.meta.get("source"),
        "bar_timezone": BAR_TIMEZONE,
        "data": {
            "key": ds.key,
            "latest_close_date": ds.series.index[-1].date().isoformat(),
            "latest_close": round(float(ds.series.iloc[-1]), 4),
            "fetched_at": ds.meta.get("fetched_at"),
            "provider_error": ds.meta.get("provider_error"),
            "in_progress": ds.meta.get("in_progress"),
        },
        "forecasts": forecasts,
        "learning": learning_block(conn, data_dir, now),
        "note": "A forecast, not advice. Check evaluation.advantage before relying on a direction.",
    }


def write_export(doc: dict, path: Path) -> None:
    """Replace ``path`` in one step, so a reader never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    for attempt in range(5):     # Windows refuses a replace while a reader has the file open; that lasts milliseconds
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def main(argv: list[str] | None = None, data_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.export", description="Write the current champion forecasts as one JSON file.")
    parser.add_argument("--out", required=True, type=Path, help="The file to write (replaced in one step).")
    args = parser.parse_args(argv)
    data_dir = Path(data_dir or config.DATA_DIR)
    try:
        ds = DataStore(data_dir).cached()
        if ds is None:
            raise NoForecast("no saved prices: the learning worker has not collected data yet")
        conn = _open(data_dir / "gold.db")
        try:
            doc = build_export(ds, conn, data_dir)
        finally:
            conn.close()
    except (NoForecast, sqlite3.Error) as exc:
        print(f"no forecast written: {exc}", file=sys.stderr)
        return 1
    write_export(doc, args.out)
    one = doc["forecasts"][0]
    print(f"forecast written: {one['direction']} {one['change_pct']:+.2f}% to {one['target_date']} ({one['model_label']}, "
          f"issued {one['issued_at']}), valid until {one['valid_until']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
