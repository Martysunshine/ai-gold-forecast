"""Sell-REVIEW notifications. These are user-selected review levels, never trading advice; nothing here buys or sells anything.

Two independent alert types, with different wording, different data and different dedupe keys:

  forecast_headsup  a newly issued *settlement (endpoint)* forecast is at or above a review level. A forecast of where the official
                    settlement may land. It says nothing about whether the intraday high touches the level.
  price_crossing    a fresh *observed* quote has crossed to or above a review level.

Alerts are read-only consumers of the forecast ledger and of quotes. They never feed back into model training.
Reliability rules (all in this file, all tested without a network):
  * dedupe: one alert per (instrument, threshold, target session, alert type, episode) - enforced by a UNIQUE key in the database;
  * re-arm: a price alert only re-arms after the price was observed BELOW the level (and, by default, in a later session);
  * stale data: a stale quote or an old/expired forecast never raises an alert, it is logged as stale;
  * expiry: an alert is not texted after its expiry (a late "alert" would read as news);
  * durable log: every check and every decision is written to SQLite (alert_log / alert_events / alert_deliveries).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, db, ledger

NY = ZoneInfo("America/New_York")
INSTRUMENT = "futures"
INSTRUMENT_NAME = "Gold futures (COMEX GC=F)"
UNIT = "USD per troy ounce"
TRADING212_URL = "https://www.trading212.com/"

DEFAULTS: dict = {
    "forecast_alerts": True,          # forecast heads-up on/off
    "price_alerts": True,             # actual-price crossing on/off
    "levels": [4000.0, 4100.0],       # user-selected review levels, USD per troy ounce
    "entry_price": 4039.96,           # the user's entry, as entered. Its instrument is NOT verified (see verify_basis)
    "position_size": None,            # optional; profit is never computed without size AND costs
    # The manually tracked Trading 212 position. The user reported it CLOSED; the app has not seen the fill, so it is "closed_unconfirmed" until
    # the user reconciles it. exit_price / closed_at / closure_reason stay empty unless the USER supplies them: nothing is inferred or invented.
    "position_status": "closed_unconfirmed",   # open | closed_unconfirmed | closed
    "exit_price": None, "closed_at": None, "closure_reason": None,
    "quote_basis": "futures",         # which observed quote the price alert watches: futures (matches the forecasts) | spot
    "immediate_sms": False,           # optional SMS the moment a new forecast first qualifies
    "price_sms": True,                # SMS for an observed crossing
    "morning_enabled": True,
    "morning_time": "07:30",
    "morning_tz": "Europe/Berlin",
    "rearm": "next_session",          # next_session | after_drop
    "rearm_drop_pct": 0.5,            # after_drop: price must fall this % below the level before the alert can re-arm in the same session
    "quote_max_age_min": {"futures": 25, "spot": 5},   # older quotes are stale: no alert is raised from them
    "forecast_max_age_h": {"fwd_1": 40, "nowcast": 3},  # older forecasts are stale: no alert is raised from them
    "event_expiry_min": 90,           # a price alert not handed to the SMS provider within this long is expired, not sent
    "review_token_hours": 24,
}
SOURCES = ("fwd_1", "nowcast")        # the "next-session" forecasts evaluated, in order of preference


class SettingsError(ValueError):
    pass


# ----------------------------------------------------------------------------------------------------------------- settings
def get_settings() -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    for r in db.rows("SELECT key, value FROM alert_settings"):
        if r["key"] in out:
            out[r["key"]] = json.loads(r["value"])
    return out


def validate(patch: dict) -> dict:
    """Return a cleaned copy of the recognised keys of ``patch``; raise SettingsError with a plain message otherwise."""
    clean: dict = {}
    for k, v in patch.items():
        if k not in DEFAULTS:
            continue
        if k in ("forecast_alerts", "price_alerts", "immediate_sms", "price_sms", "morning_enabled"):
            if not isinstance(v, bool):
                raise SettingsError(f"{k} must be true or false.")
            clean[k] = v
        elif k == "levels":
            if not isinstance(v, list) or not 1 <= len(v) <= 6:
                raise SettingsError("Review levels: give between 1 and 6 prices.")
            try:
                lv = sorted({round(float(x), 2) for x in v})
            except (TypeError, ValueError) as exc:
                raise SettingsError("Review levels must be numbers.") from exc
            if any(not 100 <= x <= 100000 for x in lv):
                raise SettingsError("Review levels must be USD per troy ounce between 100 and 100,000.")
            clean[k] = lv
        elif k == "position_status":
            if v not in ("open", "closed_unconfirmed", "closed"):
                raise SettingsError("position_status must be open, closed_unconfirmed or closed.")
            clean[k] = v
        elif k in ("closed_at", "closure_reason"):
            if v in (None, ""):
                clean[k] = None
            elif not isinstance(v, str) or len(v) > 200:
                raise SettingsError(f"{k} must be text of at most 200 characters.")
            else:
                clean[k] = v.strip()
        elif k in ("entry_price", "position_size", "exit_price"):
            if v in (None, ""):
                clean[k] = None
            else:
                try:
                    f = float(v)
                except (TypeError, ValueError) as exc:
                    raise SettingsError(f"{k} must be a number.") from exc
                if f <= 0:
                    raise SettingsError(f"{k} must be positive.")
                clean[k] = round(f, 4)
        elif k == "quote_basis":
            if v not in ("futures", "spot"):
                raise SettingsError("quote_basis must be futures or spot.")
            clean[k] = v
        elif k == "morning_time":
            try:
                hh, mm = str(v).split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    raise ValueError
            except ValueError as exc:
                raise SettingsError("Morning time must look like 07:30 (24-hour).") from exc
            clean[k] = f"{int(hh):02d}:{int(mm):02d}"
        elif k == "morning_tz":
            try:
                ZoneInfo(str(v))
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise SettingsError("Unknown time zone. Use an IANA name such as Europe/Berlin.") from exc
            clean[k] = str(v)
        elif k == "rearm":
            if v not in ("next_session", "after_drop"):
                raise SettingsError("rearm must be next_session or after_drop.")
            clean[k] = v
        elif k in ("rearm_drop_pct", "event_expiry_min", "review_token_hours"):
            try:
                f = float(v)
            except (TypeError, ValueError) as exc:
                raise SettingsError(f"{k} must be a number.") from exc
            if not 0 < f <= 24 * 60:
                raise SettingsError(f"{k} is out of range.")
            clean[k] = f
        elif k in ("quote_max_age_min", "forecast_max_age_h"):
            if not isinstance(v, dict):
                raise SettingsError(f"{k} must be an object.")
            clean[k] = {**DEFAULTS[k], **{a: float(b) for a, b in v.items() if a in DEFAULTS[k] and float(b) > 0}}
    return clean


def save_settings(patch: dict, now: float | None = None) -> dict:
    clean = validate(patch)
    now = now or time.time()
    for k, v in clean.items():
        db.connect().execute("INSERT INTO alert_settings (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                             (k, json.dumps(v), now))
    log("settings", "saved", ", ".join(sorted(clean)) or "no changes", now)
    return get_settings()


# ----------------------------------------------------------------------------------------------------------------- logging
def log(topic: str, outcome: str, detail: str | None = None, now: float | None = None) -> None:
    db.connect().execute("INSERT INTO alert_log (at, topic, outcome, detail) VALUES (?,?,?,?)", (now or time.time(), topic, outcome, detail))


def _state_get(key: str, default=None):
    r = db.rows("SELECT value FROM alert_state WHERE key=?", (key,))
    return json.loads(r[0]["value"]) if r else default


def _state_set(key: str, value, now: float) -> None:
    db.connect().execute("INSERT INTO alert_state (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (key, json.dumps(value), now))


# ----------------------------------------------------------------------------------------------------------------- formatting
def usd(v: float, d: int = 2) -> str:
    return f"${v:,.{d}f}"


def lvl(v: float) -> str:
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def _fmt_et(epoch: float, fmt: str = "%a %d %b %H:%M") -> str:
    return datetime.fromtimestamp(epoch, NY).strftime(fmt) + " New York"


def _fmt_tz(epoch: float, tz: str, fmt: str = "%a %d %b %H:%M") -> str:
    name = tz.split("/")[-1].replace("_", " ")
    return datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime(fmt) + f" {name}"


def session_label(date_iso: str) -> str:
    return datetime.strptime(date_iso, "%Y-%m-%d").strftime("%a %d %b %Y")


def futures_session_date(epoch: float) -> str:
    """CME gold session a moment belongs to: the session opens 18:00 New York the evening before, so +6 h gives its date."""
    return (datetime.fromtimestamp(epoch, NY) + timedelta(hours=6)).date().isoformat()


def position_open(cfg: dict) -> bool:
    return cfg.get("position_status", "closed_unconfirmed") == "open"


def entry_for_notes(cfg: dict) -> float | None:
    """The entry price only means something while the tracked position is open: a closed (or unconfirmed) position has no entry to compare with."""
    return cfg.get("entry_price") if position_open(cfg) else None


def review_phrase(cfg: dict) -> str:
    return ("Review your position; no response means no trade." if position_open(cfg) else
            "Your tracked position is recorded as closed (unconfirmed), so this is a price-level notice only. No response means no trade.")


def below_entry_note(level: float, entry: float | None) -> str | None:
    if entry is None:
        return None
    if level < entry:
        return "Below entry price"
    if level > entry:
        return "Above entry price"
    return "Equal to entry price"


def performance_line(task: str) -> str:
    """Measured model performance for the forecast that triggered an alert, in plain words (from the ledger and the walk-forward)."""
    try:
        from . import learning
        t = learning.status_payload()["tasks"][task]
    except Exception:  # noqa: BLE001 - an alert is never blocked by a missing statistic
        return "Measured performance unavailable."
    if task == "nowcast":
        return "Intraday models have not beaten the unchanged price."
    dev, fwd, cal = t.get("development"), t.get("forward") or {}, t.get("calibration") or {}
    bits = []
    if dev:
        bits.append(f"backtest error {dev['mae_vs_baseline_pct']:+.1f}% vs assuming no change, direction right {dev['direction_pct']:.0f}%")
        bits.append("no proven edge" if dev["mae_vs_baseline_pct"] > -2 else "backtest edge over no change")
    bits.append(f"{fwd.get('n_matured', 0)} live forecasts scored")
    if cal.get("historical_pct") is not None:
        bits.append(f"80% range held {cal['historical_pct']:.0f}% historically")
    return "; ".join(bits).capitalize() + "."


# ----------------------------------------------------------------------------------------------------------------- events
def create_event(*, alert_type: str, threshold: float, target_session: str, message: str, payload: dict, now: float, expires_at: float,
                 episode: int = 0, sms_requested: bool = False, instrument: str = INSTRUMENT) -> int | None:
    """Insert an alert unless one already exists for its dedupe key. Returns the new id, or None when it was a duplicate."""
    key = f"{instrument}|{threshold:g}|{target_session}|{alert_type}|{episode}"
    cur = db.connect().execute(
        "INSERT OR IGNORE INTO alert_events (dedupe_key, alert_type, instrument, threshold, target_session, created_at, expires_at, message, payload, sms_state) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (key, alert_type, instrument, float(threshold), target_session, now, expires_at, message, json.dumps(payload), "pending" if sms_requested else "not_requested"))
    return cur.lastrowid if cur.rowcount == 1 else None


def get_event(event_id: int) -> dict | None:
    r = db.rows("SELECT * FROM alert_events WHERE id=?", (event_id,))
    if not r:
        return None
    e = r[0]
    e["payload"] = json.loads(e["payload"])
    e["deliveries"] = db.rows("SELECT * FROM alert_deliveries WHERE event_id=? ORDER BY id", (event_id,))
    return e


def recent_events(n: int = 10) -> list[dict]:
    ids = [r["id"] for r in db.rows("SELECT id FROM alert_events ORDER BY id DESC LIMIT ?", (n,))]
    return [get_event(i) for i in ids]  # type: ignore[misc]


# ----------------------------------------------------------------------------------------------------------------- forecast heads-up
def _fc_target_epoch(row: dict) -> float:
    return datetime.fromisoformat(row["target_time"]).timestamp()


def headsup_message(row: dict, level: float, cfg: dict, perf: str) -> str:
    tdate = session_label(row["target_date"])
    note = below_entry_note(level, entry_for_notes(cfg))
    src = "unchanged-price baseline (not a learned prediction)" if row["algorithm"] == "unchanged" else "model forecast"
    return (f"FORECAST heads-up. The {INSTRUMENT_NAME} settlement forecast for {tdate} is {usd(row['predicted'])}, at or above your {lvl(level)} review level"
            f"{' (' + note.lower() + ')' if note else ''}. Forecast range ({round(row['level'] * 100)}%): {usd(row['low'])}–{usd(row['high'])}. "
            f"Issued {_fmt_et(row['issued_at'])} ({src}). {perf} "
            "This is a forecast of the settlement price (the close), not a statement that the price will reach or touch the level, and not a recommendation. "
            + review_phrase(cfg))


def check_forecasts(now: float | None = None) -> dict:
    """Evaluate the latest next-session forecasts against every review level. Idempotent: repeats are suppressed by the dedupe key."""
    now = now or time.time()
    cfg = get_settings()
    seen, created = [], []
    for task in SOURCES:
        row = ledger.latest(task, "champion")
        info: dict = {"task": task}
        if row is None:
            info["verdict"] = "none_issued"
            seen.append(info)
            continue
        tgt = _fc_target_epoch(row)
        age_h = (now - row["issued_at"]) / 3600
        info.update({"forecast_id": row["id"], "predicted": row["predicted"], "target_date": row["target_date"], "issued_at": row["issued_at"], "age_h": round(age_h, 2)})
        if row["instrument"] != cfg_instrument():
            info["verdict"] = "wrong_instrument"
        elif tgt <= now:
            info["verdict"] = "stale_target_passed"
        elif age_h > cfg["forecast_max_age_h"].get(task, 36):
            info["verdict"] = "stale_too_old"
        elif row["algorithm"] == "unchanged":
            info["verdict"] = "baseline_copy"   # equals the price when issued: the price-crossing alert covers it, a heads-up would repackage the quote
        else:
            info["verdict"] = "evaluated"
        seen.append(info)
        if info["verdict"] != "evaluated" or not cfg["forecast_alerts"]:
            continue
        perf = performance_line(task)
        for level in cfg["levels"]:
            if row["predicted"] < level:
                continue
            payload = {"task": task, "forecast_id": row["id"], "predicted": row["predicted"], "low": row["low"], "high": row["high"], "level": row["level"],
                       "target_date": row["target_date"], "target_time": row["target_time"], "issued_at": row["issued_at"], "threshold": level,
                       "model_version": row["model_version"], "reference_price": row["base_price"], "performance": perf, "kind": "endpoint_settlement",
                       "below_entry": below_entry_note(level, entry_for_notes(cfg))}
            eid = create_event(alert_type="forecast_headsup", threshold=level, target_session=row["target_date"], message=headsup_message(row, level, cfg, perf),
                               payload=payload, now=now, expires_at=tgt, sms_requested=cfg["immediate_sms"])
            if eid is None:
                log("forecast_check", "duplicate", f"{task} {row['target_date']} level {level:g} already alerted", now)
            else:
                created.append(eid)
                log("forecast_check", "fired", f"{task} {row['id']} predicted {row['predicted']} >= {level:g}", now)
    state = {"at": now, "enabled": cfg["forecast_alerts"], "forecasts": seen, "new_events": created}
    _state_set("last_forecast_check", state, now)
    log("forecast_check", "checked" if cfg["forecast_alerts"] else "off", json.dumps([f"{s['task']}:{s['verdict']}" for s in seen]), now)
    return state


def cfg_instrument() -> str:
    return INSTRUMENT


# ----------------------------------------------------------------------------------------------------------------- actual-price crossing
def fetch_quote(basis: str) -> dict:
    """The latest observed quote on the chosen basis: {price, as_of, source, instrument}. Raises on failure."""
    from . import quote
    raw = quote._fetch_yahoo() if basis == "futures" else quote._fetch_spot()
    if raw.get("tick_epoch") is None:
        raise quote.QuoteUnavailable("the provider gave no quote time")
    return {"price": float(raw["price"]), "as_of": float(raw["tick_epoch"]), "source": raw.get("source", basis),
            "instrument": raw.get("instrument", INSTRUMENT_NAME if basis == "futures" else "Spot gold (XAU/USD)"), "basis": basis,
            "currency": raw.get("currency") or ("USD" if basis == "spot" else None), "exchange": raw.get("exchange")}


def price_message(q: dict, level: float, cfg: dict, first_sight: bool) -> str:
    basis = "delayed quote (~10 min behind)" if q["basis"] == "futures" else "spot quote"
    note = below_entry_note(level, entry_for_notes(cfg))
    return (f"ACTUAL PRICE alert (observed quote, not a forecast). {q['instrument']}, {basis}: {usd(q['price'])} as of {_fmt_et(q['as_of'], '%a %d %b %H:%M')}, "
            f"{'already ' if first_sight else 'crossed '}at or above your {lvl(level)} review level"
            f"{' (' + note.lower() + ')' if note else ''}. {review_phrase(cfg)}")


def check_price(now: float | None = None, quote: dict | None = None) -> dict:
    """Independent of forecasts: has a FRESH observed quote crossed to or above a review level?"""
    now = now or time.time()
    cfg = get_settings()
    basis = cfg["quote_basis"]
    out: dict = {"at": now, "basis": basis, "enabled": cfg["price_alerts"], "new_events": []}
    if not cfg["price_alerts"]:
        log("price_check", "off", None, now)
        return out
    try:
        q = quote or fetch_quote(basis)
    except Exception as exc:  # noqa: BLE001 - provider failure must not stop the worker
        out["verdict"] = "quote_unavailable"
        log("price_check", "error", f"{exc.__class__.__name__}: {exc}", now)
        _state_set("last_price_check", out, now)
        return out
    age_min = (now - q["as_of"]) / 60
    out.update({"price": q["price"], "as_of": q["as_of"], "age_min": round(age_min, 1)})
    if age_min > cfg["quote_max_age_min"].get(basis, 25):
        out["verdict"] = "stale"
        log("price_check", "stale", f"quote is {age_min:.0f} min old (limit {cfg['quote_max_age_min'].get(basis)} min): no alert raised from it", now)
        _state_set("last_price_check", out, now)
        return out
    session = futures_session_date(q["as_of"])
    for level in cfg["levels"]:
        key = f"price:{basis}:{level:g}"
        st = _state_get(key, {"armed": True, "episode": 0, "fired_session": None, "seen": False})
        above = q["price"] >= level
        if not st["armed"]:
            dropped = q["price"] < level
            deep = q["price"] < level * (1 - cfg["rearm_drop_pct"] / 100)
            new_session = st["fired_session"] is not None and session != st["fired_session"]
            if (cfg["rearm"] == "after_drop" and deep) or (cfg["rearm"] == "next_session" and dropped and new_session):
                st.update({"armed": True, "episode": st["episode"] + 1})
                log("price_check", "rearmed", f"level {level:g} (episode {st['episode']})", now)
        if st["armed"] and above:
            eid = create_event(alert_type="price_crossing", threshold=level, target_session=session, episode=st["episode"], now=now, sms_requested=cfg["price_sms"],
                               expires_at=now + cfg["event_expiry_min"] * 60, message=price_message(q, level, cfg, first_sight=not st["seen"]),
                               payload={"price": q["price"], "as_of": q["as_of"], "basis": basis, "source": q["source"], "threshold": level, "first_sight": not st["seen"],
                                        "session": session, "below_entry": below_entry_note(level, entry_for_notes(cfg))})
            st.update({"armed": False, "fired_session": session})
            if eid is None:
                log("price_check", "duplicate", f"level {level:g} session {session}", now)
            else:
                out["new_events"].append(eid)
                log("price_check", "fired", f"{q['price']} >= {level:g}", now)
        st["seen"] = True
        _state_set(key, st, now)
    out["verdict"] = "checked"
    _state_set("last_price_check", out, now)
    log("price_check", "checked", f"{q['price']} ({basis}, {age_min:.0f} min old)", now)
    return out


# ----------------------------------------------------------------------------------------------------------------- basis verification
def _currency_line(fut: dict, spot: dict) -> str:
    fc = fut.get("currency")
    if fc:
        return f"{fc} as reported by the futures provider ({fut.get('exchange') or 'exchange n/a'}); the unit is the contract's quote convention, US dollars per troy ounce"
    return "not reported by the provider this time; COMEX gold futures are quoted in US dollars per troy ounce"

def verify_basis(now: float | None = None, quotes: dict | None = None) -> dict:
    """Check what the review levels actually apply to. Returns plain facts and warnings; never assumes the entry price's instrument."""
    now = now or time.time()
    cfg = get_settings()
    quotes = quotes or {}
    got: dict = {}
    for b in ("futures", "spot"):
        try:
            got[b] = quotes.get(b) or fetch_quote(b)
        except Exception as exc:  # noqa: BLE001
            got[b] = {"error": f"{exc.__class__.__name__}"}
    fut, spot = got["futures"], got["spot"]
    warn: list[str] = []
    ref = (fut if "price" in fut else spot if "price" in spot else None)
    entry = cfg.get("entry_price")
    if ref and entry and position_open(cfg):
        gap = (entry / ref["price"] - 1) * 100
        if abs(gap) > 3:
            warn.append(f"Your entry price {usd(entry)} is {gap:+.1f}% away from the current {ref['basis']} quote {usd(ref['price'])}. "
                        "That is plausible for an earlier purchase, but it also fits a different instrument, currency or unit. It cannot be verified here.")
    for level in cfg["levels"]:
        if ref and not 0.7 <= level / ref["price"] <= 1.4:
            warn.append(f"Review level {lvl(level)} is far from the current price {usd(ref['price'])}: check it is USD per troy ounce.")
    for k, q in (("futures", fut), ("spot", spot)):
        if q.get("currency") and q["currency"] != "USD":
            warn.append(f"The {k} provider reports its currency as {q['currency']}, not USD: the review levels would be in the wrong currency.")
    basis_gap = None
    if "price" in fut and "price" in spot:
        basis_gap = round(spot["price"] - fut["price"], 2)
    return {
        "levels_apply_to": {"instrument": INSTRUMENT_NAME, "currency": "USD", "unit": UNIT, "forecast_target": "official 13:30 New York settlement (endpoint), not the intraday high",
                            "price_alert_quote": cfg["quote_basis"], "price_alert_quote_note": "delayed ~10 minutes" if cfg["quote_basis"] == "futures" else "spot XAU/USD, seconds-fresh"},
        "currency_checked": _currency_line(fut, spot),
        "quotes": {k: ({"price": v["price"], "as_of": v["as_of"], "age_min": round((now - v["as_of"]) / 60, 1), "source": v["source"]} if "price" in v else v) for k, v in got.items()},
        "spot_minus_futures": basis_gap,
        "entry_price": {"value": entry, "verified": False, "note": "Taken as entered. This app cannot see which Trading 212 instrument (CFD, ETC, currency) your entry refers to."
                        + ("" if position_open(cfg) else " The tracked position is recorded as closed, so the entry is history, not a comparison point."), },
        "position": position_info(cfg),
        "warnings": warn,
    }


# ----------------------------------------------------------------------------------------------------------------- UI payload
def position_info(cfg: dict) -> dict:
    st = cfg.get("position_status", "closed_unconfirmed")
    note = {"open": "Tracked as open at the entry price you entered (its instrument is not verified).",
            "closed_unconfirmed": "You reported this position closed. The app has not seen the fill, so the exit price, the realised result and the reason are NOT recorded and nothing is assumed about them.",
            "closed": "Recorded as closed. Only what you entered is shown; profit is not computed."}[st]
    return {"status": st, "entry_price": cfg.get("entry_price"), "exit_price": cfg.get("exit_price"), "closed_at": cfg.get("closed_at"), "closure_reason": cfg.get("closure_reason"), "note": note}


def panel(now: float | None = None) -> dict:
    from . import n8n_link
    now = now or time.time()
    cfg = get_settings()
    ev = recent_events(8)
    last = ev[0] if ev else None
    return {
        "settings": cfg,
        "levels": [{"level": x, "note": below_entry_note(x, entry_for_notes(cfg))} for x in cfg["levels"]],
        "position": position_info(cfg),
        "last_forecast_check": _state_get("last_forecast_check"),
        "last_price_check": _state_get("last_price_check"),
        "last_alert": last,
        "recent": ev,
        "delivery": n8n_link.describe(),
        "last_handoff": _state_get("last_handoff"),
        "log": db.rows("SELECT at, topic, outcome, detail FROM alert_log ORDER BY id DESC LIMIT 12"),
        "labels": {"forecast_kind": "Settlement (endpoint) forecast: where the official 13:30 New York settlement may land. Not the intraday high.",
                   "entry_note": "Review levels are your own; this is not a trading recommendation. Profit is not computed: position size and costs are unknown."},
    }
