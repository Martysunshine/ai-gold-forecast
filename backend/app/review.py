"""The page an SMS link opens: forecast, latest quote, position information and a Trading 212 link. Read-only. No response means no trade.

The page is a bearer-token page: the link carries an unguessable, expiring token. The same HTML is served by this backend
(/review?t=...) and, because a sleeping Mac cannot serve anything, is also handed to n8n so the hosted workflow can serve the same
page while the Mac is off. Everything dynamic is HTML-escaped.
"""
from __future__ import annotations

import secrets
import time
from html import escape

from . import alerts, config, db

CSS = ("body{font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0b0e12;color:#e6eaee;margin:0;padding:20px}main{max-width:560px;margin:0 auto}"
       "h1{font-size:20px;margin:0 0 4px}h2{font-size:12px;letter-spacing:.09em;color:#9aa3af;margin:22px 0 6px;text-transform:uppercase}"
       ".card{background:#0f1318;border:1px solid #1b2128;border-radius:14px;padding:14px 16px}.big{font:600 30px ui-monospace,Menlo,monospace}"
       ".muted{color:#9aa3af;font-size:13px}.tag{display:inline-block;border:1px solid #3a434e;border-radius:999px;padding:1px 9px;font-size:12px;color:#d6b878}"
       "dl{display:grid;grid-template-columns:140px 1fr;gap:6px 10px;margin:8px 0 0;font-size:14px}dt{color:#9aa3af}dd{margin:0}"
       "a.btn{display:inline-block;margin-top:10px;padding:10px 16px;border:1px solid #d6b878;border-radius:999px;color:#d6b878;text-decoration:none}"
       ".warn{color:#e7b04f}")


def mint_token(hours: float, kind: str, event_id: int | None = None, now: float | None = None) -> tuple[str, float]:
    now = now or time.time()
    token = secrets.token_urlsafe(32)
    exp = now + hours * 3600
    db.connect().execute("INSERT INTO review_tokens (token, created_at, expires_at, event_id, kind) VALUES (?,?,?,?,?)", (token, now, exp, event_id, kind))
    return token, exp


def verify_token(token: str, now: float | None = None) -> dict | None:
    now = now or time.time()
    r = db.rows("SELECT * FROM review_tokens WHERE token=?", (token or "",))
    return r[0] if r and r[0]["expires_at"] > now else None


def review_base() -> str:
    if config.ALERT_REVIEW_BASE_URL:
        return config.ALERT_REVIEW_BASE_URL
    return f"{config.N8N_WEBHOOK_BASE}/gold-review" if config.N8N_WEBHOOK_BASE else "http://localhost:8742/review"


def link(token: str) -> str:
    return f"{review_base()}?t={token}"


def _row(k: str, v: str) -> str:
    return f"<dt>{escape(k)}</dt><dd>{v}</dd>"


def render(ctx: dict) -> str:
    """ctx: {title, kind_label, forecast|None, quote|None, settings, generated_at, expires_at, note|None, event|None}."""
    cfg = ctx["settings"]
    f, q = ctx.get("forecast"), ctx.get("quote")
    parts = [f"<h1>{escape(ctx['title'])}</h1><p class='muted'>{escape(ctx['kind_label'])}. Review levels are your own; this is not a recommendation. No response means no trade.</p>"]
    if ctx.get("note"):
        parts.append(f"<p class='warn'>{escape(ctx['note'])}</p>")
    parts.append("<h2>Settlement forecast (endpoint)</h2><div class='card'>")
    if f:
        base = "<span class='tag'>Unchanged-price baseline</span>" if f.get("baseline") else "<span class='tag'>Model forecast</span>"
        parts.append(f"<div class='big'>{escape(alerts.usd(f['predicted']))}</div> {base}"
                     "<dl>" + _row("Instrument", escape(alerts.INSTRUMENT_NAME) + f" · {escape(alerts.UNIT)}") + _row("Target", escape(f["target_label"]))
                     + _row("Prediction range", f"{escape(alerts.usd(f['low']))}–{escape(alerts.usd(f['high']))} ({escape(str(f['level_pct']))}%)")
                     + _row("Reference", escape(alerts.usd(f["reference_price"])) + " when issued")
                     + _row("Issued", escape(f["issued_label"])) + _row("Measured", escape(f.get("performance", "")))
                     + "</dl><p class='muted'>This forecasts where the official settlement may land. It does not say the price will reach or touch any level.</p>")
    else:
        parts.append("<p class='warn'>No valid forecast is available (none issued, or the last one is stale). It is not being repeated as new.</p>")
    parts.append("</div><h2>Latest observed quote</h2><div class='card'>")
    if q:
        parts.append(f"<div class='big'>{escape(alerts.usd(q['price']))}</div><dl>" + _row("Basis", escape(q["basis_label"])) + _row("As of", escape(q["as_of_label"]))
                     + _row("Age at this page", escape(q["age_label"])) + "</dl>")
    else:
        parts.append("<p class='warn'>No quote is available.</p>")
    parts.append("</div><h2>Your position and review levels</h2><div class='card'><dl>")
    entry = cfg.get("entry_price")
    pos = alerts.position_info(cfg)
    if alerts.position_open(cfg):
        parts.append(_row("Entry price", (escape(alerts.usd(entry)) + " <span class='muted'>(as entered; instrument not verified)</span>") if entry else "not set"))
    else:
        parts.append(_row("Tracked position", "Closed (unconfirmed)<br><span class='muted'>" + escape(pos["note"]) + "</span>"))
        if entry:
            parts.append(_row("Last entry recorded", escape(alerts.usd(entry)) + " <span class='muted'>(history; not reconciled)</span>"))
        if pos["exit_price"]:
            parts.append(_row("Exit price (you entered)", escape(alerts.usd(pos["exit_price"]))))
    if cfg.get("position_size"):
        parts.append(_row("Position size", escape(str(cfg["position_size"]))))
    for lv in cfg["levels"]:
        note = alerts.below_entry_note(lv, alerts.entry_for_notes(cfg))
        parts.append(_row("Review level", escape(alerts.lvl(lv)) + (f" <span class='tag'>{escape(note)}</span>" if note else "")))
    if q and entry and alerts.position_open(cfg):
        d = q["price"] - entry
        parts.append(_row("Quote vs entry", f"{d:+,.2f} USD per troy ounce <span class='muted'>(a price difference, not profit)</span>"))
    parts.append("</dl><p class='muted'>Profit is not calculated: it needs your position size and your broker's costs.</p></div>")
    parts.append(f"<p><a class='btn' href='{escape(alerts.TRADING212_URL)}' rel='noreferrer'>Open Trading 212</a></p>")
    parts.append(f"<p class='muted'>Page generated {escape(ctx['generated_label'])}. The quote above is as of that time. Link expires {escape(ctx['expires_label'])}.</p>")
    return f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'><title>Gold review</title><style>{CSS}</style></head><body><main>{''.join(parts)}</main></body></html>"
