"""Hand-off to n8n (hosted scheduling + SMS) and the morning outlook.

Why a push hand-off and not "n8n calls the backend": the backend runs on a Mac that sleeps. A hosted n8n schedule cannot call a sleeping
laptop, so the backend PUSHES everything n8n needs (the latest forecast, the pre-rendered morning text with its own expiry, the review page,
the user's settings) to an n8n webhook whenever something changes. n8n stores it and runs the 07:30 job from that stored copy, so the
morning SMS goes out even if the Mac is off; if the stored forecast has expired by then, n8n says so instead of recycling it.
What this cannot do: create a forecast while the Mac is off. A forecast that was never issued is reported as unavailable.

The SMS provider (Twilio) and the phone number live only inside n8n. This backend holds one shared secret (N8N_AUTH_SECRET) and never the phone number.
GOLD_ALERTS_MODE=simulate (the default) never touches the network.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

import httpx

from . import alerts, config, db, ledger, review

Transport = Callable[[str, str, dict, "dict | None"], "tuple[int, dict]"]


# ----------------------------------------------------------------------------------------------------------------- mode / transport
def describe() -> dict:
    missing = [n for n, v in (("N8N_WEBHOOK_BASE", config.N8N_WEBHOOK_BASE), ("N8N_AUTH_SECRET", config.N8N_AUTH_SECRET)) if not v]
    live = config.ALERTS_MODE == "live"
    return {"mode": "live" if live else "simulate", "configured": not missing, "missing": missing if live else [],
            "note": ("Simulation: no SMS is sent and n8n is not contacted." if not live else
                     ("Live: alerts are handed to n8n, which sends the SMS." if not missing else f"Live mode needs {', '.join(missing)} (see .env.example). Nothing is sent until they are set.")),
            "review_base": review.review_base()}


def _http(method: str, url: str, headers: dict, body: dict | None) -> tuple[int, dict]:
    r = httpx.request(method, url, headers=headers, json=body, timeout=config.HTTP_TIMEOUT)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:200]}
    return r.status_code, data


def _call(method: str, path: str, body: dict | None, transport: Transport | None) -> tuple[int, dict]:
    headers = {config.N8N_AUTH_HEADER: config.N8N_AUTH_SECRET, "Content-Type": "application/json"}
    return (transport or _http)(method, f"{config.N8N_WEBHOOK_BASE}/{path}", headers, body)


# ----------------------------------------------------------------------------------------------------------------- contexts
def _tz_label(epoch: float, tz: str) -> str:
    return datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime("%H:%M") + f" {tz.split('/')[-1].replace('_', ' ')}"


def forecast_ctx(row: dict, task: str, cfg: dict) -> dict:
    tgt = datetime.fromisoformat(row["target_time"]).timestamp()
    return {"task": task, "forecast_id": row["id"], "predicted": row["predicted"], "low": row["low"], "high": row["high"], "level_pct": round(row["level"] * 100),
            "reference_price": row["base_price"], "baseline": row["algorithm"] == "unchanged", "issued_at": row["issued_at"], "target_epoch": tgt,
            "target_date": row["target_date"],
            "target_label": f"{alerts.session_label(row['target_date'])} · {_tz_label(tgt, 'America/New_York')} · {_tz_label(tgt, cfg['morning_tz'])}",
            "issued_label": f"{alerts._fmt_et(row['issued_at'])} · {alerts._fmt_tz(row['issued_at'], cfg['morning_tz'], '%H:%M')}",
            "performance": alerts.performance_line(task)}


def select_forecast(now: float, cfg: dict) -> tuple[dict | None, str]:
    """The latest VALID next-session forecast (fwd_1 preferred, then the intraday settlement forecast), or why there is none."""
    last_why = "no forecast has been issued yet"
    for task in alerts.SOURCES:
        row = ledger.latest(task, "champion")
        if row is None:
            continue
        tgt = datetime.fromisoformat(row["target_time"]).timestamp()
        age_h = (now - row["issued_at"]) / 3600
        if tgt <= now:
            last_why = f"the latest {task} forecast targeted {row['target_date']}, which has already settled"
        elif age_h > cfg["forecast_max_age_h"].get(task, 36):
            last_why = f"the latest {task} forecast is {age_h:.0f} h old"
        else:
            return forecast_ctx(row, task, cfg), "valid"
    return None, last_why


def latest_quote(now: float, cfg: dict, fetch: bool = True) -> dict | None:
    st = alerts._state_get("last_price_check")
    q = None
    if st and st.get("price") is not None:
        q = {"price": st["price"], "as_of": st["as_of"], "basis": st.get("basis", cfg["quote_basis"])}
    if (q is None or now - q["as_of"] > 15 * 60) and fetch:
        try:
            q = alerts.fetch_quote(cfg["quote_basis"])
        except Exception:  # noqa: BLE001
            pass
    if not q:
        return None
    basis_label = "COMEX gold futures GC=F, ~10 min delayed" if q["basis"] == "futures" else "Spot gold XAU/USD"
    return {**q, "basis_label": basis_label, "as_of_label": alerts._fmt_et(q["as_of"]), "age_label": f"{(now - q['as_of']) / 60:.0f} min old when this page was made"}


def review_html(kind_label: str, title: str, cfg: dict, now: float, expires_at: float, note: str | None = None, forecast: dict | None = None, fetch_quote: bool = True) -> str:
    if forecast is None:
        forecast, _ = select_forecast(now, cfg)
    return review.render({"title": title, "kind_label": kind_label, "forecast": forecast, "quote": latest_quote(now, cfg, fetch_quote), "settings": cfg, "note": note,
                          "generated_label": alerts._fmt_tz(now, cfg["morning_tz"], "%a %d %b %H:%M"), "expires_label": alerts._fmt_tz(expires_at, cfg["morning_tz"], "%a %d %b %H:%M")})


# ----------------------------------------------------------------------------------------------------------------- SMS text
def _levels_line(f: dict, cfg: dict) -> str:
    bits = [f"{alerts.lvl(x)} {'≤ forecast' if f['predicted'] >= x else '> forecast'}" for x in cfg["levels"]]
    return "Review levels: " + ", ".join(bits) + "."


def sms_text(event: dict, url: str) -> str:
    p = event["payload"]
    if event["alert_type"] == "forecast_headsup":
        tgt = datetime.strptime(p["target_date"], "%Y-%m-%d").strftime("%a %d %b")
        return (f"GOLD forecast heads-up: the settlement forecast for {tgt} is {alerts.usd(p['predicted'])} (80% range {alerts.usd(p['low'], 0)}–{alerts.usd(p['high'], 0)}), "
                f"at/above your {alerts.lvl(p['threshold'])} review level. A forecast of the close, not a touch; not a recommendation. {p['performance']} Review: {url}")
    basis = "futures, ~10 min delayed" if p["basis"] == "futures" else "spot"
    return (f"GOLD price alert (observed quote, not a forecast): {alerts.usd(p['price'])} ({basis}, as of {alerts._fmt_et(p['as_of'], '%H:%M')}) "
            f"is at/above your {alerts.lvl(p['threshold'])} review level. Review: {url}")


def morning_block(now: float, cfg: dict, url: str) -> dict:
    """Pre-rendered morning text with its own validity: n8n decides at SEND time whether it is still fresh."""
    f, why = select_forecast(now, cfg)
    if f:
        tgt = datetime.strptime(f["target_date"], "%Y-%m-%d").strftime("%a %d %b")
        kind = "unchanged-price baseline (equals the price when issued)" if f["baseline"] else "model forecast"
        text = (f"GOLD morning outlook: settlement forecast for {tgt} is {alerts.usd(f['predicted'])} (80% range {alerts.usd(f['low'], 0)}–{alerts.usd(f['high'], 0)}), "
                f"{kind}, issued {f['issued_label']}. {_levels_line(f, cfg)} {f['performance']} Forecast of the close, not a touch of any level; not a recommendation. Review: {url}")
        valid_until = min(f["target_epoch"], f["issued_at"] + cfg["forecast_max_age_h"].get(f["task"], 36) * 3600)
        stale = (f"GOLD morning outlook: the forecast issued {f['issued_label']} for {tgt} has expired and is not being repeated as new. Review: {url}")
        return {"text_fresh": text, "text_stale": stale, "valid_until": valid_until, "forecast_id": f["forecast_id"], "status": "valid"}
    return {"text_fresh": None, "valid_until": 0, "forecast_id": None, "status": "unavailable", "reason": why,
            "text_stale": f"GOLD morning outlook: no forecast is available ({why}). Nothing is being repeated as new. Review: {url}"}


def decide_morning(snap: dict, now: float, max_sync_age_h: float = 72) -> dict:
    """What n8n's morning job sends at ``now`` from a stored snapshot. (The n8n Code node implements exactly this rule; see n8n/lib/morning.js.)"""
    m = snap["morning"]
    fresh = m.get("text_fresh") is not None and now < m["valid_until"]
    text = m["text_fresh"] if fresh else m["text_stale"]
    sync_h = (now - snap["generated_at"]) / 3600
    if sync_h > max_sync_age_h:
        text += f" (App last synced {sync_h:.0f} h ago.)"
    return {"fresh": fresh, "text": text}


# ----------------------------------------------------------------------------------------------------------------- snapshot (durable hand-off)
def _morning_token(now: float, cfg: dict) -> tuple[str, float]:
    st = alerts._state_get("morning_review_token")
    if st and st["expires_at"] - now > 6 * 3600:
        return st["token"], st["expires_at"]
    token, exp = review.mint_token(max(cfg["review_token_hours"], 36), "morning", now=now)
    alerts._state_set("morning_review_token", {"token": token, "expires_at": exp}, now)
    return token, exp


def build_snapshot(now: float | None = None, fetch_quote: bool = True) -> dict:
    now = now or time.time()
    cfg = alerts.get_settings()
    token, exp = _morning_token(now, cfg)
    url = review.link(token)
    morning = morning_block(now, cfg, url)
    html = review_html("Morning outlook", "Gold morning outlook", cfg, now, exp, note=None if morning["status"] == "valid" else morning.get("reason"), fetch_quote=fetch_quote)
    return {"v": 1, "type": "snapshot", "generated_at": now,
            "schedule": {"enabled": cfg["morning_enabled"], "time": cfg["morning_time"], "tz": cfg["morning_tz"]},
            "levels": cfg["levels"], "morning": morning, "review": {"token": token, "expires_at": exp, "html": html}}


def _digest(snap: dict) -> str:
    core = {"schedule": snap["schedule"], "levels": snap["levels"], "morning": {k: snap["morning"].get(k) for k in ("text_fresh", "text_stale", "valid_until", "forecast_id", "status")}}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def push_snapshot(now: float | None = None, force: bool = False, transport: Transport | None = None, fetch_quote: bool = True) -> dict:
    """Send the latest forecast + morning text + schedule to n8n. Skipped when nothing changed (and re-sent at least every 6 h)."""
    now = now or time.time()
    snap = build_snapshot(now, fetch_quote=fetch_quote)
    digest = _digest(snap)
    last = alerts._state_get("last_handoff") or {}
    if not force and last.get("digest") == digest and now - last.get("at", 0) < 6 * 3600 and last.get("state") in ("ok", "simulated"):
        return {"state": "unchanged", "digest": digest}
    d = describe()
    if d["mode"] == "simulate":
        out = {"at": now, "state": "simulated", "digest": digest, "detail": "simulation: not sent"}
    elif not d["configured"]:
        out = {"at": now, "state": "failed", "digest": digest, "detail": f"not configured: {', '.join(d['missing'])}"}
    else:
        try:
            code, data = _call("POST", "gold-snapshot", snap, transport)
            out = {"at": now, "state": "ok" if code < 300 and data.get("ok") else "failed", "digest": digest, "detail": f"HTTP {code}"}
        except Exception as exc:  # noqa: BLE001
            out = {"at": now, "state": "failed", "digest": digest, "detail": f"{exc.__class__.__name__}"}
    alerts._state_set("last_handoff", out, now)
    alerts.log("handoff", out["state"], out["detail"], now)
    return out


# ----------------------------------------------------------------------------------------------------------------- alert delivery
def _record(event_id: int, now: float, mode: str, state: str, sid: str | None, detail: str, accepted: bool) -> None:
    db.connect().execute("INSERT INTO alert_deliveries (event_id, kind, created_at, mode, state, provider_sid, accepted_at, detail) VALUES (?,?,?,?,?,?,?,?)",
                         (event_id, "immediate", now, mode, state, sid, now if accepted else None, detail))
    db.connect().execute("UPDATE alert_events SET sms_state=? WHERE id=?", (state, event_id))


def deliver_pending(now: float | None = None, transport: Transport | None = None) -> dict:
    """Hand every pending alert to n8n (or simulate it). Expired alerts are not sent. 'accepted' is NOT 'delivered'."""
    now = now or time.time()
    cfg = alerts.get_settings()
    d = describe()
    out = {"sent": 0, "simulated": 0, "expired": 0, "failed": 0}
    for r in db.rows("SELECT id FROM alert_events WHERE sms_state='pending' ORDER BY id"):
        e = alerts.get_event(r["id"])
        assert e is not None
        if now > e["expires_at"]:
            db.connect().execute("UPDATE alert_events SET sms_state='expired' WHERE id=?", (e["id"],))
            alerts.log("handoff", "expired", f"event {e['id']} ({e['alert_type']} {e['threshold']:g}) not sent: past its expiry", now)
            out["expired"] += 1
            continue
        token, exp = review.mint_token(cfg["review_token_hours"], e["alert_type"], e["id"], now)
        url = review.link(token)
        if d["mode"] == "simulate":
            _record(e["id"], now, "simulate", "simulated", None, "simulation: no SMS sent", False)
            alerts.log("handoff", "simulated", f"event {e['id']} would SMS: {sms_text(e, url)[:140]}", now)
            out["simulated"] += 1
            continue
        if not d["configured"]:
            _record(e["id"], now, "live", "failed", None, f"not configured: {', '.join(d['missing'])}", False)
            out["failed"] += 1
            continue
        kind_label = "Forecast heads-up (settlement forecast)" if e["alert_type"] == "forecast_headsup" else "Actual-price alert (observed quote)"
        html = review_html(kind_label, "Gold review", cfg, now, exp, forecast=None)
        body = {"v": 1, "type": "alert", "event_id": e["id"], "alert_type": e["alert_type"], "text": sms_text(e, url), "expires_at": e["expires_at"],
                "review": {"token": token, "expires_at": exp, "html": html}}
        try:
            code, data = _call("POST", "gold-alert", body, transport)
            if code < 300 and data.get("accepted"):
                _record(e["id"], now, "live", "accepted", data.get("sid"), f"provider status {data.get('status', 'accepted')}", True)
                out["sent"] += 1
            else:
                _record(e["id"], now, "live", "failed", None, f"HTTP {code}: {str(data.get('error') or data)[:120]}", False)
                out["failed"] += 1
        except Exception as exc:  # noqa: BLE001
            _record(e["id"], now, "live", "failed", None, f"{exc.__class__.__name__}", False)
            out["failed"] += 1
    if any(out.values()):
        alerts.log("handoff", "delivery", json.dumps(out), now)
    return out


_FINAL = {"delivered": "delivered", "undelivered": "undelivered", "failed": "failed"}


def sync_status(now: float | None = None, transport: Transport | None = None) -> dict:
    """Pull provider delivery receipts that n8n collected (also while the Mac slept) and merge them into the durable log."""
    now = now or time.time()
    d = describe()
    if d["mode"] != "live" or not d["configured"]:
        return {"state": "skipped", "why": d["note"]}
    try:
        code, data = _call("GET", "gold-status", None, transport)
    except Exception as exc:  # noqa: BLE001
        alerts.log("sync", "error", exc.__class__.__name__, now)
        return {"state": "error"}
    if code >= 300:
        alerts.log("sync", "error", f"HTTP {code}", now)
        return {"state": "error"}
    n = 0
    for rec in data.get("deliveries", []):
        sid, st = rec.get("sid"), str(rec.get("status", "")).lower()
        if not sid:
            continue
        existing = db.rows("SELECT id, event_id, state FROM alert_deliveries WHERE provider_sid=?", (sid,))
        final = _FINAL.get(st)
        if not existing:
            if rec.get("kind") == "morning":
                db.connect().execute("INSERT INTO alert_deliveries (event_id, kind, created_at, mode, state, provider_sid, accepted_at, delivered_at, detail) VALUES (NULL,'morning',?,?,?,?,?,?,?)",
                                     (rec.get("at", now), "live", final or "accepted", sid, rec.get("at", now), rec.get("at", now) if final == "delivered" else None, f"morning outlook ({rec.get('note', '')})"))
                n += 1
            continue
        row = existing[0]
        if final and row["state"] != final:
            db.connect().execute("UPDATE alert_deliveries SET state=?, delivered_at=? WHERE id=?", (final, rec.get("at", now) if final == "delivered" else None, row["id"]))
            if row["event_id"]:
                db.connect().execute("UPDATE alert_events SET sms_state=? WHERE id=?", (final, row["event_id"]))
            n += 1
    alerts.log("sync", "ok", f"{n} receipt(s) merged", now)
    return {"state": "ok", "merged": n}


# ----------------------------------------------------------------------------------------------------------------- review page (local)
def local_review(token: str, now: float | None = None) -> str | None:
    """Render the review page from live data for a valid token (served by the backend itself; reachable only while the Mac is on)."""
    now = now or time.time()
    t = review.verify_token(token, now)
    if t is None:
        return None
    cfg = alerts.get_settings()
    kind = {"forecast_headsup": "Forecast heads-up (settlement forecast)", "price_crossing": "Actual-price alert (observed quote)"}.get(t["kind"], "Morning outlook")
    return review_html(kind, "Gold review", cfg, now, t["expires_at"])
