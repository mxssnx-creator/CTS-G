import assert from "node:assert/strict";
import { test } from "node:test";
import { calculateDrawdownTime, lastNCostPf } from "./analytics.ts";

test("drawdown time isolates interleaved symbols", () => {
  const metric = calculateDrawdownTime(
    [
      { t: 1_700_000_100_000, symbol: "A", pnl: 1 },
      { t: 1_700_000_160_000, symbol: "A", pnl: -2 },
      { t: 1_700_000_500_000, symbol: "B", pnl: 1 },
      { t: 1_700_000_560_000, symbol: "B", pnl: -0.2 },
      { t: 1_700_000_620_000, symbol: "B", pnl: 1.5 },
    ],
    1_700_000_700_000,
    3,
  );
  assert.equal(metric.samples, 5);
  assert.equal(metric.episodes, 2);
  assert.equal(metric.maxDurationMs, 540_000);
  assert.equal(metric.currentDurationMs, 540_000);
  assert.equal(metric.maxDepth, 2);
  assert.equal(metric.inDrawdown, true);
});

test("last-N cost PF selects the newest timestamps", () => {
  const metric = lastNCostPf([
    { t: 4, pnl: 0, pnl_pct: 0.003 },
    { t: 1, pnl: 0, pnl_pct: -0.003 },
    { t: 3, pnl: 0, pnl_pct: 0.003 },
    { t: 2, pnl: 0, pnl_pct: -0.003 },
  ], 2);
  assert.equal(metric.count, 2);
  assert.equal(metric.avgR, 1);
  assert.equal(metric.ratio, 1.1);
});
