"""The forecast as a file for other programs: ``python -m app.export --out forecast.json``.

One JSON document holds the forecast of every horizon. Each forecast states the period it applies to
(``valid_from`` to ``valid_until``) and the model's own test results, so a reader can tell a current
forecast from an old one and a tested advantage from none without knowing how the model works.

Nothing here places or suggests a trade. A reader that acts on the direction is expected to look at
``evaluation.advantage`` first: it is ``false`` whenever the model did not beat "price unchanged" on the
test period.

Run from ``backend/``. The exit code is 0 when the file was written and 1 when it was not; a failed run
leaves an existing file as it was.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config
from .data import DataStore, DataUnavailable, Dataset
from .model import ModelError, run_prediction

SCHEMA = "ai-gold-forecast.forecast/1"
#: Daily bars are dated by the exchange's local calendar day (see data._local_dates). A forecast for a
#: target date is therefore about the close of that local day, and stops applying when the day ends.
BAR_TIMEZONE = "America/New_York"


def _utc(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_until(target_date: str, tz: str = BAR_TIMEZONE) -> str:
    """The instant the target date ends in the bars' timezone, in UTC."""
    next_day = pd.Timestamp(target_date) + pd.Timedelta(days=1)  # local midnight, so no daylight-saving drift
    return _utc(next_day.tz_localize(tz))


def direction_of(change_pct: float) -> str:
    return "up" if change_pct > 0 else "down" if change_pct < 0 else "flat"


def forecast_entry(result: dict, generated_at: str) -> dict:
    """One horizon of ``model.run_prediction`` in the export's shape."""
    f, t = result["forecast"], result["test"]
    chosen = t["methods"][f["model"]]
    return {
        "horizon_sessions": result["horizon"],
        "base_date": result["base_date"],
        "target_date": result["target_date"],
        "base_price": round(result["base_price"], 4),
        "price": round(f["price"], 4),
        "change_pct": round(f["change_pct"], 6),
        "direction": direction_of(f["change_pct"]),
        "range": {
            "level": f["interval"]["level"],
            "low": round(f["interval"]["low"], 4),
            "high": round(f["interval"]["high"], 4),
            "low_pct": round(f["interval"]["low_pct"], 6),
            "high_pct": round(f["interval"]["high_pct"], 6),
        },
        "model": f["model"],
        "model_label": f["model_label"],
        "valid_from": generated_at,
        "valid_until": valid_until(result["target_date"]),
        "evaluation": {
            "test_start": t["period"]["start"],
            "test_end": t["period"]["end"],
            "n": t["n"],
            "mae_model_usd": round(chosen["mae"], 4),
            "mae_unchanged_usd": round(t["methods"]["unchanged"]["mae"], 4),
            "vs_unchanged_pct": round(chosen["vs_baseline_pct"], 4),
            "directional_accuracy_pct": round(chosen["directional_accuracy"], 2),
            "always_up_pct": round(t["always_up_pct"], 2),
            "advantage": bool(t["advantage"]),
            "verdict": t["verdict"],
        },
    }


def build_export(ds: Dataset, now: datetime | None = None) -> dict:
    """The export document for the dataset ``ds``. Raises ``ModelError`` when a horizon cannot be forecast."""
    now = now or datetime.now(timezone.utc)
    generated_at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "symbol": ds.meta.get("symbol"),
        "instrument": (ds.meta.get("instrument") or {}).get("name"),
        "source": ds.meta.get("source"),
        "bar_timezone": BAR_TIMEZONE,
        "data": {
            "key": ds.key,
            "latest_close_date": ds.series.index[-1].date().isoformat(),
            "latest_close": round(float(ds.series.iloc[-1]), 4),
            "fetched_at": ds.meta.get("fetched_at"),
            "provider_error": ds.meta.get("provider_error"),
            "in_progress": ds.meta.get("in_progress"),
        },
        "forecasts": [forecast_entry(run_prediction(ds.series, h), generated_at) for h in config.HORIZONS],
        "note": "A forecast, not advice. Check evaluation.advantage before relying on a direction.",
    }


def write_export(doc: dict, path: Path) -> None:
    """Replace ``path`` in one step, so a reader never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None, store: DataStore | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.export", description="Write the current forecasts as one JSON file.")
    parser.add_argument("--out", required=True, type=Path, help="The file to write (replaced in one step).")
    parser.add_argument("--refresh", action="store_true", help="Fetch prices now instead of using data younger than the cache time.")
    args = parser.parse_args(argv)
    try:
        ds = (store or DataStore()).get(force_refresh=args.refresh)
        doc = build_export(ds)
    except (DataUnavailable, ModelError) as exc:
        print(f"no forecast written: {exc}", file=sys.stderr)
        return 1
    write_export(doc, args.out)
    one = doc["forecasts"][0]
    print(f"forecast written: {one['direction']} {one['change_pct']:+.2f}% to {one['target_date']} ({one['model_label']}), "
          f"valid until {one['valid_until']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
