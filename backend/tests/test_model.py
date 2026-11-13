import numpy as np
import pandas as pd
import pytest

from app import model
from app.features import FEATURE_NAMES, build_features, build_target
from conftest import make_series


def test_features_are_causal(series):
    """Changing prices after date T must not change any feature at or before T."""
    T = 600
    base = build_features(series)
    altered = series.copy()
    altered.iloc[T + 1:] = altered.iloc[T + 1:] * np.random.default_rng(1).uniform(0.5, 1.5, len(altered) - T - 1)
    pd.testing.assert_frame_equal(base.iloc[: T + 1], build_features(altered).iloc[: T + 1])


def test_feature_values_match_definitions(series):
    f = build_features(series)
    t = 100
    assert f["ma_ratio_5"].iloc[t] == pytest.approx(series.iloc[t] / series.iloc[t - 4: t + 1].mean() - 1)
    assert f["ma_ratio_20"].iloc[t] == pytest.approx(series.iloc[t] / series.iloc[t - 19: t + 1].mean() - 1)
    assert f["mom_5"].iloc[t] == pytest.approx(np.log(series.iloc[t] / series.iloc[t - 5]))
    assert f["ret_t1"].iloc[t] == pytest.approx(np.log(series.iloc[t - 1] / series.iloc[t - 2]))
    assert list(f.columns) == FEATURE_NAMES


def test_target_is_forward_log_return(series):
    h = 5
    y = build_target(series, h)
    assert np.isclose(y.iloc[10], np.log(series.iloc[10 + h] / series.iloc[10]))
    assert y.iloc[-h:].isna().all() and y.iloc[:-h].notna().all()


@pytest.mark.parametrize("h", [1, 5, 20])
def test_splits_are_chronological_and_purged(h):
    n = 700
    pos = np.arange(n) + 20  # sample positions in the price series
    train, val, test, pre_test = model.split_positions(pos, h, n)
    assert train.max() < val.min() <= val.max() < test.min()
    # no target window crosses into the next period
    assert pos[train].max() + h < pos[val].min()
    assert pos[val].max() + h < pos[test].min()
    assert pos[pre_test].max() + h < pos[test].min()
    assert len(test) == n - int(n * 0.8)  # test period is never shrunk by purging


def test_prediction_ignores_prices_after_a_cutoff_for_validation_choice(series):
    """The default-model choice and validation errors must not depend on the test period's prices."""
    h = 5
    base = model.run_prediction(series, h)
    cut = base["splits"]["test"]["start"]
    altered = series.copy()
    mask = altered.index >= pd.Timestamp(cut)
    altered[mask] *= np.random.default_rng(3).uniform(0.5, 2.0, mask.sum())
    alt = model.run_prediction(altered, h)
    assert alt["selection"]["validation_mae"]["ridge"] == pytest.approx(base["selection"]["validation_mae"]["ridge"])
    assert alt["selection"]["validation_mae"]["random_forest"] == pytest.approx(base["selection"]["validation_mae"]["random_forest"])
    assert alt["selection"]["default"] == base["selection"]["default"]


@pytest.mark.parametrize("h", [1, 5, 20])
def test_prediction_shape_and_consistency(series, h):
    out = model.run_prediction(series, h)
    f = out["forecast"]
    last = float(series.iloc[-1])
    assert out["base_price"] == pytest.approx(last)
    assert f["price"] == pytest.approx(last * (1 + f["change_pct"] / 100))
    assert f["interval"]["low"] < f["interval"]["high"] and f["interval"]["level"] == 0.8
    assert out["target_date"] > out["base_date"]
    assert f["model"] == out["selection"]["default"]
    te = out["test"]
    assert te["n"] == len(out["test_series"]) and set(te["methods"]) == {"ridge", "random_forest", "unchanged"}
    # baseline MAE recomputed independently from the series the chart shows
    ts = pd.DataFrame(out["test_series"])
    assert te["methods"]["unchanged"]["mae"] == pytest.approx((ts["actual"] - ts["baseline"]).abs().mean(), rel=1e-3)
    assert te["methods"]["unchanged"]["directional_accuracy"] is None
    assert 0 <= f["interval"]["observed_coverage_pct"] <= 100
    # actual price in the chart is the real price h sessions after the origin
    row = out["test_series"][0]
    assert row["actual"] == pytest.approx(float(series.loc[pd.Timestamp(row["date"])]), abs=0.01)


def test_random_walk_shows_no_material_advantage():
    """On a driftless random walk the default model should not look materially better than 'unchanged'."""
    adv = 0
    for seed in range(4):
        s = make_series(1100, seed=seed, drift=0.0)
        adv += sum(model.run_prediction(s, h)["test"]["advantage"] for h in (1, 5, 20))
    assert adv <= 2


def test_insufficient_history_raises():
    with pytest.raises(model.ModelError):
        model.run_prediction(make_series(250), 5)


def test_bad_horizon_raises(series):
    with pytest.raises(model.ModelError):
        model.run_prediction(series, 7)


def test_range_widens_when_recent_volatility_is_higher():
    """Same history, but one copy has calm final weeks and the other turbulent ones: the range must be wider in the turbulent one."""
    base = make_series(900, seed=5, vol=0.008)
    calm, wild = base.copy(), base.copy()
    rng = np.random.default_rng(11)
    for series, v in ((calm, 0.002), (wild, 0.03)):
        shocks = rng.normal(0, v, 30)
        series.iloc[-30:] = series.iloc[-31] * np.exp(np.cumsum(shocks))
    widths = []
    for series in (calm, wild):
        i = model.run_prediction(series, 5)["forecast"]["interval"]
        widths.append((i["high"] - i["low"]) / float(series.iloc[-1]))
    assert widths[1] > 2 * widths[0]
