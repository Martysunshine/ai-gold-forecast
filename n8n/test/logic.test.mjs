import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
const L = createRequire(import.meta.url)("../lib/logic.js");
const V = JSON.parse(readFileSync(new URL("./vectors.json", import.meta.url)));

test("morning text matches the shared vectors (same cases are run against the Python implementation)", () => {
  for (const c of V.cases) {
    const r = L.chooseMorningText(V.snapshot, c.now);
    assert.equal(r.fresh, c.fresh, `fresh @${c.now}`);
    assert.equal(r.text, c.text, `text @${c.now}`);
  }
});

test("no snapshot at all: says so instead of inventing a forecast", () => {
  const r = L.chooseMorningText(null, 1);
  assert.equal(r.fresh, false);
  assert.match(r.text, /no forecast has been received/);
});

const berlin = (iso) => Math.floor(Date.parse(iso) / 1000);

test("07:30 Berlin: sends inside the window, once per local day", () => {
  const st = { snapshot: V.snapshot };
  // 2026-11-18 07:30 Berlin (CET, UTC+1) = 06:30Z
  assert.equal(L.decideMorning(st, berlin("2026-11-18T06:29:00Z")).send, false, "before");
  const d = L.decideMorning(st, berlin("2026-11-18T06:30:00Z"));
  assert.equal(d.send, true);
  assert.equal(d.dateKey, "2026-11-18");
  assert.equal(L.decideMorning(st, berlin("2026-11-18T07:59:00Z")).send, true, "still inside the 90 min window");
  assert.equal(L.decideMorning(st, berlin("2026-11-18T08:00:00Z")).send, false, "window over: never at noon because n8n was down");
  L.recordMorning(st, "2026-11-18", true);
  assert.equal(L.decideMorning(st, berlin("2026-11-18T06:40:00Z")).reason, "already sent today");
  assert.equal(L.decideMorning(st, berlin("2026-11-19T06:35:00Z")).send, true, "next day sends again");
});

test("daylight saving: 07:30 Berlin in summer is 05:30Z", () => {
  const st = { snapshot: V.snapshot };
  assert.equal(L.decideMorning(st, berlin("2026-07-15T05:30:00Z")).send, true);
  assert.equal(L.decideMorning(st, berlin("2026-07-15T06:30:00Z")).send, true, "inside window");
  assert.equal(L.decideMorning(st, berlin("2026-07-15T07:30:00Z")).send, false);
});

test("configurable time and zone come from the snapshot", () => {
  const snap = { ...V.snapshot, schedule: { enabled: true, time: "09:00", tz: "America/New_York" } };
  const st = { snapshot: snap };
  assert.equal(L.decideMorning(st, berlin("2026-11-18T14:00:00Z")).send, true); // 09:00 EST
  assert.equal(L.decideMorning(st, berlin("2026-11-18T06:30:00Z")).send, false);
});

test("switched off, unknown zone and default schedule", () => {
  assert.equal(L.decideMorning({ snapshot: { ...V.snapshot, schedule: { enabled: false, time: "07:30", tz: "Europe/Berlin" } } }, berlin("2026-11-18T06:30:00Z")).send, false);
  assert.match(L.decideMorning({ snapshot: { ...V.snapshot, schedule: { enabled: true, time: "07:30", tz: "Nowhere/Land" } } }, 1).reason, /unknown time zone/);
  assert.equal(L.decideMorning({}, berlin("2026-11-18T06:30:00Z")).send, true, "no snapshot: default 07:30 Europe/Berlin, and it says nothing was received");
});

test("failed sends retry within the window but stop after the attempt limit", () => {
  const st = { snapshot: V.snapshot };
  for (let i = 0; i < L.MAX_ATTEMPTS; i++) { assert.equal(L.decideMorning(st, berlin("2026-11-18T06:40:00Z")).send, true); L.recordMorning(st, "2026-11-18", false); }
  assert.equal(L.decideMorning(st, berlin("2026-11-18T06:40:00Z")).reason, "too many failed attempts today");
});

test("review tokens: valid, expired, unknown; old ones are pruned", () => {
  const st = {};
  L.storeReview(st, { token: "a", html: "<p>A</p>", expires_at: 100 }, 10);
  assert.equal(L.lookupReview(st, "a", 50).status, 200);
  assert.equal(L.lookupReview(st, "a", 150).status, 410);
  assert.equal(L.lookupReview(st, "zzz", 50).status, 403);
  assert.equal(L.lookupReview(st, "", 50).status, 403);
  L.storeReview(st, { token: "b", html: "B", expires_at: 500 }, 200);
  assert.equal(st.reviews.a, undefined, "expired token pruned on the next store");
});

test("alerts: expired or malformed payloads are refused, not sent", () => {
  assert.equal(L.validAlert({ type: "alert", text: "x", expires_at: 100 }, 50).ok, true);
  assert.equal(L.validAlert({ type: "alert", text: "x", expires_at: 100 }, 150).ok, false);
  assert.equal(L.validAlert({ type: "snapshot" }, 1).ok, false);
  assert.equal(L.validAlert({ type: "alert", text: "" }, 1).ok, false);
});

test("accepted is not delivered: a receipt updates the same record later", () => {
  const st = {};
  L.pushDelivery(st, { sid: "SM1", status: "queued" });
  assert.equal(st.deliveries[0].status, "queued");
  assert.equal(L.applyReceipt(st, "SM1", "DELIVERED", 9), true);
  assert.equal(st.deliveries[0].status, "delivered");
  assert.equal(L.applyReceipt(st, "unknown", "delivered", 9), false);
});
