import { test } from "node:test";
import assert from "node:assert/strict";
import { viewFromSnapshot, type LiveStats } from "./live-stats.ts";

/** An overall snapshot as the sidecar writes it: two lanes, closed rows tagged with their connection. */
function overallSnapshot(): LiveStats {
  const lanes = [
    {
      type: "live", id: "bingx-x01", label: "Live", unit: "USDT", exchange: "BingX", mode: "LIVE_MAINNET",
      running: true, halted: false, haltReason: null, paused: false, stale: false,
      equity: 100, available: 50, usedMargin: 10, unrealized: 0, sessionPnl: 2,
      pnlPct: 2, drawdownPct: 1, maxOpen: 3, pf: 1.3, pfCost: 1.3, pfDetail: { pf: 1.3, count: 20 },
      wins: 3, losses: 1, openCount: 1, symbolCount: 5,
    },
    {
      type: "vst", id: "bingx-x02", label: "VST demo", unit: "VST", exchange: "BingX VST", mode: "VST_DEMO",
      running: false, halted: true, haltReason: "stats stale", paused: false, stale: true,
      equity: 300, available: 120, usedMargin: null, unrealized: 0, sessionPnl: -1,
      pnlPct: null, drawdownPct: null, maxOpen: null, pf: null, pfCost: null, pfDetail: null,
      wins: 0, losses: 1, openCount: 1, symbolCount: 4,
    },
  ];
  return {
    running: true, mode: "OVERALL", connection: "overall", connType: "overall", unit: "MIXED", exchange: "All",
    lanes,
    equity: 100, equityLive: 100, equityVst: 300, available: 50,
    usedMargin: null, unrealized: 0, sessionPnl: 1, pnlPct: 0.25, drawdownPct: null, maxOpen: null,
    profitFactor: 1.1, pf: 1.1, pfCost: { pf: 1.1, count: 40 },
    symbols: ["BTC-USDT", "ETH-USDT"], openCount: 2,
    open: [
      { symbol: "BTC-USDT", clientId: "Gx01a", unit: "USDT", side: "long" },
      { symbol: "ETH-USDT", clientId: "Gx02b", unit: "VST", side: "long" },
    ],
    closed: [
      { symbol: "BTC-USDT", pnl: 1, pnl_pct: 0.5, connection: "bingx-x01" },
      { symbol: "ETH-USDT", pnl: -1, pnl_pct: -0.4, connection: "bingx-x02" },
      { symbol: "SOL-USDT", pnl: 2, pnl_pct: 0.9, connection: "bingx-x01" },
    ],
    tests: [], errors: 0, halted: false,
  } as unknown as LiveStats;
}

test("the overall view is returned unchanged", () => {
  const s = overallSnapshot();
  assert.equal(viewFromSnapshot(s, "overall"), s);
});

test("a lane view lists only the closed rows that lane closed", () => {
  const s = overallSnapshot();
  const live = viewFromSnapshot(s, "live");
  const vst = viewFromSnapshot(s, "vst");
  assert.deepEqual((live?.closed ?? []).map((c) => c.symbol), ["BTC-USDT", "SOL-USDT"]);
  assert.deepEqual((vst?.closed ?? []).map((c) => c.symbol), ["ETH-USDT"]);
});

test("a lane view lists only the open symbols of that lane", () => {
  const s = overallSnapshot();
  assert.deepEqual(viewFromSnapshot(s, "live")?.symbols, ["BTC-USDT"]);
  assert.deepEqual(viewFromSnapshot(s, "vst")?.symbols, ["ETH-USDT"]);
});

test("a lane view takes its own PF block, never the overall one", () => {
  const s = overallSnapshot();
  const live = viewFromSnapshot(s, "live");
  const vst = viewFromSnapshot(s, "vst");
  assert.equal(live?.pfCost?.pf, 1.3);
  assert.equal(live?.profitFactor, 1.3);
  assert.equal(vst?.pfCost, undefined, "a lane without a PF block must not show the overall PF");
  assert.equal(vst?.profitFactor, undefined);
});

test("nullable fields stay null in a lane view when the lane does not report them", () => {
  const s = overallSnapshot();
  const live = viewFromSnapshot(s, "live");
  const vst = viewFromSnapshot(s, "vst");
  assert.equal(live?.usedMargin, 10);
  assert.equal(live?.pnlPct, 2);
  assert.equal(live?.drawdownPct, 1);
  assert.equal(live?.maxOpen, 3);
  assert.equal(vst?.usedMargin, null, "a silent lane is null, never the overall sum or 0");
  assert.equal(vst?.pnlPct, null);
  assert.equal(vst?.drawdownPct, null);
  assert.equal(vst?.maxOpen, null);
});

test("a frozen lane is reported stale and not running", () => {
  const vst = viewFromSnapshot(overallSnapshot(), "vst");
  assert.equal(vst?.stale, true);
  assert.equal(vst?.running, false);
  assert.equal(vst?.halted, true);
});
