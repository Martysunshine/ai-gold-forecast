"""The CSV export is built from the same stored record as the screen, and shadow estimates are matched to exactly the same target."""
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, ledger, main


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db._inited.discard(str(db.path()))


def _issue(role="champion", algo="ridge", version="fwd_5:ridge:gold_v1", predicted=3901.234, schema="gold_v1"):
    now = time.time()
    assert ledger.issue(task="fwd_5", role=role, version=version, algorithm=algo, feature_schema=schema, input_cutoff=now - 3600, base_price=3850.5,
                        base_time="2026-11-17", target_date="2099-01-05", target_time="2099-01-05T13:30:00-05:00", predicted=predicted, low=3700.126, high=4100.987,
                        level=0.8, horizon=5, data_key="2026-11-17|900", issued_at=now - 60)
    return db.rows("SELECT * FROM forecasts WHERE model_version=?", (version,))[0]


def test_export_is_the_stored_forecast_with_display_rounding():
    f = _issue()
    r = TestClient(main.app).get("/api/forecast/export", params={"forecast_id": f["id"]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    head, row = lines[0].split(","), __import__("csv").reader([lines[1]]).__next__()
    d = dict(zip(head, row))
    assert d["forecast_id"] == f["id"] and d["predicted_price_usd"] == "3901.23" and d["range_low_usd"] == "3700.13" and d["range_high_usd"] == "4100.99"
    assert d["reference_price_usd"] == "3850.50" and d["change_usd"] == "50.73" and d["target_date"] == "2099-01-05" and d["horizon_sessions"] == "5"
    assert d["actual_usd"] == "" and d["inside_range"] == ""      # not matured: nothing is invented


def test_export_unknown_id_is_404_not_a_guess():
    assert TestClient(main.app).get("/api/forecast/export", params={"forecast_id": "nope"}).status_code == 404


def test_experimental_lists_only_shadows_of_the_same_target_and_base():
    champ = _issue()
    _issue(role="shadow", algo="hgb", version="fwd_5:hgb:drivers_v1", predicted=3911.0, schema="drivers_v1")
    other = _issue(role="shadow", algo="ridge", version="fwd_5:ridge:other", predicted=1.0)
    got = main._experimental(champ)
    assert {e["version"] for e in got} >= {"fwd_5:hgb:drivers_v1"} and all(e["predicted"] > 0 for e in got)
    assert champ["id"] not in {e["forecast_id"] for e in got}
    assert other["id"] in {e["forecast_id"] for e in got}        # same target and base: listed (it is a shadow of the same forecast)
