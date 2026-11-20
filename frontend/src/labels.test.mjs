// Run: node --test frontend/src/labels.test.mjs   (Node >= 23 strips the TypeScript types)
import test from "node:test";
import assert from "node:assert/strict";
import { resolveLabels } from "./labels.ts";

const box = { width: 340, height: 300 };
const rectsOverlap = (a, b) => a.left < b.left + b.w && b.left < a.left + a.w && a.top < b.top + b.h && b.top < a.top + a.h;

test("labels for near-identical prices are separated vertically and never overlap", () => {
  const out = resolveLabels([
    { id: "latest", x: 250, y: 150, w: 120, h: 22, side: "right" },
    { id: "forecast", x: 270, y: 150.4, w: 130, h: 34, side: "right" },
    { id: "ref", x: 240, y: 150, w: 120, h: 22, side: "right" },
  ], box);
  for (let i = 0; i < out.length; i++) for (let j = i + 1; j < out.length; j++) assert.equal(rectsOverlap(out[i], out[j]), false, `${out[i].id} overlaps ${out[j].id}`);
  assert.ok(out.some((o) => o.leader), "a displaced label must request a leader line");
});

test("no label is ever clipped by the plot box", () => {
  const out = resolveLabels([
    { id: "a", x: 330, y: 3, w: 150, h: 22, side: "left" },
    { id: "b", x: 4, y: 299, w: 150, h: 22, side: "right" },
    { id: "c", x: 170, y: 150, w: 400, h: 22, side: "center" },
  ], box);
  for (const o of out) {
    assert.ok(o.left >= 4 && o.left + o.w <= box.width - 4 + 0.001 || o.w > box.width - 8, `${o.id} clipped horizontally`);
    assert.ok(o.top >= 4 && o.top + o.h <= box.height - 4, `${o.id} clipped vertically`);
  }
});

test("well-separated labels stay exactly where they were asked to be (no needless movement)", () => {
  const [a] = resolveLabels([{ id: "a", x: 200, y: 100, w: 100, h: 20, side: "right", dy: -12 }], box);
  assert.equal(a.top, 100 - 10 - 12);
  assert.equal(a.leader, false);
});

test("the point positions are never modified", () => {
  const items = [{ id: "a", x: 100, y: 100, w: 90, h: 20, side: "right" }, { id: "b", x: 100, y: 101, w: 90, h: 20, side: "right" }];
  const out = resolveLabels(items, box);
  assert.deepEqual(out.map((o) => [o.x, o.y]), items.map((i) => [i.x, i.y]));
});
