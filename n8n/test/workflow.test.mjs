// Runs the REAL Code-node source from gold-alerts.workflow.json against a stub of n8n's globals. Twilio is replaced by a fake response.
// This exercises the node logic and the way the nodes pass data to each other; it is not a substitute for importing the workflow into n8n.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const wf = JSON.parse(readFileSync(new URL("../gold-alerts.workflow.json", import.meta.url)));
const byName = Object.fromEntries(wf.nodes.map((n) => [n.name, n]));
const ts = (iso) => Math.floor(Date.parse(iso) / 1000);

function run(name, json, sd, outputs = {}, nowIso) {
  const code = byName[name].parameters.jsCode;
  const RealDate = Date;
  if (nowIso) globalThis.Date = class extends RealDate { constructor(...a) { return a.length ? new RealDate(...a) : new RealDate(nowIso); } static now() { return new RealDate(nowIso).getTime(); } };
  try {
    const fn = new Function("$getWorkflowStaticData", "$input", "$", code);
    const out = fn(() => sd, { first: () => ({ json }) }, (n) => ({ first: () => ({ json: outputs[n] }) }));
    return out[0].json;
  } finally { globalThis.Date = RealDate; }
}

const snapshot = (over = {}) => ({ type: "snapshot", generated_at: ts("2026-11-17T22:00:00Z"), schedule: { enabled: true, time: "07:30", tz: "Europe/Berlin" }, levels: [4000, 4100],
  morning: { text_fresh: "FRESH outlook", text_stale: "STALE outlook", valid_until: ts("2026-11-18T18:30:00Z"), status: "valid" },
  review: { token: "tok-morning", expires_at: ts("2026-11-20T00:00:00Z"), html: "<p>review</p>" }, ...over });

test("workflow carries no real phone number or secret", () => {
  const all = JSON.stringify(wf);
  assert.match(all, /REPLACE_WITH_YOUR_NUMBER/);
  assert.doesNotMatch(all, /\+\d{9,}/);
});

test("snapshot hand-off: stored for the morning job, review page stored, bad payload refused", () => {
  const sd = {};
  assert.equal(run("Store snapshot", { body: snapshot() }, sd, {}, "2026-11-17T22:00:05Z").ok, true);
  assert.equal(sd.snapshot.morning.text_fresh, "FRESH outlook");
  assert.ok(sd.reviews["tok-morning"]);
  assert.equal(sd.snapshot.review, undefined, "the HTML is kept only in the token registry");
  assert.equal(run("Store snapshot", { body: { type: "nope" } }, sd).ok, false);
});

test("morning job: sends the fresh text at 07:30 Berlin, once, from the stored copy (no backend call needed)", () => {
  const sd = {};
  run("Store snapshot", { body: snapshot() }, sd, {}, "2026-11-17T22:00:05Z");
  assert.equal(run("Decide morning", {}, sd, {}, "2026-11-18T06:20:00Z").send, false);
  const d = run("Decide morning", {}, sd, {}, "2026-11-18T06:35:00Z");
  assert.equal(d.send, true); assert.equal(d.fresh, true); assert.equal(d.message, "FRESH outlook");
  assert.match(d.statusCallback, /gold-sms-status/);
  const rec = run("Record morning delivery", { sid: "SMm1", status: "queued" }, sd, { "Decide morning": d }, "2026-11-18T06:35:02Z");
  assert.equal(rec.accepted, true);
  assert.equal(sd.deliveries[0].kind, "morning");
  assert.equal(run("Decide morning", {}, sd, {}, "2026-11-18T06:45:00Z").send, false, "not sent twice");
});

test("morning job: an expired forecast is reported as stale, never recycled as new", () => {
  const sd = {};
  run("Store snapshot", { body: snapshot() }, sd, {}, "2026-11-17T22:00:05Z");
  // 2026-11-19 07:35 Berlin: the stored forecast's target (18 Nov 13:30 New York) has long passed
  const d = run("Decide morning", {}, sd, {}, "2026-11-19T06:35:00Z");
  assert.equal(d.send, true); assert.equal(d.fresh, false); assert.match(d.message, /^STALE outlook/);
});

test("morning job: nothing ever received -> says so", () => {
  const d = run("Decide morning", {}, {}, {}, "2026-11-18T06:35:00Z");
  assert.equal(d.send, true); assert.match(d.message, /no forecast has been received/);
});

test("a failed provider call is recorded as failed and does not mark the day as sent", () => {
  const sd = {};
  run("Store snapshot", { body: snapshot() }, sd, {}, "2026-11-17T22:00:05Z");
  const d = run("Decide morning", {}, sd, {}, "2026-11-18T06:35:00Z");
  const rec = run("Record morning delivery", { error: { message: "Twilio 21211 invalid number" } }, sd, { "Decide morning": d }, "2026-11-18T06:35:02Z");
  assert.equal(rec.accepted, false);
  assert.equal(sd.deliveries[0].status, "failed");
  assert.equal(run("Decide morning", {}, sd, {}, "2026-11-18T06:40:00Z").send, true, "retries inside the window");
});

test("immediate alert: accepted by the provider is recorded as accepted (not delivered); expired alerts are refused", () => {
  const sd = {};
  const body = { type: "alert", event_id: 7, text: "GOLD price alert", expires_at: ts("2026-11-17T21:00:00Z"), review: { token: "tok-7", expires_at: ts("2026-11-18T20:00:00Z"), html: "<p>7</p>" } };
  const b = run("Build alert SMS", { body }, sd, {}, "2026-11-17T20:30:00Z");
  assert.equal(b.send, true); assert.equal(b.message, "GOLD price alert");
  assert.ok(sd.reviews["tok-7"]);
  const rec = run("Record alert delivery", { sid: "SM7", status: "queued" }, sd, { "Build alert SMS": b }, "2026-11-17T20:30:03Z");
  assert.deepEqual([rec.accepted, rec.status], [true, "queued"]);
  assert.equal(run("Build alert SMS", { body }, {}, {}, "2026-11-17T21:30:00Z").send, false, "past expiry");
});

test("delivery receipts: wrong key ignored; right key upgrades accepted -> delivered", () => {
  const sd = { deliveries: [{ sid: "SM7", status: "queued" }] };
  const bad = run("Record receipt", { query: { k: "wrong" }, body: { MessageSid: "SM7", MessageStatus: "delivered" } }, sd);
  assert.equal(bad.ok, false); assert.equal(sd.deliveries[0].status, "queued");
  const key = /STATUS_KEY: '([^']+)'/.exec(byName["Record receipt"].parameters.jsCode)[1];
  assert.equal(run("Record receipt", { query: { k: key }, body: { MessageSid: "SM7", MessageStatus: "delivered" } }, sd).ok, true);
  assert.equal(sd.deliveries[0].status, "delivered");
});

test("review page and status endpoint", () => {
  const sd = {};
  run("Store snapshot", { body: snapshot() }, sd, {}, "2026-11-17T22:00:05Z");
  assert.equal(run("Find review", { query: { t: "tok-morning" } }, sd, {}, "2026-11-18T06:00:00Z").status, 200);
  assert.equal(run("Find review", { query: { t: "tok-morning" } }, sd, {}, "2026-11-21T06:00:00Z").status, 410);
  assert.equal(run("Find review", { query: { t: "guess" } }, sd).status, 403);
  const s = run("Build status", {}, { ...sd, deliveries: [{ sid: "x", status: "delivered" }] }, {}, "2026-11-18T06:00:00Z");
  assert.equal(s.deliveries.length, 1);
});
