"""Ridge-regression forecaster with purged chronological walk-forward validation.

Approach (deliberately simple):
  * One Ridge model per horizon (1, 5, 20 sessions) predicting the forward *log return*.
  * Pipeline = StandardScaler -> Ridge. Inside every validation fold the scaler is fitted on
    the training rows only.
  * Walk-forward: expanding training window, consecutive test blocks, training rows whose
    target window reaches the first test date are purged.
  * Baseline: "price stays unchanged" (predicted return = 0).
  * Uncertainty: empirical 10th/90th percentiles of out-of-sample residuals (an 80% range).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import config
from .features import FEATURE_LABELS, FEATURE_NAMES, WARMUP, build_features, build_target

RIDGE_ALPHA = 100.0  # fixed a priori (strong shrinkage; weak-signal problem). Not tuned on test data.
TEST_BLOCK = 63  # ~one quarter of trading sessions per validation fold
MAX_MIN_TRAIN = 504  # ~two years of sessions before the first validation fold
INTERVAL_LEVEL = 0.80
MIN_FOLDS = 2
# Verdict thresholds (fixed in advance, documented in the README):
MATERIAL_IMPROVEMENT = 0.02  # model MAE must be >=2% lower than the baseline to count as "beats"


class ModelError(Exception):
    pass


def make_pipeline() -> Pipeline:
    return Pipeline([("scale", StandardScaler()), ("ridge", Ridge(alpha=RIDGE_ALPHA))])


def purged_walk_forward_splits(n: int, horizon: int, min_train: int, test_size: int = TEST_BLOCK):
    """Yield ``(train_idx, test_idx)`` positional index arrays over ``n`` time-ordered samples.

    Sample ``k`` has a target that spans positions ``(k, k + horizon]``. For a test block starting at
    ``a`` a training sample is kept only if ``k + horizon < a``, i.e. its whole target window ends
    before the first test date. Samples ``a - horizon .. a - 1`` are purged.
    """
    starts = []
    a = min_train
    while a < n:
        starts.append(a)
        a += test_size
    # A very short trailing block is merged into the previous one rather than evaluated alone.
    blocks = []
    for i, a in enumerate(starts):
        b = starts[i + 1] if i + 1 < len(starts) else n
        blocks.append((a, b))
    if len(blocks) >= 2 and blocks[-1][1] - blocks[-1][0] < test_size // 3:
        last = blocks.pop()
        blocks[-1] = (blocks[-1][0], last[1])
    for a, b in blocks:
        train_end = a - horizon  # exclusive: k <= a - horizon - 1
        if train_end <= 0:
            continue
        yield np.arange(0, train_end), np.arange(a, b)


@dataclass
class _Labelled:
    X: pd.DataFrame
    y: pd.Series
    price0: pd.Series
    price_h: pd.Series


def _labelled(close: pd.Series, horizon: int) -> _Labelled:
    X = build_features(close).dropna()
    y = build_target(close, horizon).reindex(X.index)
    keep = y.notna()
    X, y = X[keep], y[keep]
    price0 = close.reindex(X.index)
    price_h = close.shift(-horizon).reindex(X.index)
    return _Labelled(X, y, price0, price_h)


def walk_forward(close: pd.Series, horizon: int) -> pd.DataFrame:
    """Out-of-sample predictions for one horizon. One row per evaluated forecast origin."""
    data = _labelled(close, horizon)
    n = len(data.X)
    min_train = min(MAX_MIN_TRAIN, n // 2)
    rows = []
    for fold, (tr, te) in enumerate(purged_walk_forward_splits(n, horizon, min_train)):
        pipe = make_pipeline().fit(data.X.iloc[tr].to_numpy(), data.y.iloc[tr].to_numpy())
        pred = pipe.predict(data.X.iloc[te].to_numpy())
        rows.append(
            pd.DataFrame(
                {
                    "fold": fold,
                    "price0": data.price0.iloc[te].to_numpy(),
                    "price_actual": data.price_h.iloc[te].to_numpy(),
                    "ret_actual": data.y.iloc[te].to_numpy(),
                    "ret_pred": pred,
                    "train_end_pos": tr[-1],
                },
                index=data.X.index[te],
            )
        )
    if len(rows) < MIN_FOLDS:
        raise ModelError(
            f"Not enough history for walk-forward validation at horizon {horizon} "
            f"({n} labelled samples; need roughly {2 * min_train + MIN_FOLDS * TEST_BLOCK})."
        )
    oos = pd.concat(rows)
    oos["price_pred"] = oos["price0"] * np.exp(oos["ret_pred"])
    oos["price_base"] = oos["price0"]
    return oos


def summarise_oos(oos: pd.DataFrame, horizon: int) -> dict:
    err_model = (oos["price_pred"] - oos["price_actual"]).abs()
    err_base = (oos["price_base"] - oos["price_actual"]).abs()
    mae_m, mae_b = float(err_model.mean()), float(err_base.mean())
    improvement = 1.0 - mae_m / mae_b if mae_b > 0 else 0.0

    moved = oos["ret_actual"] != 0
    hit = (np.sign(oos["ret_pred"]) == np.sign(oos["ret_actual"]))[moved]
    always_up = (oos["ret_actual"] > 0)[moved]

    per_fold = pd.DataFrame({"m": err_model, "b": err_base, "fold": oos["fold"]}).groupby("fold").mean()
    folds_won = int((per_fold["m"] < per_fold["b"]).sum())
    n_folds = int(len(per_fold))

    if improvement >= MATERIAL_IMPROVEMENT and folds_won * 2 > n_folds:
        verdict, label = "beats", "Beats baseline"
    elif improvement < 0:
        verdict, label = "worse", "Does not beat baseline"
    else:
        verdict, label = "inconclusive", "No reliable edge over baseline"

    resid = oos["ret_actual"] - oos["ret_pred"]
    lo_q, hi_q = (1 - INTERVAL_LEVEL) / 2, 1 - (1 - INTERVAL_LEVEL) / 2
    return {
        "horizon": horizon,
        "n_predictions": int(len(oos)),
        "n_folds": n_folds,
        "test_start": oos.index[0].date().isoformat(),
        "test_end": oos.index[-1].date().isoformat(),
        "mae_model": mae_m,
        "mae_baseline": mae_b,
        "mae_improvement_pct": float(improvement * 100),
        "folds_beating_baseline": folds_won,
        "directional_accuracy": float(hit.mean() * 100),
        "always_up_accuracy": float(always_up.mean() * 100),
        "verdict": verdict,
        "verdict_label": label,
        "residual_std": float(resid.std(ddof=1)),
        "residual_quantiles": {"low": float(resid.quantile(lo_q)), "high": float(resid.quantile(hi_q))},
    }


def _fmt_pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:+.{digits}f}%"


def explain(pipe: Pipeline, x_last: pd.Series, horizon: int, top: int = 3) -> tuple[list[dict], float]:
    """Per-feature contributions (coef x standardised value) to the forecast return, plus the drift term."""
    scaler: StandardScaler = pipe.named_steps["scale"]
    ridge: Ridge = pipe.named_steps["ridge"]
    z = scaler.transform(x_last.to_numpy().reshape(1, -1))[0]
    contrib = ridge.coef_ * z
    order = np.argsort(-np.abs(contrib))[:top]
    out = []
    for j in order:
        name = FEATURE_NAMES[j]
        value = float(x_last.iloc[j])
        zj = float(z[j])
        level = "well above" if zj > 1 else "above" if zj > 0.25 else "well below" if zj < -1 else "below" if zj < -0.25 else "close to"
        direction = "adds to" if contrib[j] > 0 else "subtracts from"
        out.append(
            {
                "feature": name,
                "label": FEATURE_LABELS[name],
                "value": value,
                "z_score": zj,
                "contribution_pct": float(contrib[j] * 100),
                "text": (
                    f"{FEATURE_LABELS[name]} is {_fmt_pct(value, 2)}, {level} its usual level in the training window "
                    f"(z = {zj:+.1f}); this {direction} the {horizon}-session forecast by {abs(contrib[j]) * 100:.2f} percentage points."
                ),
            }
        )
    return out, float(ridge.intercept_)


def _target_date(last: pd.Timestamp, horizon: int) -> str:
    # Weekdays only; exchange holidays are not modelled, so the date is approximate.
    return pd.bdate_range(last + pd.Timedelta(days=1), periods=horizon)[-1].date().isoformat()


def run_forecast(close: pd.Series, horizons=config.HORIZONS) -> dict:
    close = close.astype(float)
    if len(close) < config.MIN_ROWS:
        raise ModelError(f"Need at least {config.MIN_ROWS} observations; have {len(close)}.")
    feats = build_features(close)
    x_last = feats.iloc[-1]
    if x_last.isna().any():
        raise ModelError("Latest feature row is incomplete.")
    last_date, last_price = close.index[-1], float(close.iloc[-1])

    forecasts, performance, signals = [], [], {}
    for h in horizons:
        oos = walk_forward(close, h)
        perf = summarise_oos(oos, h)
        performance.append(perf)

        data = _labelled(close, h)  # every labelled row has a fully observed target (<= last date)
        pipe = make_pipeline().fit(data.X.to_numpy(), data.y.to_numpy())
        pred = float(pipe.predict(x_last.to_numpy().reshape(1, -1))[0])
        q = perf["residual_quantiles"]
        low_ret, high_ret = pred + q["low"], pred + q["high"]
        drivers, drift = explain(pipe, x_last, h)
        signals[str(h)] = {"drivers": drivers, "drift_pct": drift * 100}
        forecasts.append(
            {
                "horizon": h,
                "base_date": last_date.date().isoformat(),
                "target_date": _target_date(last_date, h),
                "base_price": last_price,
                "predicted_return_pct": pred * 100,
                "forecast_price": last_price * float(np.exp(pred)),
                "expected_change_pct": (float(np.exp(pred)) - 1) * 100,
                "interval": {
                    "level": INTERVAL_LEVEL,
                    "low": last_price * float(np.exp(low_ret)),
                    "high": last_price * float(np.exp(high_ret)),
                    "low_pct": (float(np.exp(low_ret)) - 1) * 100,
                    "high_pct": (float(np.exp(high_ret)) - 1) * 100,
                },
                "residual_std_pct": perf["residual_std"] * 100,
            }
        )

    vol20 = feats["vol_20"].dropna()
    context = {
        "vol_20_percentile": float((vol20 <= vol20.iloc[-1]).mean() * 100),
        "price_vs_ma50_pct": float(x_last["ma_ratio_50"] * 100),
        "momentum_20_pct": float((np.exp(x_last["mom_20"]) - 1) * 100),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "forecasts": forecasts,
        "performance": performance,
        "signals": signals,
        "context": context,
        "model": {
            "type": "Ridge regression (scikit-learn) on standardised features, one model per horizon",
            "alpha": RIDGE_ALPHA,
            "features": [{"name": n, "label": FEATURE_LABELS[n]} for n in FEATURE_NAMES],
            "target": "forward log return over the horizon, converted to a price",
            "validation": (
                f"Expanding-window walk-forward in ~{TEST_BLOCK}-session blocks; training rows whose target window "
                "reaches the test block are purged; scaler fitted on training rows only."
            ),
            "interval": f"{int(INTERVAL_LEVEL * 100)}% range from empirical out-of-sample residual quantiles",
            "baseline": "Price unchanged (predicted return = 0)",
            "warmup_sessions": WARMUP,
            "n_observations": int(len(close)),
        },
    }
