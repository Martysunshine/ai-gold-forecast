"""Candidate feature groups beyond the original 12 gold features.

Every feature at date t uses closing prices up to and including t ONLY (trailing windows). Nothing is a pivot, a swing that needed later
bars to confirm, or a value that can change when more data arrives: ``tests/test_feature_groups.py`` recomputes every feature on a truncated
series and requires the identical value (no look-ahead, no repainting).

Groups (versions are frozen: changing a window or a level creates a NEW schema with a fresh forward record):

  patterns_v1   moving-average distances and slopes, trailing high/low distances and breakout strength, all volatility-normalised.
  fib_v1        patterns_v1 + FIBONACCI (EXPERIMENTAL). Retracement levels of the trailing 20- and 60-session range. The windows and the five
                levels (23.6, 38.2, 50, 61.8, 78.6 %) were fixed in advance and are not tuned. The levels are inputs a model may or may not
                use; they are NOT treated as support or resistance. The group is kept only if a pre-set paired test supports it
                (``learning.fib_verdict``); otherwise its candidates are rejected and stop issuing forecasts, while the chart is unaffected.

Futures volume is deliberately NOT a feature: the stored series has closes (and highs) only, and the continuous front-month contract's volume
is distorted by contract rolls. It would need a verified source and roll handling first.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FIB_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_WINDOWS = (20, 60)
EPS = 1e-12

PATTERN_LABELS = {
    "ma_dist_10": "Price vs 10-session average", "ma_dist_50": "Price vs 50-session average",
    "ma_slope_20": "Slope of the 20-session average (5 sessions)", "ma_slope_50": "Slope of the 50-session average (10 sessions)",
    "hi_dist_20": "Distance below the 20-session high (in daily volatilities)", "lo_dist_20": "Distance above the 20-session low (in daily volatilities)",
    "hi_dist_60": "Distance below the 60-session high (in daily volatilities)", "lo_dist_60": "Distance above the 60-session low (in daily volatilities)",
    "brk_up_20": "Breakout above the prior 20-session high", "brk_dn_20": "Breakdown below the prior 20-session low",
}
PATTERN_FEATURES = list(PATTERN_LABELS)
FIB_FEATURES = [f"fib{w}_{k}" for w in FIB_WINDOWS for k in ("pos", "dir", "near")]
FIB_LABELS = {f"fib{w}_pos": f"Position in the {w}-session range (0 = low, 1 = high)" for w in FIB_WINDOWS} | \
             {f"fib{w}_dir": f"Direction of the {w}-session swing (+1 high is more recent)" for w in FIB_WINDOWS} | \
             {f"fib{w}_near": f"Distance to the nearest Fibonacci level of the {w}-session range (in daily volatilities)" for w in FIB_WINDOWS}


def _vol20(close: pd.Series) -> pd.Series:
    return np.log(close).diff().rolling(20).std()


def pattern_features(close: pd.Series) -> pd.DataFrame:
    close = close.astype(float)
    v = _vol20(close).replace(0, np.nan)
    f = pd.DataFrame(index=close.index)
    f["ma_dist_10"] = close / close.rolling(10).mean() - 1.0
    f["ma_dist_50"] = close / close.rolling(50).mean() - 1.0
    ma20, ma50 = close.rolling(20).mean(), close.rolling(50).mean()
    f["ma_slope_20"] = ma20 / ma20.shift(5) - 1.0
    f["ma_slope_50"] = ma50 / ma50.shift(10) - 1.0
    for w in (20, 60):
        f[f"hi_dist_{w}"] = np.log(close / close.rolling(w).max()) / v          # <= 0: how far below the trailing high
        f[f"lo_dist_{w}"] = np.log(close / close.rolling(w).min()) / v          # >= 0: how far above the trailing low
    prior_hi = close.shift(1).rolling(20).max()                                   # the PRIOR window: today does not define its own breakout
    prior_lo = close.shift(1).rolling(20).min()
    f["brk_up_20"] = ((close - prior_hi) / close / v).clip(lower=0, upper=5)
    f["brk_dn_20"] = ((prior_lo - close) / close / v).clip(lower=0, upper=5)
    return f[PATTERN_FEATURES]


def _argmax_recent(x: np.ndarray) -> float:
    return float(len(x) - 1 - np.argmax(x[::-1]))     # index of the most recent maximum


def _argmin_recent(x: np.ndarray) -> float:
    return float(len(x) - 1 - np.argmin(x[::-1]))


def fib_features(close: pd.Series) -> pd.DataFrame:
    """Trailing-window Fibonacci position/direction/nearest-level distance. The window ends at t, so a value never changes later."""
    close = close.astype(float)
    v = _vol20(close).replace(0, np.nan)
    f = pd.DataFrame(index=close.index)
    for w in FIB_WINDOWS:
        hi, lo = close.rolling(w).max(), close.rolling(w).min()
        rng = (hi - lo).where((hi - lo) > EPS)
        hi_i = close.rolling(w).apply(_argmax_recent, raw=True)
        lo_i = close.rolling(w).apply(_argmin_recent, raw=True)
        up = hi_i > lo_i                                                       # the high is more recent than the low: an up-swing is being retraced
        f[f"fib{w}_pos"] = (close - lo) / rng
        f[f"fib{w}_dir"] = np.where(up, 1.0, -1.0)
        levels = np.stack([np.where(up, hi - k * rng, lo + k * rng) for k in FIB_LEVELS])  # retrace down from the high / up from the low
        nearest = levels[np.abs(levels - close.to_numpy()).argmin(axis=0), np.arange(len(close))]
        f[f"fib{w}_near"] = (close.to_numpy() - nearest) / close.to_numpy() / v.to_numpy()
        f.loc[rng.isna(), [f"fib{w}_pos", f"fib{w}_near"]] = np.nan
        f.loc[hi.isna(), f"fib{w}_dir"] = np.nan
    return f[FIB_FEATURES]


def all_candidate_features(close: pd.Series) -> pd.DataFrame:
    return pd.concat([pattern_features(close), fib_features(close)], axis=1)
