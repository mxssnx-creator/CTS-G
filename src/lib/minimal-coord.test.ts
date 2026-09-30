import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MINIMAL_COORD,
  MINIMAL_RANGE,
  SHORT_RANGE,
  coordProtects,
  coordVariantTotal,
  rangeOfTp,
} from "./minimal-coord.ts";

const grid = {
  holdH: [16, 24],
  minSl: 0.01,
  minTrail: 0.006,
  short: SHORT_RANGE,
  minimal: MINIMAL_RANGE,
};

test("Minimal Coord keeps the two close ranges off the wide grid", () => {
  assert.equal(MINIMAL_COORD, "Minimal Coord.");
  assert.deepEqual(MINIMAL_RANGE.tp, [0.004, 0.0045, 0.005, 0.0055, 0.006]);
  assert.deepEqual(SHORT_RANGE.tp, [0.006, 0.008, 0.01, 0.012]);
  assert.equal(rangeOfTp(0.005), "minimal");
  assert.equal(rangeOfTp(0.012), "short");
  assert.equal(rangeOfTp(0.03), "wide");
});

test("trailing cells push the stop out, and identical cells collapse", () => {
  const raw = coordVariantTotal(grid);
  assert.equal(raw, 4 * 9 * 3 * 2 + 5 * 5 * 2 * 2);
  const cells = coordProtects(1, grid);
  assert.ok(cells.length < raw);
  assert.ok(cells.every((p) => p.tp <= 0.012));
  const trailing = cells.filter((p) => p.trail > 0);
  assert.ok(trailing.length > 0);
  assert.ok(trailing.every((p) => p.sl + 1e-9 >= p.tp * 2));
  const keys = new Set(cells.map((p) => `${p.tp}|${p.sl}|${p.trail}|${p.hold}`));
  assert.equal(keys.size, cells.length);
});
