"""Alert pipeline tests: simulated prices and forecasts only. No test may reach the network or send an SMS (httpx is patched to fail loudly)."""
import json
import time
from pathlib import Path

import httpx
import pytest

from app import alerts, alerts_sim, config, db, ledger, n8n_link, review
from conftest import block_network

HERE = Path(__file__).resolve().parent
NOW = 1_795_000_000.0   # a fixed "now" (2026-11-18 ~09:46 UTC)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ALERTS_MODE", "simulate")
    monkeypatch.setattr(config, "N8N_WEBHOOK_BASE", "")
    monkeypatch.setattr(config, "N8N_AUTH_SECRET", "")
    monkeypatch.setattr(config, "ALERT_REVIEW_BASE_URL", "")
    db._inited.discard(str(db.path()))
    block_network(monkeypatch)          # in-process ASGI calls are unaffected; any real connection fails loudly
    yield


def fc(predicted, task="fwd_1", algorithm="ridge", **kw):
    return alerts_sim.issue_sim_forecast(predicted, NOW, task=task, algorithm=algorithm, **kw)


def quote(price, age_min=2, basis="futures"):
    return {"price": price, "as_of": NOW - age_min * 60, "source": "simulated", "instrument": alerts.INSTRUMENT_NAME, "basis": basis}


def events(kind=None):
    return [e for e in alerts.recent_events(50) if kind is None or e["alert_type"] == kind]


# ------------------------------------------------------------------------------------------------------ settings
def test_defaults_are_the_requested_configuration():
    s = alerts.get_settings()
    assert s["levels"] == [4000.0, 4100.0] and s["entry_price"] == 4039.96
    assert (s["morning_time"], s["morning_tz"]) == ("07:30", "Europe/Berlin")
    assert s["forecast_alerts"] and s["price_alerts"] and s["immediate_sms"] is False


def test_settings_validate_and_persist():
    s = alerts.save_settings({"levels": [4100, 3950.5, 4100], "morning_time": "6:05", "morning_tz": "America/New_York", "forecast_alerts": False, "entry_price": "4000"})
    assert s["levels"] == [3950.5, 4100.0] and s["morning_time"] == "06:05" and s["forecast_alerts"] is False and s["entry_price"] == 4000.0
    assert alerts.get_settings()["morning_tz"] == "America/New_York"
    for bad in ({"levels": []}, {"levels": ["x"]}, {"levels": [5]}, {"morning_time": "25:00"}, {"morning_tz": "Mars/Base"}, {"forecast_alerts": "yes"}, {"quote_basis": "bank"}, {"entry_price": -1}):
        with pytest.raises(alerts.SettingsError):
            alerts.save_settings(bad)


def test_below_entry_label():
    assert alerts.below_entry_note(4000, 4039.96) == "Below entry price"
    assert alerts.below_entry_note(4100, 4039.96) == "Above entry price"
    assert alerts.below_entry_note(4100, None) is None


# ------------------------------------------------------------------------------------------------------ forecast heads-up
def test_headsup_fires_for_each_level_the_forecast_reaches():
    fc(4120)
    r = alerts.check_forecasts(NOW)
    assert len(r["new_events"]) == 2
    assert {e["threshold"] for e in events("forecast_headsup")} == {4000.0, 4100.0}


def test_headsup_only_for_levels_reached_and_never_below():
    alerts.save_settings({"position_status": "open"}, NOW)
    fc(4050)
    alerts.check_forecasts(NOW)
    assert [e["threshold"] for e in events()] == [4000.0]
    assert events()[0]["payload"]["below_entry"] == "Below entry price"


def test_headsup_message_is_honest():
    fc(4120, half_range=60)
    alerts.check_forecasts(NOW)
    msg = next(e["message"] for e in events() if e["threshold"] == 4100.0)
    assert "FORECAST heads-up" in msg and "$4,120.00" in msg and "above" not in msg.split("your")[0].lower().replace("at or above", "")
    assert "$4,060.00–$4,180.00" in msg                      # forecast range
    assert "Gold futures (COMEX GC=F)" in msg                # instrument
    assert "Issued" in msg and "New York" in msg              # issue time
    assert "no response means no trade" in msg.lower()
    assert "not a statement that the price will reach or touch" in msg
    low = msg.lower()
    assert "will reach" not in low.replace("not a statement that the price will reach or touch the level", "") and "definitely" not in low
    assert "buy" not in low and "sell" not in low.replace("settlement", "")


def test_headsup_dedupes_across_repeated_checks_and_new_forecasts_for_the_same_session():
    fc(4120)
    alerts.check_forecasts(NOW)
    alerts.check_forecasts(NOW + 60)
    assert len(events()) == 2
    fc(4150, base=4140)                    # a different forecast row, same target session -> still the same dedupe key
    alerts.check_forecasts(NOW + 120)
    assert len(events()) == 2
    assert any(r["outcome"] == "duplicate" for r in db.rows("SELECT outcome FROM alert_log"))


def test_headsup_ignores_unchanged_price_baseline_copies():
    fc(4200, task="nowcast", algorithm="unchanged", target_in_h=4)
    r = alerts.check_forecasts(NOW)
    assert events() == []
    assert any(f["verdict"] == "baseline_copy" for f in r["forecasts"])


def test_headsup_never_from_stale_or_finished_forecasts():
    fc(4120, issued_ago_h=50)               # older than the 40 h limit
    r = alerts.check_forecasts(NOW)
    assert events() == [] and r["forecasts"][0]["verdict"] == "stale_too_old"
    alerts_sim.issue_sim_forecast(4130, NOW - 3600 * 30, base=4100, target_in_h=-3)  # target already passed
    r = alerts.check_forecasts(NOW)
    assert events() == []


def test_headsup_off_switch_still_records_the_check():
    alerts.save_settings({"forecast_alerts": False})
    fc(4120)
    r = alerts.check_forecasts(NOW)
    assert events() == [] and r["enabled"] is False
    assert alerts.panel(NOW)["last_forecast_check"]["at"] == NOW


def test_immediate_sms_is_optional():
    fc(4120)
    alerts.check_forecasts(NOW)
    assert {e["sms_state"] for e in events()} == {"not_requested"}      # default: included in the morning outlook, no immediate SMS
    alerts.save_settings({"immediate_sms": True})
    alerts_sim.issue_sim_forecast(4300, NOW + 600, base=4290, target_in_h=44)  # a newer forecast for a later session
    alerts.check_forecasts(NOW + 700)
    assert "pending" in {e["sms_state"] for e in events()}


def test_endpoint_forecast_is_labelled_as_an_endpoint_not_a_touch():
    p = alerts.panel(NOW)
    assert "Not the intraday high" in p["labels"]["forecast_kind"]


# ------------------------------------------------------------------------------------------------------ actual-price crossing
def test_price_crossing_fires_and_is_distinct_from_the_forecast_alert():
    r = alerts.check_price(NOW, quote=quote(4101))
    ev = events("price_crossing")
    assert {e["threshold"] for e in ev} == {4000.0, 4100.0} and r["verdict"] == "checked"
    msg = ev[0]["message"]
    assert msg.startswith("ACTUAL PRICE alert (observed quote, not a forecast)") and "FORECAST" not in msg
    assert "delayed quote" in msg and "no response means no trade" in msg.lower()


def test_price_below_levels_raises_nothing():
    alerts.check_price(NOW, quote=quote(3850))
    assert events() == []


def test_stale_quote_never_alerts():
    r = alerts.check_price(NOW, quote=quote(4150, age_min=60))
    assert events() == [] and r["verdict"] == "stale"
    assert any(x["outcome"] == "stale" for x in db.rows("SELECT outcome FROM alert_log"))
    alerts.save_settings({"quote_basis": "spot"})
    assert alerts.check_price(NOW, quote=quote(4150, age_min=10, basis="spot"))["verdict"] == "stale"     # spot allows only 5 min


def test_actual_crossing_from_below_then_no_repeat_while_above():
    alerts.save_settings({"levels": [4100]})
    alerts.check_price(NOW, quote=quote(4090))
    alerts.check_price(NOW + 120, quote=quote(4101))
    first = len(events("price_crossing"))
    assert [e["threshold"] for e in events("price_crossing")] == [4100.0]
    assert events("price_crossing")[0]["payload"]["first_sight"] is False
    alerts.check_price(NOW + 240, quote=quote(4120))
    alerts.check_price(NOW + 360, quote=quote(4105))
    assert len(events("price_crossing")) == first


def test_rearm_next_session_requires_a_drop_below_and_a_later_session():
    alerts.save_settings({"levels": [4100]})
    alerts.check_price(NOW, quote=quote(4101))
    alerts.check_price(NOW + 600, quote=quote(4050))                       # dropped, same session: not re-armed
    alerts.check_price(NOW + 1200, quote=quote(4102))
    assert len(events("price_crossing")) == 1
    later = NOW + 86400
    alerts.check_price(later, quote={**quote(4050), "as_of": later - 120})  # dropped in a later session: re-arms (episode 1)
    alerts.check_price(later + 600, quote={**quote(4103), "as_of": later + 480})
    assert len(events("price_crossing")) == 2


def test_rearm_after_drop_allows_a_same_session_second_alert_with_a_distinct_dedupe_key():
    alerts.save_settings({"levels": [4100], "rearm": "after_drop", "rearm_drop_pct": 0.5})
    alerts.check_price(NOW, quote=quote(4101))
    alerts.check_price(NOW + 600, quote=quote(4090))                        # only 0.24% below: stays disarmed
    alerts.check_price(NOW + 700, quote=quote(4102))
    assert len(events("price_crossing")) == 1
    alerts.check_price(NOW + 900, quote=quote(4070))                        # 0.73% below: re-arms
    alerts.check_price(NOW + 1000, quote=quote(4103))
    keys = [e["id"] for e in events("price_crossing")]
    assert len(keys) == 2
    assert len({r["dedupe_key"] for r in db.rows("SELECT dedupe_key FROM alert_events")}) == 2


def test_first_sight_above_is_flagged_not_pretended_to_be_a_fresh_crossing():
    alerts.check_price(NOW, quote=quote(4120))
    assert all(e["payload"]["first_sight"] for e in events("price_crossing"))
    assert "already at or above" in events("price_crossing")[0]["message"]


def test_quote_provider_failure_is_logged_not_raised(monkeypatch):
    def boom(basis):
        raise RuntimeError("provider down")
    monkeypatch.setattr(alerts, "fetch_quote", boom)
    r = alerts.check_price(NOW)
    assert r["verdict"] == "quote_unavailable" and events() == []
    assert db.rows("SELECT outcome FROM alert_log WHERE topic='price_check'")[0]["outcome"] == "error"


def test_each_alert_type_is_independent_of_the_other():
    alerts.save_settings({"forecast_alerts": False})
    alerts.check_price(NOW, quote=quote(4101))
    assert events("price_crossing") and not events("forecast_headsup")
    alerts.save_settings({"forecast_alerts": True, "price_alerts": False})
    fc(4120)
    alerts.check_forecasts(NOW)
    r = alerts.check_price(NOW + 10, quote=quote(4300))
    assert r["enabled"] is False and events("forecast_headsup") and len(events("price_crossing")) == 2


def test_alerts_never_change_forecasts_outcomes_or_models():
    from app import registry
    registry.seed()
    fc(4120)
    snap = lambda: (db.rows("SELECT id, predicted FROM forecasts"), db.rows("SELECT * FROM outcomes"), db.rows("SELECT version, status, last_trained_at, trained_through FROM models ORDER BY version"))  # noqa: E731
    before = snap()
    alerts.check_forecasts(NOW)
    alerts.check_price(NOW, quote=quote(4150))
    n8n_link.deliver_pending(NOW)
    n8n_link.push_snapshot(NOW, fetch_quote=False)
    assert snap() == before


# ------------------------------------------------------------------------------------------------------ delivery
def test_simulation_never_sends_and_records_it():
    alerts.check_price(NOW, quote=quote(4101))
    out = n8n_link.deliver_pending(NOW)
    assert out["simulated"] == 2 and out["sent"] == 0
    assert {e["sms_state"] for e in events()} == {"simulated"}
    assert {d["mode"] for d in db.rows("SELECT mode FROM alert_deliveries")} == {"simulate"}


def test_live_mode_without_credentials_fails_visibly(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_MODE", "live")
    alerts.check_price(NOW, quote=quote(4101))
    out = n8n_link.deliver_pending(NOW)
    assert out["failed"] == 2
    d = db.rows("SELECT state, detail FROM alert_deliveries")[0]
    assert d["state"] == "failed" and "N8N_WEBHOOK_BASE" in d["detail"]
    assert "N8N_WEBHOOK_BASE" in n8n_link.describe()["note"]


def _live(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_MODE", "live")
    monkeypatch.setattr(config, "N8N_WEBHOOK_BASE", "https://example.invalid/webhook")
    monkeypatch.setattr(config, "N8N_AUTH_SECRET", "s3cret")


def test_live_hand_off_accepted_is_not_delivered_and_carries_review_link(monkeypatch):
    _live(monkeypatch)
    sent = []

    def transport(method, url, headers, body):
        sent.append((method, url, headers, body))
        return 200, {"accepted": True, "sid": "SM123", "status": "queued"}

    alerts.save_settings({"levels": [4100]})
    alerts.check_price(NOW, quote=quote(4101))
    out = n8n_link.deliver_pending(NOW, transport=transport)
    assert out["sent"] == 1
    method, url, headers, body = sent[0]
    assert url == "https://example.invalid/webhook/gold-alert" and headers["X-Gold-Secret"] == "s3cret"
    assert body["type"] == "alert" and "https://example.invalid/webhook/gold-review?t=" in body["text"]
    assert body["review"]["token"] in body["text"] and "<html" in body["review"]["html"]
    assert "phone" not in json.dumps(body).lower()                      # the backend never holds the number
    e = events()[0]
    assert e["sms_state"] == "accepted" and e["deliveries"][0]["provider_sid"] == "SM123" and e["deliveries"][0]["delivered_at"] is None


def test_provider_receipts_confirm_delivery_separately(monkeypatch):
    _live(monkeypatch)
    alerts.save_settings({"levels": [4100]})
    alerts.check_price(NOW, quote=quote(4101))
    n8n_link.deliver_pending(NOW, transport=lambda *a: (200, {"accepted": True, "sid": "SM9", "status": "queued"}))
    status = {"deliveries": [{"sid": "SM9", "status": "delivered", "at": NOW + 30}, {"sid": "SMmorning", "status": "delivered", "kind": "morning", "at": NOW - 7200, "note": "fresh forecast"}]}
    assert n8n_link.sync_status(NOW + 60, transport=lambda *a: (200, status))["merged"] == 2
    e = events()[0]
    assert e["sms_state"] == "delivered" and e["deliveries"][0]["delivered_at"] == NOW + 30
    assert db.rows("SELECT state FROM alert_deliveries WHERE kind='morning'")[0]["state"] == "delivered"
    # a failed receipt downgrades nothing silently
    n8n_link.sync_status(NOW + 90, transport=lambda *a: (200, {"deliveries": [{"sid": "SM9", "status": "undelivered", "at": NOW + 80}]}))
    assert events()[0]["sms_state"] == "undelivered"


def test_provider_error_and_unreachable_n8n_are_recorded_as_failed(monkeypatch):
    _live(monkeypatch)
    alerts.save_settings({"levels": [4100]})
    alerts.check_price(NOW, quote=quote(4101))

    def down(*a):
        raise httpx.ConnectError("no route")
    assert n8n_link.deliver_pending(NOW, transport=down)["failed"] == 1
    assert events()[0]["sms_state"] == "failed"


def test_expired_alerts_are_not_sent_late(monkeypatch):
    alerts.check_price(NOW, quote=quote(4101))
    out = n8n_link.deliver_pending(NOW + 3 * 3600)
    assert out["expired"] == 2 and out["simulated"] == 0
    assert {e["sms_state"] for e in events()} == {"expired"}


# ------------------------------------------------------------------------------------------------------ morning outlook & hand-off
def test_morning_uses_the_latest_valid_forecast_and_says_what_it_is():
    fc(4050)
    cfg = alerts.get_settings()
    m = n8n_link.morning_block(NOW, cfg, "https://x/review?t=T")
    assert m["status"] == "valid" and "$4,050.00" in m["text_fresh"] and "https://x/review?t=T" in m["text_fresh"]
    assert "not a touch" in m["text_fresh"] and "$4,000 ≤ forecast" in m["text_fresh"] and "$4,100 > forecast" in m["text_fresh"]
    assert m["valid_until"] <= NOW + 40 * 3600


def test_morning_unavailable_or_stale_is_reported_not_recycled():
    cfg = alerts.get_settings()
    m = n8n_link.morning_block(NOW, cfg, "u")
    assert m["status"] == "unavailable" and m["text_fresh"] is None and "no forecast is available" in m["text_stale"]
    fc(4050, issued_ago_h=50)
    m2 = n8n_link.morning_block(NOW, cfg, "u")
    assert m2["status"] == "unavailable" and "h old" in m2["reason"]
    # a forecast valid when the snapshot was built but expired by send time
    fc(4060, base=4055, issued_ago_h=1, target_in_h=5)
    snap = n8n_link.build_snapshot(NOW, fetch_quote=False)
    assert n8n_link.decide_morning(snap, NOW + 60)["fresh"] is True
    late = n8n_link.decide_morning(snap, NOW + 6 * 3600)
    assert late["fresh"] is False and "expired and is not being repeated as new" in late["text"]


def test_python_and_javascript_pick_the_same_morning_text():
    v = json.loads((HERE.parent.parent / "n8n" / "test" / "vectors.json").read_text())
    for c in v["cases"]:
        r = n8n_link.decide_morning(v["snapshot"], c["now"])
        assert (r["fresh"], r["text"]) == (c["fresh"], c["text"]), c


def test_snapshot_contains_schedule_and_a_review_page_and_no_phone_number():
    fc(4050)
    snap = n8n_link.build_snapshot(NOW, fetch_quote=False)
    assert snap["schedule"] == {"enabled": True, "time": "07:30", "tz": "Europe/Berlin"}
    assert snap["review"]["token"] and "Open Trading 212" in snap["review"]["html"]
    assert "phone" not in json.dumps(snap).lower()


def test_snapshot_push_is_idempotent_and_records_failures(monkeypatch):
    fc(4050)
    assert n8n_link.push_snapshot(NOW, fetch_quote=False)["state"] == "simulated"
    assert n8n_link.push_snapshot(NOW + 60, fetch_quote=False)["state"] == "unchanged"
    _live(monkeypatch)
    calls = []
    assert n8n_link.push_snapshot(NOW + 120, force=True, transport=lambda *a: (calls.append(a) or (200, {"ok": True})), fetch_quote=False)["state"] == "ok"
    assert calls[0][1].endswith("/gold-snapshot")
    assert n8n_link.push_snapshot(NOW + 180, force=True, transport=lambda *a: (500, {}), fetch_quote=False)["state"] == "failed"
    assert alerts.panel(NOW)["last_handoff"]["state"] == "failed"


# ------------------------------------------------------------------------------------------------------ review page
def test_review_page_content_and_auth():
    alerts.save_settings({"position_status": "open"}, NOW)
    fc(4050)
    alerts.check_price(NOW, quote=quote(4101))
    token, exp = review.mint_token(24, "price_crossing", now=NOW)
    page = n8n_link.local_review(token, NOW + 5)
    assert page and "Settlement forecast (endpoint)" in page and "Latest observed quote" in page
    assert "Below entry price" in page and "Above entry price" in page          # 4000 vs entry 4039.96 and 4100
    assert "https://www.trading212.com/" in page and "No response means no trade" in page
    assert "not profit" in page and "Profit is not calculated" in page
    assert "profit:" not in page.lower() and "gain" not in page.lower()
    assert n8n_link.local_review("not-a-token", NOW) is None
    assert n8n_link.local_review(token, exp + 1) is None                          # expired


def test_review_page_escapes_everything_dynamic():
    html = review.render({"title": "<script>alert(1)</script>", "kind_label": "x", "forecast": None, "quote": None, "note": "<img src=x onerror=1>",
                          "settings": {**alerts.get_settings(), "entry_price": 4039.96}, "generated_label": "g", "expires_label": "e"})
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html and "<img src=x" not in html


def test_review_page_without_a_forecast_says_so():
    page = n8n_link.local_review(review.mint_token(1, "morning", now=NOW)[0], NOW)
    assert page and "No valid forecast is available" in page


def test_no_money_value_is_computed_without_position_size_and_costs():
    alerts.save_settings({"position_size": 3})
    token, _ = review.mint_token(1, "morning", now=NOW)
    page = n8n_link.local_review(token, NOW) or ""
    assert "Position size" in page and "Profit is not calculated" in page


# ------------------------------------------------------------------------------------------------------ basis verification
def test_basis_check_states_instrument_currency_unit_and_flags_the_entry():
    alerts.save_settings({"position_status": "open"}, NOW)
    b = alerts.verify_basis(NOW, quotes={"futures": quote(3850), "spot": quote(3862, basis="spot")})
    assert b["levels_apply_to"]["currency"] == "USD" and "troy ounce" in b["levels_apply_to"]["unit"] and "GC=F" in b["levels_apply_to"]["instrument"]
    assert b["entry_price"]["verified"] is False
    assert b["spot_minus_futures"] == 12.0
    assert any("entry price" in w.lower() for w in b["warnings"])            # +4.9% away from the quote
    near = alerts.verify_basis(NOW, quotes={"futures": quote(4030), "spot": quote(4040, basis="spot")})
    assert not any("entry price" in w.lower() for w in near["warnings"])


def test_basis_check_reads_the_currency_from_the_provider_and_flags_a_mismatch():
    ok = alerts.verify_basis(NOW, quotes={"futures": {**quote(3850), "currency": "USD", "exchange": "CMX"}, "spot": quote(3862, basis="spot")})
    assert ok["currency_checked"].startswith("USD as reported") and "CMX" in ok["currency_checked"]
    bad = alerts.verify_basis(NOW, quotes={"futures": {**quote(3850), "currency": "EUR"}, "spot": quote(3862, basis="spot")})
    assert any("not USD" in w for w in bad["warnings"])


def test_basis_check_survives_a_failed_quote(monkeypatch):
    monkeypatch.setattr(alerts, "fetch_quote", lambda b: (_ for _ in ()).throw(RuntimeError("down")))
    b = alerts.verify_basis(NOW)
    assert "error" in b["quotes"]["futures"]


def test_simulation_cli_is_isolated_and_sends_nothing():
    out = alerts_sim.simulate(price=4101, forecast=4120)
    assert out["mode"].startswith("simulate")
    assert {e["type"] for e in out["events"]} == {"forecast_headsup", "price_crossing"}
    assert all(e["sms_state"] == "simulated" for e in out["events"] if e["type"] == "price_crossing")
    assert all(e["sms_state"] == "not_requested" for e in out["events"] if e["type"] == "forecast_headsup")   # immediate SMS is opt-in


# ------------------------------------------------------------------------------------------------------ HTTP API
@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app import main
    return TestClient(main.app)


def test_api_alerts_panel_and_settings_roundtrip(client):
    assert client.put("/api/alerts/settings", json={"position_status": "open"}).status_code == 200
    j = client.get("/api/alerts").json()
    assert j["settings"]["levels"] == [4000.0, 4100.0]
    assert [(x["level"], x["note"]) for x in j["levels"]] == [(4000.0, "Below entry price"), (4100.0, "Above entry price")]
    assert j["delivery"]["mode"] == "simulate" and "touch" in j and j["touch"]["status"] == "experimental"
    r = client.put("/api/alerts/settings", json={"levels": [3900, 4200], "morning_time": "08:15", "forecast_alerts": False})
    assert r.status_code == 200 and r.json()["settings"]["levels"] == [3900.0, 4200.0] and r.json()["settings"]["forecast_alerts"] is False
    assert alerts.get_settings()["morning_time"] == "08:15"
    assert client.get("/api/alerts").json()["last_handoff"]["state"] == "simulated"       # the change was handed off (simulated)
    bad = client.put("/api/alerts/settings", json={"morning_tz": "Mars/Base"})
    assert bad.status_code == 400 and "time zone" in bad.json()["detail"]["message"]


def test_api_review_page_requires_a_valid_token(client):
    assert client.get("/review").status_code == 403
    assert client.get("/review?t=guess").status_code == 403
    token, _ = review.mint_token(1, "morning")
    r = client.get(f"/review?t={token}")
    assert r.status_code == 200 and "Open Trading 212" in r.text and r.headers["cache-control"] == "no-store" and "noindex" in r.headers["x-robots-tag"]
    expired, _ = review.mint_token(-1, "morning")
    assert client.get(f"/review?t={expired}").status_code == 403


def test_api_morning_preview_reports_unavailable_rather_than_inventing(client):
    j = client.get("/api/alerts/morning-preview").json()
    assert j["status"] == "unavailable" and j["fresh"] is False and "no forecast is available" in j["text"]


# ------------------------------------------------------------------------------------------------------------- the tracked position is closed (unconfirmed)
def test_the_tracked_position_defaults_to_closed_unconfirmed_and_invents_nothing():
    cfg = alerts.get_settings()
    info = alerts.position_info(cfg)
    assert cfg["position_status"] == "closed_unconfirmed" and info["exit_price"] is None and info["closed_at"] is None and info["closure_reason"] is None
    assert "NOT recorded" in info["note"] and "nothing is assumed" in info["note"]
    assert alerts.entry_for_notes(cfg) is None and alerts.below_entry_note(4000.0, alerts.entry_for_notes(cfg)) is None   # no entry comparison for a closed position


def test_closed_position_alerts_are_price_level_notices_not_calls_to_review_a_position():
    fc(4050)
    alerts.check_forecasts(NOW)
    ev = events()[0]
    assert ev["payload"]["below_entry"] is None
    assert "Review your position" not in ev["message"] and "closed (unconfirmed)" in ev["message"] and "no response means no trade" in ev["message"].lower()
    alerts.check_price(NOW, quote=quote(4101))
    pm = [e for e in events() if e["alert_type"] == "price_crossing"][0]["message"]
    assert "Review your position" not in pm and "closed (unconfirmed)" in pm


def test_review_page_for_a_closed_position_shows_no_entry_comparison_and_no_invented_figures():
    fc(4050)
    alerts.check_price(NOW, quote=quote(4101))
    token, _ = review.mint_token(24, "price_crossing", now=NOW)
    page = n8n_link.local_review(token, NOW + 5)
    assert "Closed (unconfirmed)" in page and "not recorded" in page.lower() and "Last entry recorded" in page
    assert "Below entry price" not in page and "Above entry price" not in page and "Quote vs entry" not in page and "Exit price" not in page


def test_only_what_the_user_enters_about_the_closure_is_stored_and_it_validates():
    cfg = alerts.save_settings({"position_status": "closed", "exit_price": 3990.5, "closed_at": "2026-11-17", "closure_reason": "my decision"}, NOW)
    assert cfg["exit_price"] == 3990.5 and cfg["closure_reason"] == "my decision"
    assert "Exit price (you entered)" in n8n_link.local_review(review.mint_token(24, "x", now=NOW)[0], NOW + 5)
    with pytest.raises(alerts.SettingsError):
        alerts.save_settings({"position_status": "maybe"}, NOW)
    with pytest.raises(alerts.SettingsError):
        alerts.save_settings({"exit_price": -1}, NOW)
    with pytest.raises(alerts.SettingsError):
        alerts.save_settings({"closure_reason": "x" * 300}, NOW)


def test_reopening_restores_the_entry_notes():
    alerts.save_settings({"position_status": "open"}, NOW)
    assert alerts.below_entry_note(4000.0, alerts.entry_for_notes(alerts.get_settings())) == "Below entry price"


def test_no_entry_warning_in_the_basis_check_for_a_closed_position():
    b = alerts.verify_basis(NOW, quotes={"futures": quote(3850), "spot": quote(3862, basis="spot")})
    assert not any("entry price" in w.lower() and "away" in w.lower() for w in b["warnings"])
    assert b["position"]["status"] == "closed_unconfirmed"
