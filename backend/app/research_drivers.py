"""Do market-driver inputs improve gold forecasts? A controlled, point-in-time walk-forward comparison.

Compared on IDENTICAL forecast origins and outcomes:
  baseline      unchanged price
  gold_only     the app's existing 12 gold price/volatility features
  gold+drivers  gold features + dollar, yields, real yield, equities, VIX joined by *availability time*
  leaky_control gold + drivers joined by same-day observation (NOT knowable at the 13:30 settlement) - shows how much an
                availability-blind join would have flattered the result. Never used in production.
Method: expanding-window walk-forward, refit every ``REFIT`` origins, targets that overlap the forecast block purged from
training, a chronologically separate calibration slice (never the fitting slice) for the 80% range, preprocessing fitted
inside each training fold only. Uncertainty: moving-block bootstrap of the loss difference (block >= horizon) because
adjacent multi-session outcomes overlap and are not independent.
"""
from __future__ import annotations

import json
import time
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import config, drivers
from .feature_groups import all_candidate_features
from .features import FEATURE_NAMES, build_features, build_target

REFIT = 21
OOS_START_FRAC = 0.5
CALIB_FRAC = 0.2
LEVEL = 0.80
ALGOS = {
    "ridge": lambda: Pipeline([("s", StandardScaler()), ("r", Ridge(alpha=300.0))]),
    "hgb": lambda: HistGradientBoostingRegressor(max_depth=3, learning_rate=0.03, max_iter=150, min_samples_leaf=30, l2_regularization=5.0, random_state=0),
    "random_forest": lambda: RandomForestRegressor(n_estimators=150, max_depth=4, min_samples_leaf=20, max_features=0.5, random_state=0, n_jobs=-1),
}


def build_dataset(close: pd.Series, h: int, df_drivers: pd.DataFrame | None) -> dict:
    """Aligned samples. Origin = the 13:30 ET settlement of each session; targets are future settlements.

    With ``df_drivers=None`` the dataset has the gold-only columns (no ``d_``/``leak_`` driver columns): the fallback that lets
    the gold-only candidates forecast on a day the driver series could not be downloaded."""
    feats = build_features(close)
    ret = np.log(close).diff()
    if df_drivers is None:
        X = pd.concat([feats, all_candidate_features(close)], axis=1)
    else:
        origins = pd.Series([pd.Timestamp(d).tz_localize(drivers.NY) + pd.Timedelta(hours=13, minutes=30) for d in close.index], index=close.index)
        d_ok = drivers.asof_features(origins, df_drivers)
        d_leak = drivers.asof_features(origins, df_drivers, same_day_control=True)
        dcols = drivers.ALL_DRIVER_FEATURES
        X = pd.concat([feats, d_ok[dcols].add_prefix("d_"), d_leak[dcols].add_prefix("leak_"), all_candidate_features(close)], axis=1)
    X["vol60"] = ret.rolling(60).std()
    y = build_target(close, h)
    x_now = X.iloc[-1]  # the latest origin: its inputs are known, its target is not (never used for training)
    keep = X.notna().all(axis=1) & y.notna()  # only MATURED labels train a model
    X, y = X[keep], y[keep]
    pos = close.index.get_indexer(X.index)
    return {"x_now": x_now if x_now.notna().all() else None, "now_date": close.index[-1], "X": X, "y": y.to_numpy(), "pos": pos, "p0": close.reindex(X.index).to_numpy(),
            "ph": close.shift(-h).reindex(X.index).to_numpy(), "vol60": X.pop("vol60").to_numpy(), "dates": X.index}


def walk_forward(ds: dict, cols: list[str], algo: str, h: int) -> dict:
    X, y, pos, vol = ds["X"][cols].to_numpy(), ds["y"], ds["pos"], ds["vol60"] * np.sqrt(h)
    n = len(y)
    start = int(n * OOS_START_FRAC)
    pred = np.full(n, np.nan)
    q_lo = np.full(n, np.nan)
    q_hi = np.full(n, np.nan)
    for i in range(start, n, REFIT):
        j = min(i + REFIT, n)
        usable = np.where(pos + h < pos[i])[0]  # targets must end before this block's first origin
        if len(usable) < 200:
            continue
        cut = int(len(usable) * (1 - CALIB_FRAC))
        fit_idx = usable[:cut]
        cal_idx = usable[cut:]
        cal_idx = cal_idx[pos[cal_idx] > pos[fit_idx[-1]] + h]  # purge fit/calibration overlap
        if len(cal_idx) < 30:
            continue
        m = ALGOS[algo]().fit(X[fit_idx], y[fit_idx])
        z = (y[cal_idx] - m.predict(X[cal_idx])) / vol[cal_idx]
        lo, hi = np.quantile(z, (1 - LEVEL) / 2), np.quantile(z, 1 - (1 - LEVEL) / 2)
        pred[i:j], q_lo[i:j], q_hi[i:j] = m.predict(X[i:j]), lo, hi
    ok = ~np.isnan(pred)
    return {"ok": ok, "pred": pred, "lo": pred + q_lo * vol, "hi": pred + q_hi * vol}


def _block_bootstrap_ci(diff: np.ndarray, block: int, reps: int = 2000, seed: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = len(diff)
    block = max(1, min(block, n))
    nb = int(np.ceil(n / block))
    starts = np.arange(n - block + 1)
    means = np.empty(reps)
    for r in range(reps):
        idx = rng.choice(starts, nb)
        means[r] = np.concatenate([diff[s:s + block] for s in idx])[:n].mean()
    return float(np.quantile(means, 0.05)), float(np.quantile(means, 0.95))


def boot_p_value(diff: np.ndarray, block: int, reps: int = 2000, seed: int = 0) -> float:
    """One-sided: share of block-bootstrap means of (model loss - baseline loss) that are >= 0. Small = model reliably better."""
    rng = np.random.default_rng(seed)
    n = len(diff)
    block = max(1, min(block, n))
    starts = np.arange(n - block + 1)
    nb = int(np.ceil(n / block))
    means = np.array([np.concatenate([diff[s:s + block] for s in rng.choice(starts, nb)])[:n].mean() for _ in range(reps)])
    return float(np.mean(means >= 0))


def score(ds: dict, wf: dict, h: int, mask: np.ndarray) -> dict:
    p0, ph, y = ds["p0"][mask], ds["ph"][mask], ds["y"][mask]
    pred = wf["pred"][mask]
    price_pred = p0 * np.exp(pred)
    loss_m, loss_b = np.abs(price_pred - ph), np.abs(p0 - ph)
    lo, hi = wf["lo"][mask], wf["hi"][mask]
    moved = y != 0
    thirds = np.array_split(np.arange(mask.sum()), 3)
    diff = loss_m - loss_b
    ci_lo, ci_hi = _block_bootstrap_ci(diff, block=max(h, 10))
    return {
        "n": int(mask.sum()), "mae": float(loss_m.mean()), "baseline_mae": float(loss_b.mean()),
        "mae_vs_baseline_pct": float((loss_m.mean() / loss_b.mean() - 1) * 100),
        "mae_diff_ci90_pct": [float(ci_lo / loss_b.mean() * 100), float(ci_hi / loss_b.mean() * 100)],
        "direction_pct": float(np.mean(np.sign(pred[moved]) == np.sign(y[moved])) * 100),
        "coverage_pct": float(np.mean((y >= lo) & (y <= hi)) * 100),
        "mean_width_pct": float(np.mean(hi - lo) * 100),
        "by_period_vs_baseline_pct": [float((loss_m[t].mean() / loss_b[t].mean() - 1) * 100) for t in thirds],
    }


def run(close: pd.Series, horizons=(1, 5, 20), df_drivers: pd.DataFrame | None = None) -> dict:
    warnings.filterwarnings("ignore")
    started = time.time()
    df_drivers = df_drivers if df_drivers is not None else drivers.load(force=True)
    gold_cols = list(FEATURE_NAMES)
    d_cols = ["d_" + c for c in drivers.ALL_DRIVER_FEATURES]
    leak_cols = ["leak_" + c for c in drivers.ALL_DRIVER_FEATURES]
    sets = {"gold_only": gold_cols, "gold+all_drivers": gold_cols + d_cols, "leaky_control(same-day)": gold_cols + leak_cols}
    group_sets = {g: gold_cols + ["d_" + c for c in cols] for g, cols in drivers.GROUPS.items()}
    out = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "data_through": close.index[-1].date().isoformat(),
           "driver_series": {k: v[5] for k, v in drivers.SERIES.items()}, "driver_errors": df_drivers.attrs.get("errors", {}),
           "method": __doc__.strip().split("\n\n")[1] if "\n\n" in __doc__ else "", "horizons": {}}
    for h in horizons:
        ds = build_dataset(close, h, df_drivers)
        runs = {}
        for algo in ALGOS:
            for name, cols in sets.items():
                runs[(algo, name)] = walk_forward(ds, cols, algo, h)
        for algo in ("ridge", "hgb"):
            for g, cols in group_sets.items():
                runs[(algo, f"gold+{g}")] = walk_forward(ds, cols, algo, h)
        common = np.logical_and.reduce([r["ok"] for r in runs.values()])  # identical origins for every comparison
        def loss(wf):
            return np.abs(ds["p0"][common] * np.exp(wf["pred"][common]) - ds["ph"][common])
        base_mae = float(np.abs(ds["p0"][common] - ds["ph"][common]).mean())
        paired = {}
        for algo in ALGOS:  # same origins, same outcomes: does adding drivers change the loss by more than noise?
            d = loss(runs[(algo, "gold+all_drivers")]) - loss(runs[(algo, "gold_only")])
            lo, hi = _block_bootstrap_ci(d, block=max(h, 10))
            paired[algo] = {"delta_pct_of_baseline_mae": float(d.mean() / base_mae * 100), "ci90": [float(lo / base_mae * 100), float(hi / base_mae * 100)]}
        res = {"paired_drivers_vs_gold_only": paired, "n_origins": int(common.sum()), "oos_start": ds["dates"][np.where(common)[0][0]].date().isoformat(),
               "oos_end": ds["dates"][np.where(common)[0][-1]].date().isoformat(), "n_samples_total": int(len(ds["y"])), "results": []}
        for (algo, name), wf in runs.items():
            res["results"].append({"algorithm": algo, "features": name, **score(ds, wf, h, common)})
        out["horizons"][str(h)] = res
    out["duration_s"] = round(time.time() - started, 1)
    out["conclusion"] = conclude(out)
    return out


def conclude(out: dict) -> dict:
    """Plain-language verdict from the numbers.

    A driver "improvement" counts only if the paired block-bootstrap 90% interval of (drivers - gold_only) loss excludes
    zero. "Beats the market baseline" counts only if the model-vs-unchanged interval is entirely below zero.
    """
    verdicts = {}
    for h, res in out["horizons"].items():
        paired = res["paired_drivers_vs_gold_only"]
        helped = [a for a, p in paired.items() if p["ci90"][1] < 0]
        hurt = [a for a, p in paired.items() if p["ci90"][0] > 0]
        beating = sorted({f"{r['algorithm']}/{r['features']}" for r in res["results"]
                          if r["features"] != "leaky_control(same-day)" and r["mae_diff_ci90_pct"][1] < 0})
        verdicts[h] = {
            "drivers_help_significantly": helped, "drivers_hurt_significantly": hurt, "beats_unchanged_price": beating,
            "verdict": ("Drivers gave a statistically reliable improvement for: " + ", ".join(helped) if helped else
                        "Drivers did not give a statistically reliable improvement for any algorithm")
                       + (". Models that beat the unchanged price with 90% confidence: " + ", ".join(beating) if beating
                          else ". No model beat the unchanged price with statistical confidence."),
        }
    return verdicts


def save(result: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.DATA_DIR / "research_drivers.tmp"
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(config.DATA_DIR / "research_drivers.json")


def latest() -> dict | None:
    try:
        return json.loads((config.DATA_DIR / "research_drivers.json").read_text())
    except Exception:  # noqa: BLE001
        return None
