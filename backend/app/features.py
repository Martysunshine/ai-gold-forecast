"""Feature engineering. Every feature at date t uses closing prices up to and including t only."""
from __future__ import annotations

import numpy as np
import pandas as pd

WARMUP = 60  # longest look-back window (rows lost at the start of the series)

FEATURE_LABELS = {
    "ret_t0": "Latest daily return",
    "ret_t1": "Daily return 1 session ago",
    "ret_t2": "Daily return 2 sessions ago",
    "ret_t3": "Daily return 3 sessions ago",
    "ret_t4": "Daily return 4 sessions ago",
    "mom_5": "5-session momentum",
    "mom_10": "10-session momentum",
    "mom_20": "20-session momentum",
    "mom_60": "60-session momentum",
    "ma_ratio_10": "Price vs 10-session average",
    "ma_ratio_50": "Price vs 50-session average",
    "ma_cross_10_50": "10-session vs 50-session average",
    "vol_10": "10-session volatility",
    "vol_20": "20-session volatility",
    "vol_60": "60-session volatility",
}
FEATURE_NAMES = list(FEATURE_LABELS)
# Features that are returns / ratios shown as percentages; volatility is a daily standard deviation.
PERCENT_FEATURES = set(FEATURE_NAMES)


def build_features(close: pd.Series) -> pd.DataFrame:
    """Return a frame of features indexed like ``close`` (leading rows are NaN until the warm-up passes)."""
    close = close.astype(float)
    logp = np.log(close)
    ret = logp.diff()
    f = pd.DataFrame(index=close.index)
    for lag in range(5):
        f[f"ret_t{lag}"] = ret.shift(lag)
    for k in (5, 10, 20, 60):
        f[f"mom_{k}"] = logp - logp.shift(k)
    sma10 = close.rolling(10).mean()
    sma50 = close.rolling(50).mean()
    f["ma_ratio_10"] = close / sma10 - 1.0
    f["ma_ratio_50"] = close / sma50 - 1.0
    f["ma_cross_10_50"] = sma10 / sma50 - 1.0
    for k in (10, 20, 60):
        f[f"vol_{k}"] = ret.rolling(k).std()
    return f[FEATURE_NAMES]


def build_target(close: pd.Series, horizon: int) -> pd.Series:
    """Forward log return over ``horizon`` sessions. The last ``horizon`` rows are NaN (not yet observed)."""
    logp = np.log(close.astype(float))
    return (logp.shift(-horizon) - logp).rename(f"target_{horizon}")
