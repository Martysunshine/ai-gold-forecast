"""Independent trace of the stored 1-, 5- and 20-session forecasts.

For each horizon this re-derives the champion forecast from the data and checks it against what was stored, what the API returns and what the CSV
download contains. It reports the inputs, the training cutoff, the model version, the output and the target date. It reads the ledger; it never writes.

Run:  cd backend && PYTHONPATH=. .venv/bin/python -m app.forecast_trace
"""
from __future__ import annotations

import csv
import io
import json
import sys

import numpy as np
import pandas as pd

from . import db, drivers, ledger, learning, registry, research_drivers as rd
from .sessions import next_sessions


def trace_horizon(h: int, close: pd.Series, drivers_df: pd.DataFrame | None) -> dict:
    task = f"fwd_{h}"
    champ = registry.champion(task)
    stored = db.rows("SELECT * FROM forecasts WHERE task=? AND role='champion' ORDER BY issued_at DESC LIMIT 1", (task,))
    out: dict = {"task": task, "version": champ["version"], "algorithm": champ["algorithm"], "feature_schema": champ["feature_schema"], "checks": {}}
    if not stored:
        out["checks"]["a stored champion forecast exists"] = False
        return out
    f = stored[0]
    base = pd.Timestamp(f["base_time"])
    out["stored"] = {"id": f["id"], "base_date": f["base_time"], "base_price": f["base_price"], "target_date": f["target_date"], "predicted": f["predicted"],
                     "low": f["low"], "high": f["high"], "issued_at_utc": pd.Timestamp(f["issued_at"], unit="s", tz="UTC").isoformat(), "dataset_hash": f["dataset_hash"],
                     "input_cutoff_utc": pd.Timestamp(f["input_cutoff"], unit="s", tz="UTC").isoformat()}
    # 1. inputs and cutoff: rebuild the dataset from data up to the stored base date ONLY, then confirm no label or input reaches past it.
    c = close[close.index <= base]
    ds = rd.build_dataset(c, h, drivers_df)          # drivers are long-format and joined by AVAILABILITY time inside build_dataset (never by date)
    X, y, pos = ds["X"], ds["y"], ds["pos"]
    last_label_origin = X.index[-1]
    out["training"] = {"rows": int(len(y)), "first_origin": X.index[0].date().isoformat(), "last_origin": last_label_origin.date().isoformat(),
                       "last_label_date": c.index[int(pos[-1]) + h].date().isoformat() if int(pos[-1]) + h < len(c) else None, "cutoff": base.date().isoformat(),
                       "features": registry.champion(task)["feature_schema"]}
    out["checks"]["every training label date is on or before the base date"] = bool(int(pos[-1]) + h <= len(c) - 1)
    out["checks"]["the dataset used for the forecast ends at the base date"] = bool(ds["now_date"] == base)
    if f["dataset_hash"] is None:
        out["dataset_hash_note"] = "no hash stored: this forecast was issued before dataset hashes were recorded (an honest gap, not a mismatch)"
    elif close.index[-1] == base:      # the hash covers every input row, so it can only be reproduced while the data has not moved on since the issue
        out["checks"]["the dataset hash matches the stored one"] = ledger.dataset_hash(c, drivers_df) == f["dataset_hash"]
    else:
        out["dataset_hash_note"] = "not recomputed: the data has been extended since this forecast was issued"
    # 2. the target date from the exchange calendar
    out["checks"]["the target date is the h-th exchange session after the base date"] = next_sessions(base, h)[-1].date().isoformat() == f["target_date"]
    # 3. the output, recomputed directly with the model class (not through issue_forward / fit_forecast)
    cols = learning.cols_for(champ["feature_schema"]) if champ["algorithm"] != "unchanged" else []
    if cols:
        mu = float(rd.ALGOS[champ["algorithm"]]().fit(X[cols].to_numpy(), y).predict(ds["x_now"][cols].to_numpy(dtype=float).reshape(1, -1))[0])
        recomputed = float(f["base_price"] * np.exp(mu))
        out["recomputed"] = {"predicted": round(recomputed, 2), "log_return": mu}
        out["checks"]["the point forecast recomputes to the stored value (±$0.01)"] = abs(recomputed - f["predicted"]) <= 0.01
    else:
        out["checks"]["the unchanged baseline equals the base price"] = abs(f["predicted"] - f["base_price"]) < 0.01
    out["checks"]["the stored range brackets the stored forecast"] = f["low"] < f["predicted"] < f["high"]
    return out


def compare_surfaces(h: int) -> dict:
    """API payload, stored row and CSV for the same forecast id must agree."""
    from . import main

    api = main.forecast_forward(h)["forecast"]
    row = db.rows("SELECT * FROM forecasts WHERE id=?", (api["forecast_id"],))[0]
    body = main.export_forecast(api["forecast_id"]).body.decode()
    csvrow = next(csv.DictReader(io.StringIO(body)))
    same = {"API = ledger (predicted, low, high, base)": [api["predicted"], api["low"], api["high"], api["reference_price"]] == [round(row["predicted"], 2), round(row["low"], 2), round(row["high"], 2), round(row["base_price"], 2)],
            "CSV = API (predicted, low, high, reference, target date)": [csvrow["predicted_price_usd"], csvrow["range_low_usd"], csvrow["range_high_usd"], csvrow["reference_price_usd"], csvrow["target_date"]]
            == [f"{api['predicted']:.2f}", f"{api['low']:.2f}", f"{api['high']:.2f}", f"{api['reference_price']:.2f}", api["target_date"]]}
    return {"forecast_id": api["forecast_id"], "checks": same}


def main_cli() -> int:
    from . import main

    close = main._dataset().series
    drv = drivers.load()
    ok = True
    for h in (1, 5, 20):
        t = trace_horizon(h, close, drv)
        t["surfaces"] = compare_surfaces(h)
        print(json.dumps({k: v for k, v in t.items() if k != "checks"}, indent=1, default=str))
        for name, passed in {**t["checks"], **t["surfaces"]["checks"]}.items():
            print(f"  {'PASS' if passed else 'FAIL'}  {name}")
            ok &= bool(passed)
    print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main_cli())
