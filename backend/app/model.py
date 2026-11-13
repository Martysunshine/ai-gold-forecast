"""Gold price prediction: Ridge vs Random Forest vs an unchanged-price baseline.

Protocol (one chronological split per horizon, nothing fancy):

    |------- train (60%) -------|-- validation (20%) --|-- test (20%, untouched) --|

  * Both models are fitted on *train* and compared on *validation*. The one with the lower
    validation MAE becomes the default model. The test period plays no part in that choice.
  * The 80% range is built from the default model's *validation* residuals, expressed in units of recent
    volatility (20-session daily volatility x sqrt(horizon)) so it widens in turbulent markets and narrows in calm ones.
  * The test period is then scored once: models are refitted on everything before it and
    predict it. Test MAE, directional accuracy and the observed coverage of the range are reported.
  * Purging: a sample at date t has a target spanning (t, t+h]. A training/validation sample is
    only used for a split if t+h falls strictly before the first date of the next period, so no
    target window reaches into a later period.
  * Features at date t use prices up to t only.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import config
from .features import FEATURE_LABELS, FEATURE_NAMES, build_features, build_target
from .sessions import describe as calendar_description, next_sessions

TRAIN_FRAC, VAL_FRAC = 0.6, 0.2
INTERVAL_LEVEL = 0.80
MATERIAL_IMPROVEMENT = 0.02  # "advantage" needs a >=2% lower test MAE than the unchanged baseline
MIN_SAMPLES = 300

LABELS = {"ridge": "Ridge regression", "random_forest": "Random Forest", "unchanged": "Unchanged price"}


class ModelError(Exception):
    pass


def make_model(name: str):
    if name == "ridge":
        return Pipeline([("scale", StandardScaler()), ("ridge", Ridge(alpha=100.0))])
    if name == "random_forest":
        return RandomForestRegressor(n_estimators=200, max_depth=4, min_samples_leaf=20, max_features=0.5, random_state=0, n_jobs=-1)
    raise ValueError(name)


def _mae(pred_price: np.ndarray, actual: np.ndarray) -> float:
    return float(np.mean(np.abs(pred_price - actual)))


def _direction(pred_ret: np.ndarray, actual_ret: np.ndarray) -> float:
    moved = actual_ret != 0
    return float(np.mean(np.sign(pred_ret[moved]) == np.sign(actual_ret[moved])) * 100)


def target_date(last: pd.Timestamp, horizon: int) -> str:
    """Date of the ``horizon``-th daily bar after ``last`` (see sessions.py for the calendar rule)."""
    return next_sessions(last, horizon)[-1].date().isoformat()


def split_positions(pos: np.ndarray, h: int, n: int):
    """Indices (into the labelled-sample arrays) for train / validation / test, with purging.

    ``pos`` holds each sample's integer position in the full price series.
    """
    v0, t0 = int(n * TRAIN_FRAC), int(n * (TRAIN_FRAC + VAL_FRAC))
    train = np.where(pos + h < pos[v0])[0]  # target window ends before validation starts
    val = np.arange(v0, t0)
    val = val[pos[val] + h < pos[t0]]  # target window ends before test starts
    fit_before_test = np.where(pos + h < pos[t0])[0]  # train + validation, purged at the test boundary
    test = np.arange(t0, n)
    return train, val, test, fit_before_test


def run_prediction(close: pd.Series, h: int) -> dict:
    close = close.astype(float)
    if h not in config.HORIZONS:
        raise ModelError(f"Horizon must be one of {config.HORIZONS}.")
    feats = build_features(close)
    x_last = feats.iloc[-1]
    if x_last.isna().any():
        raise ModelError("Latest feature row is incomplete.")
    X = feats.dropna()
    y = build_target(close, h).reindex(X.index)
    keep = y.notna()
    X, y = X[keep], y[keep]
    n = len(X)
    if n < MIN_SAMPLES:
        raise ModelError(f"Not enough history: {n} usable samples, need at least {MIN_SAMPLES}.")
    pos = close.index.get_indexer(X.index)
    price0 = close.reindex(X.index).to_numpy()
    price_h = close.shift(-h).reindex(X.index).to_numpy()
    Xv, yv = X.to_numpy(), y.to_numpy()
    train, val, test, pre_test = split_positions(pos, h, n)
    if min(len(train), len(val), len(test)) < 30:
        raise ModelError("Not enough history to form train, validation and test periods.")

    def price_of(idx, pred_ret):
        return price0[idx] * np.exp(pred_ret)

    # ---- 1) validation: choose the default model (test period not touched) ----------------------
    val_pred, val_mae = {}, {}
    for name in ("ridge", "random_forest"):
        m = make_model(name).fit(Xv[train], yv[train])
        val_pred[name] = m.predict(Xv[val])
        val_mae[name] = _mae(price_of(val, val_pred[name]), price_h[val])
    val_mae["unchanged"] = _mae(price0[val], price_h[val])
    default = min(("ridge", "random_forest"), key=lambda k: val_mae[k])
    vol = X["vol_20"].to_numpy() * np.sqrt(h)  # expected size of an h-session move at each forecast origin
    z = (yv[val] - val_pred[default]) / vol[val]  # validation errors in units of recent volatility
    lo_q, hi_q = (1 - INTERVAL_LEVEL) / 2, 1 - (1 - INTERVAL_LEVEL) / 2
    q_low, q_high = float(np.quantile(z, lo_q)), float(np.quantile(z, hi_q))

    # ---- 2) test: score once --------------------------------------------------------------------
    test_pred = {}
    for name in ("ridge", "random_forest"):
        m = make_model(name).fit(Xv[pre_test], yv[pre_test])
        test_pred[name] = m.predict(Xv[test])
    actual_ret = yv[test]
    methods = {}
    for name in ("ridge", "random_forest"):
        methods[name] = {
            "label": LABELS[name],
            "mae": _mae(price_of(test, test_pred[name]), price_h[test]),
            "directional_accuracy": _direction(test_pred[name], actual_ret),
        }
    methods["unchanged"] = {"label": LABELS["unchanged"], "mae": _mae(price0[test], price_h[test]), "directional_accuracy": None}
    base_mae = methods["unchanged"]["mae"]
    for k in ("ridge", "random_forest"):
        methods[k]["vs_baseline_pct"] = float((methods[k]["mae"] / base_mae - 1) * 100)
    d_mae = methods[default]["mae"]
    advantage = d_mae <= (1 - MATERIAL_IMPROVEMENT) * base_mae
    tol = {}
    for t_pct in (1, 2, 5):
        row = {k: float(np.mean(np.abs(price_of(test, test_pred[k]) - price_h[test]) / price_h[test] <= t_pct / 100) * 100) for k in ("ridge", "random_forest")}
        row["unchanged"] = float(np.mean(np.abs(price0[test] - price_h[test]) / price_h[test] <= t_pct / 100) * 100)
        row["default"] = row[default]
        tol[str(t_pct)] = row
    lo_r, hi_r = test_pred[default] + q_low * vol[test], test_pred[default] + q_high * vol[test]
    coverage = float(np.mean((actual_ret >= lo_r) & (actual_ret <= hi_r)) * 100)
    width_usd = float(np.mean(price0[test] * (np.exp(hi_r) - np.exp(lo_r))))

    test_series = [
        {
            "date": close.index[pos[i] + h].date().isoformat(),
            "actual": round(float(price_h[i]), 2),
            "predicted": round(float(price0[i] * np.exp(test_pred[default][j])), 2),
            "baseline": round(float(price0[i]), 2),
        }
        for j, i in enumerate(test)
    ]

    def span(idx):
        return {"start": X.index[idx[0]].date().isoformat(), "end": X.index[idx[-1]].date().isoformat(), "n": int(len(idx))}

    # ---- 3) forecast: refit the default model on all labelled data -------------------------------
    final = make_model(default).fit(Xv, yv)
    pred = float(final.predict(x_last.to_numpy().reshape(1, -1))[0])
    last_date, last_price = close.index[-1], float(close.iloc[-1])
    f_price = last_price * float(np.exp(pred))
    vol_now = float(x_last["vol_20"]) * np.sqrt(h)
    low, high = last_price * float(np.exp(pred + q_low * vol_now)), last_price * float(np.exp(pred + q_high * vol_now))

    n_test = int(len(test))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizon": h,
        "base_date": last_date.date().isoformat(),
        "target_date": target_date(last_date, h),
        "base_price": last_price,
        "forecast": {
            "model": default,
            "model_label": LABELS[default],
            "price": f_price,
            "change_pct": (f_price / last_price - 1) * 100,
            "interval": {
                "level": INTERVAL_LEVEL,
                "low": low,
                "high": high,
                "low_pct": (low / last_price - 1) * 100,
                "high_pct": (high / last_price - 1) * 100,
                "observed_coverage_pct": coverage,
                "mean_width_usd_on_test": width_usd,
                "z_low": q_low,
                "z_high": q_high,
                "scale": "20-session daily volatility x sqrt(horizon), measured at each forecast origin",
                "calibrated_on": span(val),
                "evaluated_on": {"start": X.index[test[0]].date().isoformat(), "end": X.index[test[-1]].date().isoformat(), "n": int(len(test))},
                "built_from": "10th/90th percentiles of the default model's validation-period errors, expressed in units of recent volatility",
            },
        },
        "selection": {
            "default": default,
            "rule": "Lower mean absolute error (MAE) on the validation period, Ridge vs Random Forest. The test period is not used.",
            "validation_mae": val_mae,
        },
        "test": {
            "period": {"start": X.index[test[0]].date().isoformat(), "end": X.index[test[-1]].date().isoformat()},
            "n": n_test,
            "effective_n": round(n_test / h, 1),
            "methods": methods,
            "always_up_pct": float(np.mean(actual_ret > 0) * 100),
            "within_tolerance": tol,
            "advantage": bool(advantage),
            "verdict": "Beat the unchanged baseline on the test period" if advantage else "No demonstrated advantage over the unchanged-price baseline",
        },
        "training": {"closes": int(len(close)), "first_close": close.index[0].date().isoformat(), "last_close": close.index[-1].date().isoformat(),
                     "usable_samples": int(n), "n_features": len(FEATURE_NAMES), "final_fit_samples": int(n)},
        "calendar": calendar_description(),
        "splits": {"train": span(train), "validation": span(val), "test": span(test)},
        "test_series": test_series,
        "features": [{"name": k, "label": v} for k, v in FEATURE_LABELS.items()],
    }
