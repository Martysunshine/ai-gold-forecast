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
    after = build_features(altered)
    pd.testing.assert_frame_equal(base.iloc[: T + 1], after.iloc[: T + 1])


def test_target_is_forward_log_return(series):
    h = 5
    y = build_target(series, h)
    assert np.isclose(y.iloc[10], np.log(series.iloc[10 + h] / series.iloc[10]))
    assert y.iloc[-h:].isna().all() and y.iloc[:-h].notna().all()


@pytest.mark.parametrize("h", [1, 5, 20])
def test_purged_splits_have_no_target_overlap(h):
    n, min_train = 700, 300
    splits = list(model.purged_walk_forward_splits(n, h, min_train))
    assert len(splits) >= 4
    prev_end = 0
    for tr, te in splits:
        # sample k's target window ends at position k + h
        assert tr.max() + h < te.min(), "training target window reaches into the test block"
        assert te.min() - tr.max() - 1 >= h, "fewer than `horizon` samples were purged"
        assert te.min() >= prev_end  # test blocks advance chronologically, no overlap
        assert tr.min() == 0 and np.all(np.diff(tr) == 1)  # expanding window
        prev_end = te.max() + 1
    assert prev_end == n  # every sample is evaluated exactly once


def test_scaler_is_fit_on_training_rows_only(series):
    h = 5
    data = model._labelled(series, h)
    n = len(data.X)
    tr, te = next(iter(model.purged_walk_forward_splits(n, h, n // 2)))
    pipe = model.make_pipeline().fit(data.X.iloc[tr].to_numpy(), data.y.iloc[tr].to_numpy())
    scaler = pipe.named_steps["scale"]
    np.testing.assert_allclose(scaler.mean_, data.X.iloc[tr].mean().to_numpy())
    assert not np.allclose(scaler.mean_, data.X.mean().to_numpy())  # differs from full-sample stats


@pytest.mark.parametrize("h", [1, 5, 20])
def test_walk_forward_predictions_ignore_future_prices(series, h):
    """Out-of-sample predictions made at dates <= T must not depend on any price after T."""
    base = model.walk_forward(series, h)
    T_pos = 650
    T = series.index[T_pos]
    altered = series.copy()
    altered.iloc[T_pos + 1:] *= np.random.default_rng(7).uniform(0.4, 2.0, len(altered) - T_pos - 1)
    alt = model.walk_forward(altered, h)
    # Folds whose entire test block ends at or before T use only data <= T.
    last_fold_ok = base[base.index <= T].groupby("fold").size().index
    full_folds = [f for f in last_fold_ok if base[base.fold == f].index.max() <= T]
    assert full_folds, "test needs at least one fold fully before T"
    for f in full_folds:
        np.testing.assert_allclose(base[base.fold == f]["ret_pred"].to_numpy(), alt[alt.fold == f]["ret_pred"].to_numpy())


def test_summary_metrics_are_consistent(series):
    oos = model.walk_forward(series, 5)
    s = model.summarise_oos(oos, 5)
    assert s["mae_baseline"] == pytest.approx((oos["price0"] - oos["price_actual"]).abs().mean())
    assert 0 <= s["directional_accuracy"] <= 100 and s["n_folds"] >= 2
    assert s["verdict"] in {"beats", "worse", "inconclusive"}
    assert s["residual_quantiles"]["low"] < 0 < s["residual_quantiles"]["high"]


def test_forecast_generation_shapes_and_ordering(series):
    out = model.run_forecast(series)
    assert [f["horizon"] for f in out["forecasts"]] == [1, 5, 20]
    last = float(series.iloc[-1])
    for f in out["forecasts"]:
        assert f["interval"]["low"] < f["forecast_price"] < f["interval"]["high"]
        assert f["base_price"] == pytest.approx(last)
        assert f["forecast_price"] == pytest.approx(last * np.exp(f["predicted_return_pct"] / 100))
        assert f["target_date"] > f["base_date"]
    widths = [f["interval"]["high"] - f["interval"]["low"] for f in out["forecasts"]]
    assert widths == sorted(widths)  # longer horizon -> wider range
    assert set(out["signals"]) == {"1", "5", "20"} and all(len(v["drivers"]) == 3 for v in out["signals"].values())


def test_pure_noise_does_not_beat_baseline_materially():
    """On a driftless random walk the model must not appear to have a material edge (sanity check)."""
    wins = 0
    for seed in range(5):
        s = make_series(1100, seed=seed, drift=0.0)
        perf = model.run_forecast(s)["performance"]
        wins += sum(p["verdict"] == "beats" for p in perf)
    assert wins <= 2  # a few false positives out of 15 are possible but should be rare


def test_insufficient_history_raises():
    with pytest.raises(model.ModelError):
        model.run_forecast(make_series(200))
