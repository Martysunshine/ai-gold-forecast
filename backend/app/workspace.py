"""Read-only payload for the Learning page: what is running, what is in charge per horizon, how it is doing, and whether it is improving.

Two kinds of evidence are kept apart on purpose:
  * ``development``  walk-forward results on history. They are re-examined every day, so they are NOT an untouched test.
  * ``forward``      forecasts recorded BEFORE their outcome existed, scored after. Only these are out-of-sample proof.
Nothing here writes anything.
"""
from __future__ import annotations

import json

import numpy as np

from . import db, learning, ledger, quality, registry, worker

HORIZON = {"nowcast": 1, "fwd_1": 1, "fwd_5": 5, "fwd_20": 20}
TASK_LABEL = {"nowcast": "Next settlement", "fwd_1": "1 session", "fwd_5": "5 sessions", "fwd_20": "20 sessions"}
TREND_TOL = 0.25         # percentage points of MAE-vs-unchanged: smaller day-to-day changes are noise, not learning
ROLL = 60


def _dev_metrics(task: str, version: str) -> dict | None:
    r = db.rows("SELECT metrics, data_through FROM dev_evals WHERE task=? AND version=? ORDER BY data_through DESC LIMIT 1", (task, version))
    return {**json.loads(r[0]["metrics"]), "data_through": r[0]["data_through"]} if r else None


def _rolling(series: dict) -> dict:
    """Rolling mean absolute error of the champion vs the unchanged price over the stored walk-forward origins."""
    if not series or len(series["dates"]) < ROLL + 5:
        return {"dates": [], "model": [], "unchanged": []}
    a, p, b = (np.array(series[k], float) for k in ("actual", "pred", "base"))
    em, eb = np.abs(p - a), np.abs(b - a)
    k = np.ones(ROLL) / ROLL
    return {"window": ROLL, "dates": series["dates"][ROLL - 1:], "model": [round(float(x), 2) for x in np.convolve(em, k, "valid")], "unchanged": [round(float(x), 2) for x in np.convolve(eb, k, "valid")]}


def _forward(task: str, version: str) -> dict:
    st = ledger.stats(task, version=version)
    rows = db.rows("SELECT f.base_time, f.target_date, f.predicted, f.base_price, f.low, f.high, o.actual, o.in_range, o.abs_error, o.baseline_abs_error "
                   "FROM forecasts f JOIN outcomes o ON o.forecast_id=f.id WHERE f.task=? AND f.model_version=? ORDER BY f.target_date DESC LIMIT 40", (task, version))
    n_origins = db.rows("SELECT COUNT(DISTINCT f.base_time) AS n FROM forecasts f JOIN outcomes o ON o.forecast_id=f.id WHERE f.task=? AND f.model_version=?", (task, version))[0]["n"]
    widths = [(r["high"] - r["low"]) / r["predicted"] * 100 for r in rows if r["low"] is not None and r["high"] is not None and r["predicted"]]
    h = HORIZON[task]
    return {"n_matured": st.get("n_matured", 0), "distinct_origins": int(n_origins), "distinct_targets": st.get("n_sessions", 0),
            "effective_blocks": round(ledger.effective_blocks(st.get("n_sessions", 0), h), 1), "blocks_needed": learning.MIN_FORWARD_BLOCKS,
            "mae": st.get("mae"), "baseline_mae": st.get("baseline_mae"), "mae_vs_baseline_pct": st.get("mae_vs_baseline_pct"),
            "coverage_pct": st.get("coverage_pct"), "mean_width_pct": float(np.mean(widths)) if widths else None, "history": rows}


def _trend(task: str, version: str) -> dict:
    hist = db.rows("SELECT data_through, metrics FROM dev_evals WHERE task=? AND version=? ORDER BY data_through", (task, version))
    pts = []
    for r in hist:
        m = json.loads(r["metrics"])
        pts.append({"data_through": r["data_through"], "mae_vs_baseline_pct": m["mae_vs_baseline_pct"], "coverage_pct": m.get("coverage_pct"), "mean_width_pct": m.get("mean_width_pct"), "n_origins": m["n_origins"]})
    if len(pts) < 2:
        return {"points": pts, "verdict": "first_run", "text": "Only one evaluation exists so far, so there is nothing to compare yet."}
    d = pts[-1]["mae_vs_baseline_pct"] - pts[-2]["mae_vs_baseline_pct"]
    if abs(d) < TREND_TOL:
        v, t = "unchanged", f"No meaningful change: error versus the unchanged price moved {d:+.2f} points since {pts[-2]['data_through']} (changes under {TREND_TOL} points are noise)."
    elif d < 0:
        v, t = "improved", f"Improved: error versus the unchanged price fell {abs(d):.2f} points since {pts[-2]['data_through']} (lower is better). This is a development measure, not proof."
    else:
        v, t = "worsened", f"Worsened: error versus the unchanged price rose {d:.2f} points since {pts[-2]['data_through']} (lower is better)."
    return {"points": pts[-30:], "verdict": v, "text": t}


def _task(task: str) -> dict:
    champ = registry.champion(task)
    base = registry.baseline_version(task)
    h = HORIZON[task]
    dev_series = db.rows("SELECT series, data_through FROM dev_series WHERE task=? AND version=? ORDER BY data_through DESC LIMIT 1", (task, champ["version"]))
    series = json.loads(dev_series[0]["series"]) if dev_series else None
    dev = _dev_metrics(task, champ["version"])
    cands = []
    for v in registry.versions(task, include_rejected=True):
        if v["version"] == champ["version"] or v["algorithm"] == "unchanged":
            continue
        m = _dev_metrics(task, v["version"])
        att = db.rows("SELECT status, at, detail FROM training_attempts WHERE version=? ORDER BY id DESC LIMIT 1", (v["version"],))
        dec = db.rows("SELECT at, decision, reason FROM decisions WHERE (champion_after=? OR champion_before=?) ORDER BY id DESC LIMIT 1", (v["version"], v["version"]))
        f = _forward(task, v["version"])
        cands.append({"version": v["version"], "algorithm": v["algorithm"], "feature_schema": v["feature_schema"], "status": v["status"], "note": v["notes"],
                      "development": None if not m else {k: m.get(k) for k in ("mae_vs_baseline_pct", "p_value_vs_baseline", "coverage_pct", "mean_width_pct", "n_origins", "data_through")},
                      "forward": {k: f[k] for k in ("n_matured", "distinct_origins", "effective_blocks", "mae_vs_baseline_pct")},
                      "last_attempt": att[0] if att else None, "last_decision": dec[0] if dec else None})
    fwd = _forward(task, champ["version"])
    is_base = champ["algorithm"] == "unchanged"
    return {"task": task, "label": TASK_LABEL[task], "horizon": h,
            "active": {k: champ[k] for k in ("version", "algorithm", "feature_schema", "status_changed_at", "last_trained_at", "trained_through", "notes")} | {"is_unchanged_baseline": is_base},
            "production_note": ("The unchanged price is the production forecast: no model has shown it can beat it. Model estimates are recorded as experimental shadows."
                                if is_base else "A model is the production forecast, but it has not shown an advantage over the unchanged price. Treat it as experimental."),
            "baseline_version": base,
            "development": {"label": "Walk-forward on history (development evidence: re-examined daily, not an untouched test)", "metrics": dev, "series": series,
                            "rolling": _rolling(series), "trend": _trend(task, champ["version"])},
            "forward": {"label": "Genuine forward results (recorded before the outcome existed)", **fwd},
            "candidates": cands, "decisions": registry.decisions(8, task),
            "range": {"development_coverage_pct": (dev or {}).get("coverage_pct"), "development_width_pct": (dev or {}).get("mean_width_pct"),
                      "forward_coverage_pct": fwd["coverage_pct"], "forward_width_pct": fwd["mean_width_pct"], "target_pct": 80.0}}


def feature_groups() -> dict:
    r = db.rows("SELECT data_through, evaluated_at, result FROM feature_evals WHERE group_name='fib_v1' ORDER BY data_through DESC LIMIT 1")
    if not r:
        return {"evaluated": False, "rule": learning.fib_rule_text()}
    res = json.loads(r[0]["result"])
    return {"evaluated": True, "data_through": r[0]["data_through"], "evaluated_at": r[0]["evaluated_at"], "verdict": res["verdict"], "comparisons": res["comparisons"],
            "rejected": [x["version"] for x in db.rows("SELECT version FROM models WHERE status='rejected' AND feature_schema='fib_v1'")],
            "volume_note": "Futures volume is not used: the stored series has closes only, and continuous front-month volume is distorted by contract rolls."}


def payload() -> dict:
    registry.seed()
    cyc = db.rows("SELECT id, started_at, finished_at, trigger, runtime, data_through, status, steps FROM cycle_runs ORDER BY id DESC LIMIT 6")
    for c in cyc:
        c["steps"] = json.loads(c["steps"] or "[]")
    att = db.rows("SELECT at, task, version, stage, status, data_through, duration_s, detail FROM training_attempts ORDER BY id DESC LIMIT 20")
    return {"worker": worker.status(), "tasks": {t: _task(t) for t in ("fwd_1", "fwd_5", "fwd_20", "nowcast")}, "feature_groups": feature_groups(),
            "attempts": {"recent": att, "ok": db.rows("SELECT COUNT(*) AS n FROM training_attempts WHERE status='ok'")[0]["n"], "failed": db.rows("SELECT COUNT(*) AS n FROM training_attempts WHERE status='failed'")[0]["n"]},
            "cycles": cyc, "quality": quality.summary(),
            "schedule": worker.cycle_history(5),
            "ledger_audit": {"conflicts": db.rows("SELECT COUNT(*) AS n FROM ledger_audit WHERE kind='snapshot_conflict'")[0]["n"],
                             "fallbacks": db.rows("SELECT COUNT(*) AS n FROM ledger_audit WHERE kind=?", (ledger.FALLBACK_KIND,))[0]["n"],
                             "recent": db.rows("SELECT at, forecast_id, kind, detail FROM ledger_audit ORDER BY id DESC LIMIT 5")},
            "promotion_rule": learning.PROMOTION_RULE,
            "totals": {"issued": db.rows("SELECT COUNT(*) AS n FROM forecasts")[0]["n"], "matured": db.rows("SELECT COUNT(*) AS n FROM outcomes")[0]["n"]}}
