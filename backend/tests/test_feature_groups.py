"""Pattern and Fibonacci features must be causal: no look-ahead, and no repainting when later bars arrive."""
import numpy as np
import pandas as pd
import pytest

from app.feature_groups import FIB_FEATURES, FIB_LEVELS, PATTERN_FEATURES, all_candidate_features, fib_features, pattern_features
from conftest import make_series


def _eq(a: pd.DataFrame, b: pd.DataFrame):
    np.testing.assert_allclose(a.to_numpy(float), b.to_numpy(float), rtol=1e-9, atol=1e-12, equal_nan=True)


@pytest.mark.parametrize("cut", [120, 333, 600, 899])
def test_features_never_repaint_when_later_data_arrives(cut):
    s = make_series(900, seed=3)
    full, early = all_candidate_features(s), all_candidate_features(s.iloc[:cut])
    _eq(full.iloc[:cut], early)            # every value on a past date is identical with or without the future


def test_changing_the_future_cannot_change_the_past():
    s = make_series(500, seed=5)
    t = s.copy()
    t.iloc[300:] = t.iloc[300:] * np.random.default_rng(1).uniform(0.7, 1.4, 200)   # rewrite everything after index 299
    _eq(all_candidate_features(s).iloc[:300], all_candidate_features(t).iloc[:300])


def test_ranges_and_signs_are_what_the_definitions_say():
    f = all_candidate_features(make_series(900, seed=7)).dropna()
    assert len(f) > 700
    assert f["hi_dist_20"].max() <= 1e-9 and f["lo_dist_20"].min() >= -1e-9 and f["hi_dist_60"].max() <= 1e-9
    assert f[[c for c in FIB_FEATURES if c.endswith("_pos")]].min().min() >= -1e-9 and f[[c for c in FIB_FEATURES if c.endswith("_pos")]].max().max() <= 1 + 1e-9
    assert set(np.unique(f[[c for c in FIB_FEATURES if c.endswith("_dir")]].to_numpy())) <= {-1.0, 1.0}
    assert f["brk_up_20"].min() >= 0 and f["brk_dn_20"].min() >= 0


def test_fibonacci_nearest_level_on_a_hand_built_swing():
    # 60 sessions rising 100 -> 200 then pulling back to 138.2: exactly the 61.8% retracement of the up-swing (200 - 0.618 * 100 = 138.2).
    up = np.linspace(100, 200, 50)
    s = pd.Series(np.concatenate([up, np.linspace(200, 138.2, 11)[1:]]), index=pd.bdate_range("2024-01-01", periods=60))
    f = fib_features(s)
    last = f.iloc[-1]
    assert last["fib20_dir"] == -1.0                                  # inside the last 20 sessions the LOW (138.2) is the more recent extreme
    assert abs(last["fib60_near"]) < 1e-6                             # sits on a Fibonacci level of the 60-session range
    assert last["fib60_dir"] == 1.0 and 0.37 < last["fib60_pos"] < 0.39


def test_levels_and_windows_are_the_pre_registered_ones():
    assert FIB_LEVELS == (0.236, 0.382, 0.5, 0.618, 0.786)           # changing these must be a new schema version, not an edit
    assert len(PATTERN_FEATURES) == 10 and len(FIB_FEATURES) == 6
    assert list(pattern_features(make_series(200)).columns) == PATTERN_FEATURES
