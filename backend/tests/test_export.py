import json
from datetime import datetime, timezone

import pytest

from app import config, export
from app.data import DataUnavailable, Dataset
from app.model import run_prediction
from conftest import make_series

NOW = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
META = {"source_kind": "provider", "source": "test data", "symbol": "GC=F", "instrument": {"name": "Gold futures"},
        "fetched_at": "2026-11-16T05:59:00+00:00"}


def test_the_export_holds_every_horizon_with_the_models_own_test_results(series):
    ds = Dataset(series, META)
    doc = export.build_export(ds, NOW)
    assert doc["schema"] == export.SCHEMA and doc["generated_at"] == "2026-11-16T06:00:00Z"
    assert doc["data"]["key"] == ds.key and doc["data"]["latest_close_date"] == series.index[-1].date().isoformat()
    assert [f["horizon_sessions"] for f in doc["forecasts"]] == list(config.HORIZONS)
    for f in doc["forecasts"]:
        r = run_prediction(series, f["horizon_sessions"])
        assert f["base_date"] == r["base_date"] and f["target_date"] == r["target_date"]
        assert f["price"] == pytest.approx(r["forecast"]["price"], abs=1e-3)
        assert f["change_pct"] == pytest.approx(r["forecast"]["change_pct"], abs=1e-5)
        assert f["direction"] == ("up" if r["forecast"]["change_pct"] > 0 else "down")
        assert f["range"]["low"] < f["price"] < f["range"]["high"]
        assert f["valid_from"] == doc["generated_at"] and f["valid_until"] > f["target_date"]
        e = f["evaluation"]
        assert e["advantage"] is r["test"]["advantage"] and e["verdict"] == r["test"]["verdict"]
        assert e["mae_unchanged_usd"] == pytest.approx(r["test"]["methods"]["unchanged"]["mae"], abs=1e-3)
        assert 0 <= e["directional_accuracy_pct"] <= 100 and 0 <= e["always_up_pct"] <= 100
    assert json.loads(json.dumps(doc)) == doc  # plain JSON types only


def test_a_forecast_applies_until_its_target_day_ends_where_the_bars_are_dated():
    assert export.valid_until("2026-11-16") == "2026-11-17T05:00:00Z"  # New York winter time
    assert export.valid_until("2026-07-01") == "2026-07-02T04:00:00Z"  # and summer time
    assert export.valid_until("2026-10-30") == "2026-10-31T04:00:00Z"  # the Friday before the clocks go back
    assert export.valid_until("2026-11-01") == "2026-11-02T05:00:00Z"  # the day they do: local midnight, not +24 h


def test_direction_words():
    assert [export.direction_of(v) for v in (0.2, -0.2, 0.0)] == ["up", "down", "flat"]


class Store:
    def __init__(self, ds=None, error=None):
        self.ds, self.error, self.refreshed = ds, error, None

    def get(self, force_refresh=False):
        self.refreshed = force_refresh
        if self.error:
            raise self.error
        return self.ds


def test_the_command_writes_the_file_in_one_step(series, tmp_path, capsys):
    out = tmp_path / "nested" / "gold.json"
    store = Store(Dataset(series, META))
    assert export.main(["--out", str(out), "--refresh"], store=store) == 0
    assert store.refreshed is True
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] == export.SCHEMA and len(doc["forecasts"]) == len(config.HORIZONS)
    assert not list(out.parent.glob("*.tmp"))
    assert "forecast written" in capsys.readouterr().out


def test_a_failed_run_leaves_the_last_file_alone(tmp_path, capsys):
    out = tmp_path / "gold.json"
    out.write_text("{\"kept\": true}", encoding="utf-8")
    assert export.main(["--out", str(out)], store=Store(error=DataUnavailable("no data"))) == 1
    assert json.loads(out.read_text(encoding="utf-8")) == {"kept": True}
    assert "no forecast written: no data" in capsys.readouterr().err
    # too little history is a model error, reported the same way
    assert export.main(["--out", str(out)], store=Store(Dataset(make_series(n=120), META))) == 1
    assert json.loads(out.read_text(encoding="utf-8")) == {"kept": True}
