import assert from "node:assert/strict";

test("unknown returns retain cash accounting but cannot become synthetic PF losses", () => {
  const metric = lastNCostPf([
    { t: 3, pnl: 5 },
    { t: 2, pnl: 0, pnl_pct: Number.NaN },
    { t: 1, pnl: 0, pnl_pct: 0.003 },
  ], 3, 0.1, 1.1);
  assert.equal(metric.count, 1);
  assert.equal(metric.unavailableReturnSamples, 2);
  assert.equal(metric.ratio, 1.2);
  assert.equal(metric.pass, false);
  assert.equal(lastNCostPf([{ t: 1, pnl: 5 }], 1).ratio, 1);
});
import { test } from "node:test";
import { buildOverview, calculateDrawdownTime, clearsReturnPf, costAsFrac, lastNCostPf, rowPositionCostPct, signedResultR } from "./analytics.ts";
import { derive } from "./derive-stats.ts";
import type { LiveStats } from "./live-stats.ts";

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
    { t: 4, pnl: 0, pnl_pct: 0.002 },
    { t: 1, pnl: 0, pnl_pct: -0.002 },
    { t: 3, pnl: 0, pnl_pct: 0.002 },
    { t: 2, pnl: 0, pnl_pct: -0.002 },
  ], 2);
  assert.equal(metric.count, 2);
  assert.equal(metric.avgR, 1);
  assert.equal(metric.ratio, 1.1);
});

test("drawdown time uses cost-net pnl_pct when pnl is zero", () => {
  const metric = calculateDrawdownTime(
    [
      { t: 1_700_000_100, symbol: "A", pnl: 0, pnl_pct: 0.01 },
      { t: 1_700_000_160, symbol: "A", pnl: 0, pnl_pct: -0.02 },
      { t: 1_700_000_220, symbol: "A", pnl: 0, pnl_pct: -0.01 },
    ],
    1_700_000_220_000,
    3,
  );
  assert.equal(metric.samples, 3);
  assert.ok(metric.episodes >= 1);
  assert.ok(metric.maxDurationMs >= 60_000);
});

test("PositionCost between 0.02% and 0.05% stays a percent, matching the engine", () => {
  assert.equal(costAsFrac(0.04), 0.0004);
  assert.equal(costAsFrac(0.05), 0.0005);
  assert.equal(costAsFrac(0.02), 0.0002);
  const rows = Array.from({ length: 10 }, (_, i) => ({ t: 1_790_000_000 - i * 60, pnl: 0.26, pnl_pct: 0.003 }));
  const metric = lastNCostPf(rows, 15, 0.04);
  assert.equal(metric.classicPf, 99);
  assert.equal(metric.netAvg, 0.0026);
  assert.equal(metric.ratio, 1.65);
});

test("small PositionCost stays percent and free fills keep a finite reference PF", () => {
  assert.equal(costAsFrac(0.01), 0.0001);
  assert.equal(signedResultR(0.0002, 0.01), 1);
  assert.ok(Math.abs(signedResultR(0.003, 0) - 3) < 1e-12);
  const free = lastNCostPf([{ t: 1, pnl: 0.3, pnl_pct: 0.003, position_cost_pct: 0, cost_source: "live-exchange", notional: 100 }], 1);
  assert.equal(free.ratio, 1.3);
  assert.equal(free.costPct, 0);
  assert.equal(free.netPct, 0.3);
  assert.equal(free.grossPct, 0.3);
  assert.equal(free.pfReferenceCostPct, 0.1);
  assert.equal(free.zeroCostSamples, 1);
});

test("return PF uses measured costs per fill and actual mean returns", () => {
  const metric = lastNCostPf([
    { t: 2, pnl: 3.7, pnl_pct: 0.004, qty: 10, entry: 100, fee_total: 0.3, cost_source: "live-exchange" },
    { t: 1, pnl: 0.15, pnl_pct: 0.003, notional: 100, position_cost_pct: 0.15, cost_source: "live-exchange" },
  ], 2);
  assert.equal(metric.avgR, 6.6667);
  assert.equal(metric.ratio, 1.6667);
  assert.equal(metric.netAvg, 0.0026);
  assert.equal(metric.netPct, 0.26);
  assert.equal(metric.grossPct, 0.35);
  assert.equal(metric.costPct, 0.04090909);
  assert.equal(metric.costSamples, 2);
});

test("cost aliases preserve measured precedence and round-trip fee scope", () => {
  assert.equal(rowPositionCostPct({ notional: 100, position_cost_pct: 0.01, fee_total: 9 }), 0.01);
  assert.equal(rowPositionCostPct({ notional: 100, costPct: 0.03 }), 0.1);
  assert.equal(rowPositionCostPct({ notional: 100, costPct: 0.03, cost_source: "live-exchange" }), 0.03);
  assert.equal(rowPositionCostPct({ notional: 100, commission: 0.05 }), 0.1);
  assert.equal(rowPositionCostPct({ notional: 100, commission: { amount: 0.05 }, fee_scope: "roundtrip" }), 0.05);
  assert.equal(rowPositionCostPct({ notional: 100, fee_rate: 0.0005 }), 0.1);
  assert.equal(rowPositionCostPct({ notional: 100, fee_total: 0 }), 0.1);
  assert.equal(rowPositionCostPct({ notional: 100, fee_total: 0, cost_source: "live-exchange" }), 0);
});

test("legacy net cash PnL reconstructs return without a second cost deduction", () => {
  const metric = lastNCostPf([{ t: 1, pnl: 1, qty: 100, entry: 1 }], 1, 0.15);
  assert.equal(metric.ratio, 1.6667);
  assert.equal(metric.netAvg, 0.01);
  assert.equal(metric.netPct, 1);
  assert.equal(metric.grossPct, 1.15);
});

test("return PF passes only nonempty evidence satisfying its configured sample floor", () => {
  const rows = Array.from({ length: 6 }, (_, t) => ({ t, pnl: 1, pnl_pct: 0.003 }));
  assert.equal(lastNCostPf([], 15).pass, false);
  assert.equal(lastNCostPf(rows, 15).pass, false);
  assert.equal(lastNCostPf(rows, 15, 0.1, 1.1, 6).pass, true);
  assert.equal(lastNCostPf(rows, 15, 0.1, 1.3, 6).pass, false);
  assert.equal(lastNCostPf([], 15, 0.1, 1.1, 0).pass, false);
  assert.equal(clearsReturnPf(1.02, 1.02), false);
  assert.equal(clearsReturnPf(1.0201, 1.02), true);
  assert.equal(clearsReturnPf(1.1, 1.15), false);
  assert.equal(lastNCostPf([{ t: 1, pnl: 0, pnl_pct: 0.0012000001 }], 1, 0.1, 1.02).pass, false);
});

test("overview follows configured last-N policy and preserves measured zero cost", () => {
  const now = 1_700_000_000_000;
  const rows = Array.from({ length: 10 }, (_, i) => ({ t: now - i * 60_000, pnl: 0.3, pnl_pct: 0.003 }));
  const overview = buildOverview(rows, now, 0, { n: 6, minPf: 1.3, requiredSamples: 6 });
  assert.equal(overview.costPf.n, 6);
  assert.equal(overview.costPf.count, 6);
  assert.equal(overview.costPf.ratio, 1.3);
  assert.equal(overview.costPf.pass, true);
  assert.equal(overview.costPf.costPct, 0);
  assert.equal(buildOverview([], now).costPf.pass, false);
});

test("derived PF never falls back to cash win-loss PF", () => {
  const stats = {
    profitFactor: 99,
    sets: { pfWindow: 1 },
    closed: [{ t: 1, symbol: "BTC-USDT", pnl: 1, pnl_pct: 0.002, hold_s: 1, reason: "tp", side: "LONG" }],
  } as LiveStats;
  assert.equal(derive(stats).pf, 1.1);
  assert.equal(derive(null).pf, 1);
});

test("tied close timestamps resolve like the engine's chronological tape (rows arrive newest-first)", () => {
  // Chronological: a +1 win, then a -2 loss, both stamped the same second. The
  // desk receives the newest row first; ordering the tie backwards would end
  // the tape one unit shallower than the engine's.
  const newestFirst = [
    { t: 1_700_000_100, symbol: "A", pnl: -2 },
    { t: 1_700_000_100, symbol: "A", pnl: 1 },
  ];
  const metric = calculateDrawdownTime(newestFirst, 1_700_000_200_000, 3);
  assert.equal(metric.currentDepth, 2);
  assert.equal(metric.inDrawdown, true);
});
