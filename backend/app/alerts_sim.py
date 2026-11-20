"""Safe simulation of the alert pipeline: a throwaway database, simulated prices and forecasts, and NO network and NO SMS.

    python -m app.worker alerts --simulate --price 4101 --forecast 4120

Nothing here touches the real ledger (it points the database at a temporary directory) and the alert mode is forced to ``simulate``.
"""
from __future__ import annotations

import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from . import alerts, config, ledger, n8n_link


def issue_sim_forecast(predicted: float, now: float, *, task: str = "fwd_1", algorithm: str = "ridge", base: float | None = None, half_range: float = 60.0,
                       issued_ago_h: float = 0.0, target_in_h: float = 20.0) -> str:
    """Write one forecast (in whatever database is currently active) targeting ``target_in_h`` hours from ``now``."""
    ny = "America/New_York"
    target = pd.Timestamp(now + target_in_h * 3600, unit="s", tz="UTC").tz_convert(ny)
    issued = now - issued_ago_h * 3600
    ledger.issue(task=task, role="champion", version=f"{task}:{algorithm}:sim", algorithm=algorithm, feature_schema="sim", input_cutoff=issued - 60,
                 base_price=base if base is not None else predicted - 5, base_time=str(target.date() - timedelta(days=1)), target_date=str(target.date()), target_time=target.isoformat(),
                 predicted=predicted, low=predicted - half_range, high=predicted + half_range, level=0.8, horizon=1 if task == "fwd_1" else None, issued_at=issued)
    return str(target.date())


def simulate(price: float | None = None, forecast: float | None = None) -> dict:
    tmp = tempfile.mkdtemp(prefix="gold-alert-sim-")
    config.DATA_DIR, config.ALERTS_MODE = Path(tmp), "simulate"
    now = time.time()
    out: dict = {"database": tmp, "mode": "simulate (no network, no SMS)"}
    if forecast is not None:
        issue_sim_forecast(forecast, now)
        out["forecast_check"] = alerts.check_forecasts(now)
    if price is not None:
        out["price_check"] = alerts.check_price(now, quote={"price": price, "as_of": now - 120, "source": "simulated", "instrument": alerts.INSTRUMENT_NAME, "basis": "futures"})
    out["delivery"] = n8n_link.deliver_pending(now)
    out["events"] = [{"type": e["alert_type"], "threshold": e["threshold"], "sms_state": e["sms_state"], "message": e["message"]} for e in alerts.recent_events(10)]
    snap = n8n_link.build_snapshot(now, fetch_quote=False)
    out["morning_sms_if_sent_now"] = n8n_link.decide_morning(snap, now)
    out["log"] = [f"{r['topic']}:{r['outcome']} {r['detail'] or ''}"[:140] for r in alerts.db.rows("SELECT topic, outcome, detail FROM alert_log ORDER BY id")]
    return out
