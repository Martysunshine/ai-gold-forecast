"""Continuous learning loop:

  collect -> features -> issue forecasts -> wait for outcomes -> score matured forecasts -> train/evaluate challengers
  -> retain or promote -> repeat

Rules that keep the loop honest (see PROMOTION_RULE for the user-facing text):
  * Only MATURED labels train anything (a 20-session forecast is not a training example until its target session exists).
  * Forecasts are written to an append-only ledger before their outcomes exist and are never edited.
  * There is no "untouched test period" reused day after day. Walk-forward results are DEVELOPMENT evidence. The only
    untouched evidence is forward shadow forecasts, judged after their targets mature.
  * A challenger needs development evidence AND forward evidence, corrected for the number of challengers compared.
  * Insufficient evidence retains the incumbent. Any champion can be rolled back, automatically or by hand.
Live price ticks never train anything.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import db, drivers, ledger, nowcast, registry
from . import research_drivers as rd
from .feature_groups import FIB_FEATURES, FIB_LEVELS, FIB_WINDOWS, PATTERN_FEATURES
from .features import FEATURE_NAMES
from .model import target_date as session_target_date

NY = "America/New_York"
LEVEL = 0.80
MIN_IMPROVEMENT = 0.02          # candidate must cut MAE by >= 2% vs the unchanged price AND vs the incumbent (development)
ALPHA = 0.10                    # one-sided, Holm-adjusted across the candidates compared for a task
COVERAGE_TOL = 8.0              # 80% range must really cover 72-88% of development outcomes
WIDTH_TOL = 1.10                # ...without being >10% wider than the incumbent's
MIN_FORWARD_BLOCKS = 30         # effective forward blocks (sessions / horizon) before a forward verdict is allowed
FORWARD_MIN_GAIN = 0.01         # forward MAE must beat the unchanged price by >= 1%
COOLDOWN_DAYS = 30
ROLLBACK_WORSE_PCT = 5.0        # auto-rollback if a promoted champion trails the unchanged price by >= 5% ...
ROLLBACK_MIN_BLOCKS = 20        # ...over at least this many forward blocks

PROMOTION_RULE = [
    "Only matured outcomes are ever used: a forecast becomes a training example only after its target session's official close exists.",
    "Every forecast is written to an append-only ledger (model version, feature schema, data cutoff, base price, range) before its outcome is known, and is never edited.",
    f"Gate 1 (development, walk-forward): a challenger must cut mean absolute error by at least {MIN_IMPROVEMENT:.0%} versus BOTH the unchanged price and the incumbent, in at least 2 of 3 chronological periods, with a one-sided block-bootstrap p-value below {ALPHA} after Holm correction for the number of challengers compared, and a range whose real coverage is within {COVERAGE_TOL:.0f} points of 80% and not more than 10% wider than the incumbent's.",
    f"Gate 2 (forward shadow): at least {MIN_FORWARD_BLOCKS} effective forward blocks (matured sessions divided by the horizon, because adjacent multi-session forecasts overlap), a forward MAE at least {FORWARD_MIN_GAIN:.0%} below the unchanged price and no worse than the incumbent, and a block-bootstrap 90% interval entirely below the unchanged price.",
    f"Promotion needs both gates and at most one promotion per task per {COOLDOWN_DAYS} days. Anything less retains the incumbent.",
    f"A promoted champion is rolled back automatically if, over at least {ROLLBACK_MIN_BLOCKS} forward blocks, it trails the unchanged price by {ROLLBACK_WORSE_PCT:.0f}% or more. Manual rollback is always available.",
    "Walk-forward numbers are development evidence, not an untouched test: they are re-examined every day, so only the forward record counts as out-of-sample proof.",
]


def make_intraday_model(name: str):
    if name == "ridge":
        return Pipeline([("s", StandardScaler()), ("r", Ridge(alpha=100.0))])
    if name == "hgb":
        return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.04, max_iter=150, min_samples_leaf=40, l2_regularization=5.0, random_state=0)
    if name == "extra_trees":
        return ExtraTreesRegressor(n_estimators=150, max_depth=5, min_samples_leaf=40, max_features=0.6, random_state=0, n_jobs=-1)
    raise ValueError(name)


def cols_for(schema: str) -> list[str]:
    if schema == "gold_v1":
        return list(FEATURE_NAMES)
    if schema == "drivers_v1":
        return list(FEATURE_NAMES) + ["d_" + c for c in drivers.ALL_DRIVER_FEATURES]
    if schema == "patterns_v1":
        return list(FEATURE_NAMES) + list(PATTERN_FEATURES)
    if schema == "fib_v1":
        return list(FEATURE_NAMES) + list(PATTERN_FEATURES) + list(FIB_FEATURES)
    raise ValueError(schema)


TARGET_COVERAGE_PCT = 80.0
UNDER_CALIBRATED_MARGIN = 8.0        # "materially below target": more than 8 points under
LITTLE_CHANGE_PCT_PER_SQRT_SESSION = 0.10  # display rule only: |change| < 0.10% x sqrt(horizon in sessions) is shown as "little change"


def calibration_label(historical_pct: float | None, target: float = TARGET_COVERAGE_PCT) -> str | None:
    """'Under-calibrated' when the measured historical coverage is materially (> 8 points) below the claimed coverage."""
    if historical_pct is None:
        return None
    return "Under-calibrated" if historical_pct < target - UNDER_CALIBRATED_MARGIN else None


def little_change(change_pct: float, horizon_sessions: float = 1.0) -> tuple[bool, float]:
    """Display threshold for 'Little change predicted'. A presentation rule only: it is not a confidence level or an edge."""
    thr = LITTLE_CHANGE_PCT_PER_SQRT_SESSION * float(np.sqrt(max(horizon_sessions, 1.0)))
    return abs(change_pct) < thr, thr


def calibration_summary(task: str, ref_price: float | None = None) -> dict:
    """Range calibration for a task's champion, from chronological evidence only (walk-forward, then matured live outcomes)."""
    champ = registry.champion(task)["version"]
    dev = db.rows("SELECT metrics, data_through FROM dev_evals WHERE task=? AND version=? ORDER BY data_through DESC LIMIT 1", (task, champ))
    m = json.loads(dev[0]["metrics"]) if dev else {}
    fwd = ledger.stats(task, version=champ)
    hist = m.get("coverage_pct")
    out = {"target_pct": TARGET_COVERAGE_PCT, "historical_pct": hist, "historical_basis": "walk-forward: each range calibrated only on earlier data",
           "n_dates": m.get("n_origins"), "by_period_pct": m.get("by_period_coverage_pct"), "ci90_pct": m.get("coverage_ci90_pct"),
           "mean_width_pct": m.get("mean_width_pct"), "label": calibration_label(hist),
           "forward_pct": fwd.get("coverage_pct"), "forward_n": fwd.get("n_matured", 0), "data_through": dev[0]["data_through"] if dev else None}
    if ref_price and out["mean_width_pct"] is not None:
        out["mean_width_usd"] = float(ref_price * out["mean_width_pct"] / 100)
    return out


def _origin_epoch(d: pd.Timestamp) -> float:
    return (pd.Timestamp(d).tz_localize(NY) + pd.Timedelta(hours=13, minutes=30)).timestamp()


def _target_time(date_iso: str) -> str:
    return (pd.Timestamp(date_iso).tz_localize(NY) + pd.Timedelta(hours=13, minutes=30)).isoformat()


# --------------------------------------------------------------------------- #
# Issue forward forecasts (champion + shadows + baseline) for the latest completed session
# --------------------------------------------------------------------------- #
def fit_forecast(ds: dict, cols: list[str], algo: str, h: int) -> tuple[float, float, float]:
    """Log-return forecast and 80% range for the latest origin, using matured labels only.

    Range quantiles come from a model fitted on the first 80% and scored on the purged last 20%; the deployed point
    forecast is refitted on all matured labels."""
    X, y, pos = ds["X"][cols].to_numpy(), ds["y"], ds["pos"]
    vol = ds["vol60"] * np.sqrt(h)
    n = len(y)
    cut = int(n * (1 - rd.CALIB_FRAC))
    cal = np.arange(cut, n)
    cal = cal[pos[cal] > pos[cut - 1] + h]
    scale_now = float(ds["x_now"]["vol60"]) * np.sqrt(h)
    if algo == "unchanged":
        z = y[cal] / vol[cal]
        mu = 0.0
    else:
        z = (y[cal] - rd.ALGOS[algo]().fit(X[:cut], y[:cut]).predict(X[cal])) / vol[cal]
        mu = float(rd.ALGOS[algo]().fit(X, y).predict(ds["x_now"][cols].to_numpy(dtype=float).reshape(1, -1))[0])
    lo, hi = np.quantile(z, (1 - LEVEL) / 2), np.quantile(z, 1 - (1 - LEVEL) / 2)
    return mu, mu + float(lo) * scale_now, mu + float(hi) * scale_now


def log_attempt(task: str, version: str, stage: str, status: str, data_through: str | None, started: float, detail: str | None = None) -> None:
    """Every candidate attempt is kept, including failures. This table is append-only and is never used to hide a bad result."""
    db.connect().execute("INSERT INTO training_attempts (at,task,version,stage,status,data_through,duration_s,detail) VALUES (?,?,?,?,?,?,?,?)",
                         (time.time(), task, version, stage, status, data_through, round(time.time() - started, 2), (detail or "")[:400] or None))


def issue_forward(close: pd.Series, drivers_df: pd.DataFrame | None, data_key: str, now: float | None = None) -> dict:
    now = now or time.time()
    base_date = close.index[-1]
    base_price = float(close.iloc[-1])
    dhash = ledger.dataset_hash(close, drivers_df)
    through = base_date.date().isoformat()
    out = {"issued": 0, "skipped": [], "dataset_hash": dhash}
    for h in (1, 5, 20):
        task = f"fwd_{h}"
        tdate = session_target_date(base_date, h)
        if pd.Timestamp(tdate) <= base_date:
            out["skipped"].append(f"{task}: target not in the future")
            continue
        ds, gold_only = None, False
        try:
            if drivers_df is not None:
                try:
                    ds = rd.build_dataset(close, h, drivers_df)
                except (KeyError, ValueError):   # a driver series is missing altogether (e.g. FRED was down at the download)
                    ds = None
            if ds is None or ds["x_now"] is None:
                # The driver series are missing or incomplete for the latest session. The gold-only candidates (the seeded
                # champions among them) need none of them, so they still forecast; the driver candidates sit this session out.
                ds, gold_only = rd.build_dataset(close, h, None), True
        except Exception as exc:  # noqa: BLE001
            out["skipped"].append(f"{task}: dataset failed ({exc.__class__.__name__})")
            continue
        if ds["x_now"] is None:
            out["skipped"].append(f"{task}: latest inputs incomplete")
            continue
        if gold_only:
            out.setdefault("gold_only", []).append(task)
        champ = registry.champion(task)["version"]
        for v in registry.versions(task):
            algo, schema = v["algorithm"], v["feature_schema"]
            t0 = time.time()
            if gold_only and algo != "unchanged" and any(c not in ds["x_now"].index for c in cols_for(schema)):
                out["skipped"].append(f"{v['version']}: driver data unavailable")
                continue
            try:
                cols = cols_for(schema) if algo != "unchanged" else []
                if cols and not ds["x_now"][cols].notna().all():
                    raise ValueError("latest inputs incomplete for this feature group")
                mu, lo, hi = fit_forecast(ds, cols, algo, h)
            except Exception as exc:  # noqa: BLE001 - one failing candidate must not block the others
                out["skipped"].append(f"{v['version']}: {exc.__class__.__name__}")
                log_attempt(task, v["version"], "forecast_fit", "failed", through, t0, f"{exc.__class__.__name__}: {exc}")
                continue
            log_attempt(task, v["version"], "forecast_fit", "ok", through, t0)
            role = "champion" if v["version"] == champ else ("baseline" if algo == "unchanged" else "shadow")
            if ledger.issue(task=task, role=role, version=v["version"], algorithm=algo, feature_schema=schema, input_cutoff=_origin_epoch(base_date),
                            base_price=base_price, base_time=base_date.date().isoformat(), target_date=tdate, target_time=_target_time(tdate),
                            predicted=base_price * float(np.exp(mu)), low=base_price * float(np.exp(lo)), high=base_price * float(np.exp(hi)),
                            level=LEVEL, horizon=h, data_key=data_key, issued_at=now, dataset_hash=dhash):
                out["issued"] += 1
            registry.mark_trained(v["version"], base_date.date().isoformat())
    return out


# --------------------------------------------------------------------------- #
# Development evaluation (walk-forward) and the promotion decision
# --------------------------------------------------------------------------- #
def _thirds(loss_m, loss_b):
    return [float(loss_m[t].mean() / loss_b[t].mean() - 1) * 100 for t in np.array_split(np.arange(len(loss_m)), 3)]


FIB_MIN_ORIGINS = 300          # a horizon with fewer identical origins than this cannot support or reject a feature group
FIB_MIN_HORIZONS = 2           # the same algorithm must beat the no-Fibonacci version, with its 90% interval entirely below zero, at this many horizons
SERIES_KEEP = 160              # origins of walk-forward predicted-vs-actual kept for the Learning page (development evidence, shown as such)


def _paired(ds: dict, runs: dict, a: str, b: str, common: np.ndarray, h: int) -> dict | None:
    """Loss of ``a`` minus loss of ``b`` on identical origins and outcomes, as a share of the unchanged-price error, with a block-bootstrap 90% interval."""
    if a not in runs or b not in runs:
        return None
    p0, ph = ds["p0"][common], ds["ph"][common]
    la = np.abs(p0 * np.exp(runs[a]["pred"][common]) - ph)
    lb = np.abs(p0 * np.exp(runs[b]["pred"][common]) - ph)
    base = float(np.abs(p0 - ph).mean())
    d = la - lb
    lo, hi = rd._block_bootstrap_ci(d, block=max(h, 10), reps=1000)
    return {"a": a, "b": b, "n_origins": int(common.sum()), "delta_pct_of_baseline_mae": float(d.mean() / base * 100), "ci90": [float(lo / base * 100), float(hi / base * 100)],
            "a_vs_unchanged_pct": float((la.mean() / base - 1) * 100), "b_vs_unchanged_pct": float((lb.mean() / base - 1) * 100)}


def dev_evaluate_forward(close: pd.Series, drivers_df: pd.DataFrame, data_through: str) -> dict[str, dict]:
    """Walk-forward development metrics for every forward candidate, on identical origins per horizon.

    A candidate that fails is logged (``training_attempts``) and skipped; the others are unaffected. The special key ``_feature_eval``
    carries the paired with/without comparisons used by ``fib_verdict`` (it is removed by ``run_learning`` before the promotion gates)."""
    results: dict[str, dict] = {}
    feature_eval: dict[str, dict] = {}
    for h in (1, 5, 20):
        task = f"fwd_{h}"
        ds = rd.build_dataset(close, h, drivers_df)
        runs = {}
        for v in registry.versions(task):
            if v["algorithm"] == "unchanged":
                continue
            t0 = time.time()
            try:
                runs[v["version"]] = rd.walk_forward(ds, cols_for(v["feature_schema"]), v["algorithm"], h)
            except Exception as exc:  # noqa: BLE001 - recorded, and the other candidates still get evaluated
                log_attempt(task, v["version"], "dev_eval", "failed", data_through, t0, f"{exc.__class__.__name__}: {exc}")
                continue
            log_attempt(task, v["version"], "dev_eval", "ok", data_through, t0)
        if not runs:
            continue
        common = np.logical_and.reduce([r["ok"] for r in runs.values()])
        p0, ph, y = ds["p0"][common], ds["ph"][common], ds["y"][common]
        loss_b = np.abs(p0 - ph)
        champ = registry.champion(task)["version"]
        for version, wf in runs.items():
            loss_m = np.abs(p0 * np.exp(wf["pred"][common]) - ph)
            lo, hi = wf["lo"][common], wf["hi"][common]
            hits = ((y >= lo) & (y <= hi)).astype(float)
            cov_lo, cov_hi = rd._block_bootstrap_ci(hits, block=max(h, 10), reps=1000)  # overlapping targets: resample blocks, not points
            results[version] = {
                "by_period_coverage_pct": [float(hits[t].mean() * 100) for t in np.array_split(np.arange(len(hits)), 3)],
                "coverage_ci90_pct": [float(cov_lo * 100), float(cov_hi * 100)], "horizon": h,
                "task": task, "n_origins": int(common.sum()), "mae": float(loss_m.mean()), "baseline_mae": float(loss_b.mean()),
                "mae_vs_baseline_pct": float((loss_m.mean() / loss_b.mean() - 1) * 100),
                "p_value_vs_baseline": rd.boot_p_value(loss_m - loss_b, max(h, 10)),
                "by_period_vs_baseline_pct": _thirds(loss_m, loss_b),
                "coverage_pct": float(np.mean((y >= lo) & (y <= hi)) * 100), "mean_width_pct": float(np.mean(hi - lo) * 100),
                "direction_pct": float(np.mean(np.sign(wf["pred"][common][y != 0]) == np.sign(y[y != 0])) * 100),
                "loss": loss_m.tolist(),  # kept in memory for incumbent comparison; stripped before storing
            }
            if version == champ:
                k = slice(-SERIES_KEEP, None)
                dates = [d.date().isoformat() for d in ds["dates"][common]][k]
                results[version]["series"] = {"dates": dates, "base": [round(float(x), 2) for x in p0[k]], "actual": [round(float(x), 2) for x in ph[k]],
                                              "pred": [round(float(x), 2) for x in (p0 * np.exp(wf["pred"][common]))[k]],
                                              "low": [round(float(x), 2) for x in (p0 * np.exp(lo))[k]], "high": [round(float(x), 2) for x in (p0 * np.exp(hi))[k]]}
        for algo in registry.PATTERN_ALGOS:     # identical models, identical origins and outcomes: with vs without each addition
            g, p_, f_ = f"fwd_{h}:{algo}:gold_v1", f"fwd_{h}:{algo}:patterns_v1", f"fwd_{h}:{algo}:fib_v1"
            for name, (x, y_) in {"fib_vs_patterns": (f_, p_), "patterns_vs_gold": (p_, g), "fib_vs_gold": (f_, g)}.items():
                t0 = time.time()
                try:
                    r = _paired(ds, runs, x, y_, common, h)
                except Exception as exc:  # noqa: BLE001
                    log_attempt(task, x, "feature_group", "failed", data_through, t0, f"{name}: {exc.__class__.__name__}: {exc}")
                    continue
                if r:
                    feature_eval.setdefault(name, {}).setdefault(str(h), {})[algo] = r
    results["_feature_eval"] = feature_eval  # type: ignore[assignment]
    return results


def fib_verdict(feature_eval: dict) -> dict:
    """The pre-set rule for keeping the EXPERIMENTAL Fibonacci group. Fixed before looking at any result; not tuned afterwards.

    supported      the same algorithm beats the identical model without Fibonacci, with the paired 90% interval entirely below zero, at
                   >= 2 horizons, AND at no horizon is any algorithm reliably WORSE with it.
    not_supported  enough origins were compared and that is not the case: the fib candidates are rejected (history kept, no new forecasts).
    insufficient   too few identical origins at too many horizons to decide either way: nothing changes."""
    comp = (feature_eval or {}).get("fib_vs_patterns") or {}
    rows = [(h, algo, r) for h, per in comp.items() for algo, r in per.items() if r["n_origins"] >= FIB_MIN_ORIGINS]
    if len({h for h, _, _ in rows}) < FIB_MIN_HORIZONS:
        return {"status": "insufficient", "reason": f"fewer than {FIB_MIN_HORIZONS} horizons had {FIB_MIN_ORIGINS}+ identical origins", "rule": fib_rule_text()}
    wins = {}
    for h, algo, r in rows:
        if r["ci90"][1] < 0:
            wins.setdefault(algo, []).append(h)
    worse = [(h, algo) for h, algo, r in rows if r["ci90"][0] > 0]
    ok = any(len(hs) >= FIB_MIN_HORIZONS for hs in wins.values()) and not worse
    reason = ("The Fibonacci group reliably improved the same model at " + ", ".join(f"{a}: horizons {hs}" for a, hs in wins.items() if len(hs) >= FIB_MIN_HORIZONS) + "."
              if ok else "No algorithm beat its no-Fibonacci twin with a 90% interval entirely below zero at "
                         f"{FIB_MIN_HORIZONS}+ horizons" + (f"; reliably worse with it at {worse}." if worse else "."))
    return {"status": "supported" if ok else "not_supported", "reason": reason, "wins": wins, "worse": worse, "rule": fib_rule_text()}


def fib_rule_text() -> str:
    return (f"Kept only if, for the same algorithm on identical origins, adding Fibonacci cuts the error with a paired 90% block-bootstrap interval entirely below zero "
            f"at {FIB_MIN_HORIZONS}+ of 3 horizons and never reliably worsens it. Windows {list(FIB_WINDOWS)} and levels {list(FIB_LEVELS)} were fixed in advance.")


def dev_evaluate_nowcast(daily: pd.Series, data_through: str) -> dict[str, dict]:
    rows = nowcast.training_rows(nowcast.build_frame(nowcast.fetch_hourly(), daily))
    sess = np.array(sorted(rows["session"].unique()))
    start = int(len(sess) * 0.5)
    X, y = rows[nowcast.FEATS].to_numpy(), rows["target"].to_numpy()
    p0, ph, s = rows["c"].to_numpy(), rows["session_close"].to_numpy(), rows["session"].to_numpy()
    oos = np.isin(s, sess[start:])
    out = {}
    for algo in registry.NOWCAST_ALGOS:
        pred = np.full(len(y), np.nan)
        for b in range(start, len(sess), 21):
            blk = sess[b:b + 21]
            tr, te = s < blk[0], np.isin(s, blk)
            if tr.sum() < 1000:
                continue
            pred[te] = make_intraday_model(algo).fit(X[tr], y[tr]).predict(X[te])
        ok = oos & ~np.isnan(pred)
        loss_m, loss_b = np.abs(p0[ok] * np.exp(pred[ok]) - ph[ok]), np.abs(p0[ok] - ph[ok])
        per = pd.DataFrame({"s": s[ok], "m": loss_m, "b": loss_b}).groupby("s").mean().sort_index()  # per-session: rows within a day are not independent
        out[registry.version_id("nowcast", algo, "intraday_v1")] = {
            "task": "nowcast", "n_origins": int(len(per)), "mae": float(per["m"].mean()), "baseline_mae": float(per["b"].mean()),
            "mae_vs_baseline_pct": float((per["m"].mean() / per["b"].mean() - 1) * 100),
            "p_value_vs_baseline": rd.boot_p_value((per["m"] - per["b"]).to_numpy(), 10),
            "by_period_vs_baseline_pct": _thirds(per["m"].to_numpy(), per["b"].to_numpy()),
            "coverage_pct": None, "mean_width_pct": None,
            "direction_pct": float(np.mean(np.sign(pred[ok][y[ok] != 0]) == np.sign(y[ok][y[ok] != 0])) * 100),
            "loss": per["m"].tolist()}
    return out


def holm_pass(pvals: dict[str, float], alpha: float = ALPHA) -> dict[str, bool]:
    """Holm-Bonferroni: which hypotheses survive correction for comparing len(pvals) candidates."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m, passed, still = len(items), {}, True
    for i, (k, p) in enumerate(items):
        still = still and p < alpha / (m - i)
        passed[k] = still
    return passed


def gate1(task: str, dev: dict[str, dict], champ_version: str) -> dict[str, dict]:
    cands = {v: m for v, m in dev.items() if m["task"] == task}
    survives = holm_pass({v: m["p_value_vs_baseline"] for v, m in cands.items()}) if cands else {}
    inc = cands.get(champ_version)
    verdicts = {}
    for v, m in cands.items():
        reasons = []
        if m["mae_vs_baseline_pct"] > -MIN_IMPROVEMENT * 100:
            reasons.append(f"MAE is {m['mae_vs_baseline_pct']:+.1f}% vs the unchanged price (needs <= -{MIN_IMPROVEMENT:.0%})")
        if inc is not None and v != champ_version and m["mae"] > (1 - MIN_IMPROVEMENT) * inc["mae"]:
            reasons.append(f"not at least {MIN_IMPROVEMENT:.0%} better than the incumbent ({m['mae']:.2f} vs {inc['mae']:.2f})")
        if sum(1 for x in m["by_period_vs_baseline_pct"] if x < 0) < 2:
            reasons.append("beats the unchanged price in fewer than 2 of 3 periods")
        if not survives.get(v, False):
            reasons.append(f"not statistically reliable after correcting for {len(cands)} candidates (p={m['p_value_vs_baseline']:.2f})")
        if m.get("coverage_pct") is not None:
            if abs(m["coverage_pct"] - 80) > COVERAGE_TOL:
                reasons.append(f"range covers {m['coverage_pct']:.0f}% instead of ~80%")
            if inc is not None and inc.get("mean_width_pct") and m["mean_width_pct"] > WIDTH_TOL * inc["mean_width_pct"]:
                reasons.append("range is much wider than the incumbent's")
        verdicts[v] = {"passes": not reasons, "reasons": reasons}
    return verdicts


def gate2(task: str, version: str, champ_version: str, h: int) -> dict:
    st = ledger.stats(task, version=version)
    blocks = ledger.effective_blocks(st.get("n_sessions", 0), h)
    base = {"blocks": round(blocks, 1), "needed": MIN_FORWARD_BLOCKS, "n_matured": st.get("n_matured", 0), "stats": st}
    if blocks < MIN_FORWARD_BLOCKS:
        return {**base, "passes": False, "reason": f"only {blocks:.1f} of {MIN_FORWARD_BLOCKS} effective forward blocks have matured"}
    inc = ledger.stats(task, version=champ_version) if version != champ_version else st
    reasons = []
    if st["mae_vs_baseline_pct"] > -FORWARD_MIN_GAIN * 100:
        reasons.append(f"forward MAE {st['mae_vs_baseline_pct']:+.1f}% vs unchanged price")
    if inc.get("mae") and st["mae"] > inc["mae"]:
        reasons.append("forward MAE worse than the incumbent")
    ci = st.get("mae_diff_ci90_pct")
    if not ci or ci[1] >= 0:
        reasons.append("forward 90% interval is not entirely below the unchanged price")
    return {**base, "passes": not reasons, "reason": "; ".join(reasons) or "forward evidence supports promotion"}


def decide(task: str, dev: dict[str, dict]) -> dict:
    """Apply the documented rule for one task. Returns the decision record (also written to the decision log)."""
    h = {"nowcast": 1, "fwd_1": 1, "fwd_5": 5, "fwd_20": 20}[task]
    champ = registry.champion(task)
    # Auto-rollback of a promoted champion that is failing forward.
    if champ["algorithm"] != "unchanged" and champ["notes"] != "seeded: the model already in production":
        st = ledger.stats(task, version=champ["version"], since_issued=champ["status_changed_at"])
        if ledger.effective_blocks(st.get("n_sessions", 0), h) >= ROLLBACK_MIN_BLOCKS and (st.get("mae_vs_baseline_pct") or 0) >= ROLLBACK_WORSE_PCT:
            to = registry.rollback(task, reason=f"automatic: forward MAE {st['mae_vs_baseline_pct']:+.1f}% vs the unchanged price over {st['n_sessions']} sessions")
            return {"task": task, "decision": "rollback", "champion": to}
    g1 = gate1(task, dev, champ["version"])
    last_promo = db.rows("SELECT MAX(at) AS at FROM decisions WHERE task=? AND decision='promote'", (task,))[0]["at"]
    cooling = bool(last_promo and time.time() - last_promo < COOLDOWN_DAYS * 86400)
    best, evidence = None, {}
    for v, verdict in g1.items():
        if v == champ["version"]:
            continue
        forward = gate2(task, v, champ["version"], h)
        evidence[v] = {"development": {"passes": verdict["passes"], "reasons": verdict["reasons"],
                                       "mae_vs_baseline_pct": round(dev[v]["mae_vs_baseline_pct"], 2), "p": round(dev[v]["p_value_vs_baseline"], 3)},
                       "forward": {k: forward[k] for k in ("passes", "reason", "blocks", "needed", "n_matured")}}
        if verdict["passes"] and forward["passes"] and not cooling and (best is None or dev[v]["mae"] < dev[best]["mae"]):
            best = v
    if best:
        registry.promote(task, best, "Passed development gate and forward shadow gate.", evidence[best])
        return {"task": task, "decision": "promote", "champion": best, "evidence": evidence}
    n_dev = sum(1 for e in evidence.values() if e["development"]["passes"])
    fwd_blocks = max([e["forward"]["blocks"] for e in evidence.values()] or [0])
    reason = (f"Retained {champ['version']}. {n_dev} of {len(evidence)} challengers passed the development gate; "
              f"the most forward evidence on any challenger is {fwd_blocks} of {MIN_FORWARD_BLOCKS} effective blocks."
              + (" Promotion is on cooldown." if cooling else ""))
    registry.log_decision(task, "evaluation", "retain", reason, champ["version"], champ["version"], evidence)
    return {"task": task, "decision": "retain", "champion": champ["version"], "reason": reason}


def _store_dev(data_through: str, dev: dict[str, dict]) -> None:
    now = time.time()
    with db.tx() as c:
        for v, m in dev.items():
            slim = {k: x for k, x in m.items() if k not in ("loss", "series")}
            c.execute("INSERT OR REPLACE INTO dev_evals VALUES (?,?,?,?,?)", (data_through, m["task"], v, now, json.dumps(slim)))
            if m.get("series"):
                c.execute("INSERT OR REPLACE INTO dev_series VALUES (?,?,?,?)", (data_through, m["task"], v, json.dumps(m["series"])))


def _apply_fib_verdict(data_through: str, feature_eval: dict) -> dict:
    """Record the paired comparisons and apply the pre-set Fibonacci rule (reject / restore / leave alone)."""
    verdict = fib_verdict(feature_eval)
    db.connect().execute("INSERT OR REPLACE INTO feature_evals VALUES (?,?,?,?)", (data_through, "fib_v1", time.time(), json.dumps({"verdict": verdict, "comparisons": feature_eval}, default=float)))
    if verdict["status"] == "not_supported":
        verdict["rejected"] = registry.reject_schema("fib_v1", "Fibonacci group not supported by the pre-set paired test: " + verdict["reason"], {"feature_eval": verdict})
    elif verdict["status"] == "supported":
        verdict["restored"] = registry.restore_schema("fib_v1")
    return verdict


def run_learning(close: pd.Series, drivers_df: pd.DataFrame | None, data_key: str, force: bool = False) -> dict:
    """score -> build features -> train/evaluate candidates -> record the decision -> publish forecasts, for fresh completed-session data.

    Failures leave the champion untouched, and publishing happens last and regardless, so a failed evaluation never leaves the app without a
    forecast for a new session (the forecasts then simply come from the unchanged champion)."""
    registry.seed()
    data_through = close.index[-1].date().isoformat()
    summary: dict = {"data_through": data_through, "matured": ledger.mature(close), "errors": {}}
    if force:
        db.connect().execute("DELETE FROM dev_evals WHERE data_through=?", (data_through,))  # dev_evals is a cache, not the ledger
    # "Done" means every live candidate was evaluated (or its attempt was recorded as failed) for this data: a candidate added later is not skipped.
    live = {v["version"] for t in registry.TASKS for v in registry.versions(t) if v["algorithm"] != "unchanged"}
    seen = {r["version"] for r in db.rows("SELECT version FROM dev_evals WHERE data_through=?", (data_through,))} | \
           {r["version"] for r in db.rows("SELECT version FROM training_attempts WHERE data_through=? AND stage='dev_eval'", (data_through,))}
    if live <= seen:
        summary["development"] = "already evaluated for this data"
    else:
        dev: dict[str, dict] = {}
        feature_eval: dict = {}
        for name, fn in (("forward", lambda: dev_evaluate_forward(close, drivers_df, data_through) if drivers_df is not None else {}),
                         ("nowcast", lambda: dev_evaluate_nowcast(close, data_through))):
            try:
                got = fn()
                feature_eval = got.pop("_feature_eval", feature_eval) or feature_eval
                dev.update(got)
            except Exception as exc:  # noqa: BLE001 - a failed evaluation must never disturb the champion
                summary["errors"][name] = f"{exc.__class__.__name__}: {exc}"
                log_attempt(name, "*", "dev_eval", "failed", data_through, time.time(), f"{exc.__class__.__name__}: {exc}")
        if dev:
            _store_dev(data_through, dev)
            if feature_eval:
                summary["fibonacci"] = _apply_fib_verdict(data_through, feature_eval)
            live = {v for t in registry.TASKS for v in (x["version"] for x in registry.versions(t))}
            gated = {v: m for v, m in dev.items() if v in live}        # rejected groups cannot be promoted or dilute the correction
            summary["decisions"] = [decide(t, gated) for t in registry.TASKS if any(m["task"] == t for m in gated.values())]
    try:
        summary["forward_issue"] = issue_forward(close, drivers_df, data_key)
    except Exception as exc:  # noqa: BLE001
        summary["errors"]["publish"] = f"{exc.__class__.__name__}: {exc}"
    return summary


# --------------------------------------------------------------------------- #
# Read side: what the "Model details" drawer shows
# --------------------------------------------------------------------------- #
def status_payload() -> dict:
    registry.seed()
    tasks = {}
    for task in registry.TASKS:
        h = {"nowcast": 1, "fwd_1": 1, "fwd_5": 5, "fwd_20": 20}[task]
        champ = registry.champion(task)
        dev = db.rows("SELECT metrics, data_through FROM dev_evals WHERE task=? AND version=? ORDER BY data_through DESC LIMIT 1", (task, champ["version"]))
        base_ver = registry.baseline_version(task)
        fwd = ledger.stats(task, version=champ["version"])
        fwd_base = ledger.stats(task, role="baseline") if task != "nowcast" else None
        last = db.rows("SELECT * FROM decisions WHERE task=? ORDER BY id DESC LIMIT 1", (task,))
        n_all = db.rows("SELECT COUNT(*) AS n FROM forecasts WHERE task=?", (task,))[0]["n"]
        tasks[task] = {
            "champion": {k: champ[k] for k in ("version", "algorithm", "feature_schema", "status_changed_at", "last_trained_at", "trained_through")},
            "baseline_version": base_ver, "forecasts_issued": n_all,
            "forward": {**fwd, "effective_blocks": round(ledger.effective_blocks(fwd.get("n_sessions", 0), h), 1), "needed_for_verdict": MIN_FORWARD_BLOCKS},
            "development": (lambda r: {**__import__("json").loads(r[0]["metrics"]), "data_through": r[0]["data_through"]} if r else None)(dev),
            "calibration": calibration_summary(task),
            "last_decision": ({k: last[0][k] for k in ("at", "decision", "reason", "champion_after")} if last else None),
            "challengers": [{"version": v["version"], "status": v["status"], "last_trained_at": v["last_trained_at"],
                             "forward": ledger.stats(task, version=v["version"])} for v in registry.versions(task) if v["status"] != "champion" and v["algorithm"] != "unchanged"],
        }
    return {"tasks": tasks, "rule": PROMOTION_RULE, "history": registry.decisions(30),
            "matured_total": db.rows("SELECT COUNT(*) AS n FROM outcomes")[0]["n"], "issued_total": db.rows("SELECT COUNT(*) AS n FROM forecasts")[0]["n"]}
