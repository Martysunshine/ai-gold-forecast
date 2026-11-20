// Run: node --test frontend/src/ranges.test.mjs   (Node >= 23 strips the TypeScript types)
import test from "node:test";
import assert from "node:assert/strict";
import { rangesFor } from "./ranges.ts";

const at = (iso) => ({ target_time: iso });
const NOW = Date.parse("2026-11-18T20:00:00Z");   // 15:00 New York, after the 13:30 settlement

test("a next-settlement target tomorrow needs 30-minute bars, not 5-minute ones", () => {
  const r = rangesFor("next", at("2026-11-19T13:30:00-05:00"), NOW);
  assert.deepEqual(r, { allowed: ["1W"], dflt: "1W" });
});
test("a next-settlement target within hours can use the 1D view", () => {
  const r = rangesFor("next", at("2026-11-18T13:30:00-05:00"), Date.parse("2026-11-18T14:30:00Z"));
  assert.deepEqual(r.allowed, ["1D", "1W"]);
  assert.equal(r.dflt, "1D");
});
test("long horizons use daily ranges so origin and target both fit", () => {
  for (const h of [5, 20]) assert.ok(rangesFor(h, at("2026-12-16T13:30:00-05:00"), NOW).allowed.every((x) => ["3M", "1Y"].includes(x)));
  assert.equal(rangesFor(1, at("2026-11-19T13:30:00-05:00"), NOW).dflt, "1M");
});
