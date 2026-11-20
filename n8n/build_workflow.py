#!/usr/bin/env python3
"""Generate gold-alerts.workflow.json (import it in n8n: Workflows -> Import from file).

One workflow holds everything because n8n "static data" (where the forecast hand-off lives) is per workflow:

  POST /webhook/gold-snapshot   the app pushes the latest forecast, morning text, schedule and review page      (Header Auth)
  POST /webhook/gold-alert      the app hands over one alert: n8n texts it and records provider acceptance      (Header Auth)
  POST /webhook/gold-sms-status Twilio's delivery receipts (accepted != delivered)                              (secret in the URL)
  GET  /webhook/gold-status     the app pulls the delivery records n8n collected while the Mac was asleep       (Header Auth)
  GET  /webhook/gold-review     the review page an SMS link opens (bearer token in the link)
  Schedule (every 5 minutes)    sends the morning outlook once per day inside the configured window, from the stored snapshot

The only logic lives in lib/logic.js (unit-tested with plain Node) and is inlined into each Code node.
"""
import json
import re
import uuid
from pathlib import Path

HERE = Path(__file__).parent
LOGIC = re.sub(r"\nmodule\.exports = .*", "", (HERE / "lib" / "logic.js").read_text())
NS = uuid.UUID("0b8f4b3c-3f0a-4d7e-9d5b-6f1e0a1c2d3e")
CFG = ("// ---- EDIT ONCE after importing (this node, 'Decide morning' and 'Record receipt' share the same three values) ----\n"
       "const CFG = { SMS_TO: '+REPLACE_WITH_YOUR_NUMBER', SMS_FROM: '+REPLACE_WITH_YOUR_TWILIO_NUMBER',\n"
       "  STATUS_URL: 'https://REPLACE.app.n8n.cloud/webhook/gold-sms-status?k=REPLACE_WITH_A_LONG_RANDOM_STRING', STATUS_KEY: 'REPLACE_WITH_THE_SAME_LONG_RANDOM_STRING' };\n")

nodes, conns = [], {}


def node(name, type_, tv, params, x, y, **extra):
    n = {"parameters": params, "id": str(uuid.uuid5(NS, name)), "name": name, "type": type_, "typeVersion": tv, "position": [x, y]}
    n.update(extra)
    nodes.append(n)
    return name


def connect(a, b, out=0):
    conns.setdefault(a, {"main": []})
    while len(conns[a]["main"]) <= out:
        conns[a]["main"].append([])
    conns[a]["main"][out].append({"node": b, "type": "main", "index": 0})


def code(name, body, x, y, cfg=False):
    return node(name, "n8n-nodes-base.code", 2, {"jsCode": (CFG if cfg else "") + LOGIC + "\n" + body}, x, y)


def webhook(name, method, path, x, y, auth=True):
    p = {"httpMethod": method, "path": path, "responseMode": "responseNode", "options": {}}
    extra = {"webhookId": str(uuid.uuid5(NS, "wh" + path))}
    if auth:
        p["authentication"] = "headerAuth"
        extra["credentials"] = {"httpHeaderAuth": {"id": "REPLACE", "name": "Gold shared secret"}}
    return node(name, "n8n-nodes-base.webhook", 2, p, x, y, **extra)


def respond(name, x, y, **p):
    return node(name, "n8n-nodes-base.respondToWebhook", 1.1, {"respondWith": "firstIncomingItem", "options": {}, **p}, x, y)


def iff(name, expr, x, y):
    return node(name, "n8n-nodes-base.if", 2.2, {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                "conditions": [{"id": str(uuid.uuid5(NS, name)), "leftValue": expr, "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}], "combinator": "and"}, "options": {}}, x, y)


def twilio(name, x, y):
    return node(name, "n8n-nodes-base.twilio", 1, {"resource": "sms", "operation": "send", "from": "={{ $json.from }}", "to": "={{ $json.to }}", "message": "={{ $json.message }}",
                                                   "options": {"statusCallback": "={{ $json.statusCallback }}"}}, x, y,
                credentials={"twilioApi": {"id": "REPLACE", "name": "Twilio account"}}, onError="continueRegularOutput", alwaysOutputData=True)


NOW = "const now = Math.floor(Date.now() / 1000);\nconst sd = $getWorkflowStaticData('global');\n"

# A. snapshot in
webhook("Snapshot in", "POST", "gold-snapshot", 0, 0)
code("Store snapshot", NOW + """const body = $input.first().json.body || {};
if (body.type !== 'snapshot' || !body.morning || !body.schedule) return [{ json: { ok: false, error: 'bad payload' } }];
storeReview(sd, body.review, now);
sd.snapshot = { type: 'snapshot', generated_at: body.generated_at, schedule: body.schedule, levels: body.levels, morning: body.morning };
return [{ json: { ok: true, stored_at: now } }];""", 260, 0)
respond("Respond snapshot", 520, 0)
connect("Snapshot in", "Store snapshot"); connect("Store snapshot", "Respond snapshot")

# B. alert in -> SMS -> record
webhook("Alert in", "POST", "gold-alert", 0, 160)
code("Build alert SMS", NOW + """const body = $input.first().json.body || {};
const v = validAlert(body, now);
if (!v.ok) return [{ json: { send: false, accepted: false, error: v.error } }];
storeReview(sd, body.review, now);
return [{ json: { send: true, event_id: body.event_id, to: CFG.SMS_TO, from: CFG.SMS_FROM, message: body.text, statusCallback: CFG.STATUS_URL } }];""", 260, 160, cfg=True)
iff("Send alert?", "={{ $json.send }}", 520, 160)
twilio("Send alert SMS", 780, 100)
code("Record alert delivery", NOW + """const ctx = $('Build alert SMS').first().json;
const r = $input.first().json || {};
const ok = !!r.sid;
const rec = { sid: r.sid || null, status: ok ? String(r.status || 'queued') : 'failed', kind: 'immediate', event_id: ctx.event_id, at: now, error: ok ? null : String((r.error && r.error.message) || r.message || 'send failed').slice(0, 200) };
pushDelivery(sd, rec);
return [{ json: { accepted: ok, sid: rec.sid, status: rec.status, error: rec.error } }];""", 1040, 100)
respond("Respond alert", 1300, 160)
connect("Alert in", "Build alert SMS"); connect("Build alert SMS", "Send alert?")
connect("Send alert?", "Send alert SMS", 0); connect("Send alert?", "Respond alert", 1)
connect("Send alert SMS", "Record alert delivery"); connect("Record alert delivery", "Respond alert")

# C. Twilio receipts
webhook("SMS status callback", "POST", "gold-sms-status", 0, 340, auth=False)
code("Record receipt", NOW + """const j = $input.first().json;
if ((j.query || {}).k !== CFG.STATUS_KEY) return [{ json: { ok: false } }];
const b = j.body || {};
return [{ json: { ok: applyReceipt(sd, b.MessageSid, b.MessageStatus || '', now) } }];""", 260, 340, cfg=True)
respond("Respond receipt", 520, 340)
connect("SMS status callback", "Record receipt"); connect("Record receipt", "Respond receipt")

# D. status out (the app pulls this when the Mac is awake)
webhook("Status out", "GET", "gold-status", 0, 500)
code("Build status", NOW + """return [{ json: { ok: true, now, snapshot_generated_at: sd.snapshot ? sd.snapshot.generated_at : null, deliveries: sd.deliveries || [] } }];""", 260, 500)
respond("Respond status", 520, 500)
connect("Status out", "Build status"); connect("Build status", "Respond status")

# E. review page
webhook("Review page", "GET", "gold-review", 0, 660, auth=False)
code("Find review", NOW + """const t = (($input.first().json.query) || {}).t;
return [{ json: lookupReview(sd, t, now) }];""", 260, 660)
node("Respond review", "n8n-nodes-base.respondToWebhook", 1.1, {"respondWith": "text", "responseBody": "={{ $json.html }}", "options": {"responseCode": "={{ $json.status }}", "responseHeaders": {"entries": [
    {"name": "Content-Type", "value": "text/html; charset=utf-8"}, {"name": "Cache-Control", "value": "no-store"}, {"name": "X-Robots-Tag", "value": "noindex"}, {"name": "Referrer-Policy", "value": "no-referrer"}]}}}, 520, 660)
connect("Review page", "Find review"); connect("Find review", "Respond review")

# F. morning schedule
node("Every 5 minutes", "n8n-nodes-base.scheduleTrigger", 1.2, {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 820)
code("Decide morning", NOW + """const d = decideMorning(sd, now);
if (!d.send) return [{ json: { send: false, reason: d.reason } }];
return [{ json: { send: true, dateKey: d.dateKey, fresh: d.fresh, to: CFG.SMS_TO, from: CFG.SMS_FROM, message: d.text, statusCallback: CFG.STATUS_URL } }];""", 260, 820, cfg=True)
iff("Send morning?", "={{ $json.send }}", 520, 820)
twilio("Send morning SMS", 780, 800)
code("Record morning delivery", NOW + """const ctx = $('Decide morning').first().json;
const r = $input.first().json || {};
const ok = !!r.sid;
pushDelivery(sd, { sid: r.sid || null, status: ok ? String(r.status || 'queued') : 'failed', kind: 'morning', note: ctx.fresh ? 'fresh forecast' : 'stale or unavailable forecast, said so', at: now, error: ok ? null : String((r.error && r.error.message) || r.message || 'send failed').slice(0, 200) });
recordMorning(sd, ctx.dateKey, ok);
return [{ json: { accepted: ok } }];""", 1040, 800)
connect("Every 5 minutes", "Decide morning"); connect("Decide morning", "Send morning?"); connect("Send morning?", "Send morning SMS", 0); connect("Send morning SMS", "Record morning delivery")

wf = {"name": "Gold alerts (SMS + morning outlook)", "nodes": nodes, "connections": conns, "active": False, "settings": {"executionOrder": "v1"}, "pinData": {}, "tags": []}
(HERE / "gold-alerts.workflow.json").write_text(json.dumps(wf, indent=2))
print(f"wrote gold-alerts.workflow.json: {len(nodes)} nodes")
