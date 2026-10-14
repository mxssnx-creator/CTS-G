import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { setImmediate as flush } from "node:timers/promises";
import { fetchLiveStats, pickView, viewFromSnapshot, deskPollMs, statsUnchanged, formatPosOrders, knownCount, posOrdersCounts, realPosOrders, livePosOrders, kindGateOpen, costPfWindow, effectiveSetCounts, formatEffectiveSets, type LiveStats } from "./live-stats.ts";
import { fetchConnections } from "./connections.ts";
import { fetchCtsBundle } from "./config-model.ts";

function transport(t: TestContext) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls: { url: string; signal?: AbortSignal | null; reply: (body: unknown, status?: number) => void }[] = [];
  t.mock.method(globalThis, "fetch", (url: string, init?: RequestInit) => new Promise<Response>((resolve) => {
    // Deliberately ignores abort to exercise bounded settlement, not just fetch rejection.
    calls.push({ url, signal: init?.signal, reply: (body, status = 200) => resolve(new Response(JSON.stringify(body), { status })) });
  }));
  return calls;
}

test("healthy Overall renders from one request without a snapshot download", async (t) => {
  const calls = transport(t);
  const promise = fetchLiveStats();
  assert.deepEqual(calls.map((c) => c.url), ["/stats.json?conn=overall"]);
  calls[0].reply({ running: true, connType: "overall", openCount: 250 });
  assert.equal((await promise)?.openCount, 250);
  t.mock.timers.tick(8000);
  assert.equal(calls.length, 1);
});

test("a hung primary falls back at 750ms and the loser is aborted", async (t) => {
  const calls = transport(t);
  const promise = fetchLiveStats("vst");
  t.mock.timers.tick(749);
  assert.equal(calls.length, 1);
  t.mock.timers.tick(1);
  assert.equal(calls.length, 2);
  calls[1].reply({ running: true, connType: "vst", openCount: 250 });
  assert.equal((await promise)?.openCount, 250);
  assert.equal(calls[0].signal?.aborted, true);
});

test("invalid primary triggers fallback immediately; stopped desks remain valid", async (t) => {
  const calls = transport(t);
  const promise = fetchLiveStats();
  calls[0].reply({ ok: false, detail: "unavailable" }, 503);
  await flush();
  assert.equal(calls.length, 2);
  calls[1].reply({ running: false, connType: "overall", halted: true });
  assert.equal((await promise)?.running, false);
});

test("a wrong connection never wins a fallback race", async (t) => {
  const calls = transport(t);
  const promise = fetchLiveStats("vst");
  t.mock.timers.tick(750);
  calls[1].reply({ running: true, connType: "live", equity: 999 });
  await flush();
  assert.equal(calls[0].signal?.aborted, false);
  calls[0].reply({ running: true, connType: "vst", equity: 123 });
  assert.equal((await promise)?.equity, 123);
});

test("all invalid responses settle without treating an error as Overall", async (t) => {
  const calls = transport(t);
  const promise = fetchLiveStats();
  calls[0].reply({ running: true, connType: "live" });
  await flush();
  calls[1].reply({ error: "offline" });
  assert.equal(await promise, null);
  assert.equal(pickView({ running: true, connType: "live" } as LiveStats, "overall"), null);
});

test("two nonresponsive endpoints are bounded by one eight-second deadline", async (t) => {
  const calls = transport(t);
  const promise = fetchLiveStats();
  t.mock.timers.tick(8000);
  assert.equal(await promise, null);
  assert.equal(calls.length, 2);
  assert.ok(calls.every((call) => call.signal?.aborted));
});

test("connection switch cancels both requests and prevents later fallback work", async (t) => {
  const calls = transport(t);
  const controller = new AbortController();
  const promise = fetchLiveStats("live", controller.signal);
  controller.abort();
  assert.equal(await promise, null);
  t.mock.timers.tick(8000);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].signal?.aborted, true);
  assert.equal(await fetchLiveStats("vst", controller.signal), null);
  assert.equal(calls.length, 1);
});

test("fallback lane contains only its attributed orders, metrics and progress", () => {
  const overall = { running: true, connType: "overall", equity: 999, pfCost: { ratio: 9 },
    sets: { setCount: 250 }, activity: { internalOpen: 250 }, progressPct: 90,
    lanes: [{ type: "vst", id: "bingx-x02", running: true, equity: 123, wins: 2, losses: 1, progressPct: 10 }],
    open: [{ connection: "bingx-x01", unit: "VST" }, { connection: "bingx-x02" }, { clientId: "Gx02abc" }, { unit: "USDT" }],
    closed: [{ connection: "bingx-x01", pnl: 500 }, { connection: "bingx-x02", pnl: -1 }, { pnl: 10 }],
  } as unknown as LiveStats;
  const lane = viewFromSnapshot(overall, "vst")!;
  assert.equal(lane.equity, 123);
  assert.equal(lane.open.length, 2);
  assert.deepEqual(lane.closed.map((c) => c.pnl), [-1]);
  assert.equal(lane.progressPct, 10);
  for (const key of ["sets", "activity", "pfCost", "available"] as const) assert.equal(lane[key], undefined);
  assert.equal(overall.open.length, 4);
});

test("connection catalog does not download full statistics on healthy polls", async (t) => {
  const calls = transport(t);
  const promise = fetchConnections();
  calls[0].reply({ types: [{ type: "overall", label: "Overall" }] });
  assert.equal((await promise)?.types.length, 1);
  t.mock.timers.tick(8000);
  assert.deepEqual(calls.map((c) => c.url), ["/connections.json"]);
});

test("settings are usable while the statistics request remains stalled", async (t) => {
  const calls = transport(t);
  const controller = new AbortController();
  const stats = fetchLiveStats("vst", controller.signal);
  const config = fetchCtsBundle("vst", controller.signal);
  calls[1].reply({ overlay: { setStepMax: 30, normalGeneral: false } });
  const result = await config;
  assert.equal(result.ok, true);
  assert.equal(result.overlay?.setStepMax, 30);
  assert.equal(calls[0].signal?.aborted, false);
  controller.abort();
  assert.equal(await stats, null);
});

test("frozen sidecar-down snapshots do not retrigger renders and poll slower", () => {
  const a = { running: false, halted: true, haltReason: "sidecar-down", stale: true, equity: 1, wins: 2, openCount: 0 } as LiveStats;
  const b = { ...a, now: 99 } as LiveStats;
  assert.equal(statsUnchanged(a, b), true);
  assert.equal(statsUnchanged(a, { ...a, wins: 3 }), false);
  assert.equal(deskPollMs(a), 12000);
  assert.equal(deskPollMs(a, true), 8000);
  assert.equal(deskPollMs({ running: true, halted: false }), 3500);
});

test("positions/orders never fall order counts back onto positions", () => {
  assert.equal(knownCount(-1), null);
  assert.equal(knownCount(0), 0);
  assert.equal(formatPosOrders(12, 24), "12/24");
  assert.equal(formatPosOrders(12, undefined, 12), "12/—");
  assert.equal(realPosOrders({ openCount: 12, realPositionCount: 12 }), "12/—");
  assert.equal(realPosOrders({ openCount: 12, realPositionCount: 12, realOrderCount: 24 }), "12/24");
  assert.equal(realPosOrders({ openCount: 298, realPositionCount: 298, realPositionGroupCount: 12, realOrderCount: 24 }), "12/24");
  assert.equal(realPosOrders({ openCount: 298, realPositionGroupCount: 12, realOrderCount: 24 }), "12/24");
  assert.equal(realPosOrders({ openCount: 298 }), "—/—");
  assert.equal(
    realPosOrders({ realPositionGroupCount: 13, realOrderCount: 30, liveOrderCount: 178, openCount: 149 }),
    "13/30",
  );
  assert.equal(
    realPosOrders({ realPositionGroupCount: 13, realOrderCount: 149, liveOrderCount: 178, openCount: 149 }),
    "13/178",
  );
  assert.equal(
    realPosOrders({ realPositionGroupCount: 12, realOrderCount: 24, liveOrderCount: 24, openCount: 700 }),
    "12/24",
  );
  assert.equal(livePosOrders({ livePositionCount: 8, exchangeOpenCount: 8, liveOrderCount: 16 }), "8/16");
  assert.equal(livePosOrders({ exchangeOpenCount: -1, liveOrderCount: -1 }), "—/—");
  assert.deepEqual(
    posOrdersCounts({
      realPositionGroupCount: 5,
      livePositionCount: 4,
      realOrderCount: 10,
      liveOrderCount: 26,
      openCount: 40,
    }),
    { realPositions: 5, livePositions: 4, realOrders: 10, liveOrders: 10 },
  );
});

test("stalled config reads time out and malformed settings cannot replace good values", async (t) => {
  const calls = transport(t);
  const stalled = fetchCtsBundle("vst");
  t.mock.timers.tick(4000);
  assert.equal((await stalled).ok, false);
  const invalid = fetchCtsBundle("vst");
  calls[1].reply({ ok: false, detail: "offline" });
  assert.equal((await invalid).ok, false);
});

test("stats changes without position movement still re-render the desk", () => {
  const a = { running: true, halted: false, openCount: 0, equity: 100, unrealized: 0, wins: 5, losses: 3, errors: 0,
    lastError: "", cycle: 10, tests: [{ name: "qa", pass: true, detail: "" }], activity: { eventCount: 50, errorCount: 0 },
    sets: { activeCount: 10, histFills: 500, validatedCount: 20 } } as unknown as LiveStats;
  assert.equal(statsUnchanged(a, { ...a } as LiveStats), true);
  for (const next of [
    { errors: 1 },
    { lastError: "order rejected" },
    { cycle: 11 },
    { tests: [{ name: "qa", pass: false, detail: "gap" }] },
    { activity: { eventCount: 51, errorCount: 0 } },
    { activity: { eventCount: 50, errorCount: 1 } },
    { sets: { activeCount: 10, histFills: 500, validatedCount: 21 } },
  ]) assert.equal(statsUnchanged(a, { ...a, ...next } as LiveStats), false, JSON.stringify(next));
});

test("a flat book reports zero Real orders instead of an unknown", () => {
  assert.deepEqual(
    posOrdersCounts({ openCount: 0, realPositionGroupCount: 0, realOrderCount: 0, livePositionCount: -1, liveOrderCount: -1 }),
    { realPositions: 0, livePositions: null, realOrders: 0, liveOrders: null },
  );
  assert.equal(realPosOrders({ openCount: 0, realPositionGroupCount: 0, realOrderCount: 0, liveOrderCount: -1 }), "0/0");
  // The legacy copied-count guard still applies to a non-empty book.
  assert.equal(realPosOrders({ realPositionGroupCount: 13, realOrderCount: 149, liveOrderCount: 178, openCount: 149 }), "13/178");
});

test("indication Gate column follows the engine gate, not the profitability flag", () => {
  assert.equal(kindGateOpen({ ok: false, gateOpen: true }, { ok: false }), true);
  assert.equal(kindGateOpen({ ok: true, gateOpen: false }), false);
  assert.equal(kindGateOpen({ ok: false, gateOpen: null }, { ok: false }), false);
  assert.equal(kindGateOpen({ ok: false }), undefined);
  assert.equal(kindGateOpen(undefined, undefined), undefined);
});

test("cost PF labels use the configured evaluation window", () => {
  assert.equal(costPfWindow({ pfCost: { n: 30 }, sets: { pfWindow: 20 } }), 30);
  assert.equal(costPfWindow({ sets: { pfWindow: 20 } }), 20);
  assert.equal(costPfWindow(null), 50); // Base window (50), not a stale 15
});

test("an idle intern book does not freeze catalog overviews", () => {
  const idle = effectiveSetCounts({
    histTest: { enabled: false, ownsCatalog: false, phase: "ready", internSetCount: 1, validatedCount: 0, processingCount: 0 },
    sets: { setCount: 39000, catalogSetCount: 39000, internSetCount: 1, validatedCount: 12, activeCount: 4, processingCount: 0 },
    progress: { phase: "initial", ready: false, setsDone: 10, setsTotal: 39000 },
  });
  assert.equal(idle.on, false);
  assert.equal(idle.setCount, 39000);
  assert.equal(idle.validated, 12);
  assert.equal(idle.processing, 38990);
  assert.match(formatEffectiveSets({
    histTest: { enabled: false, ownsCatalog: false, phase: "ready" },
    sets: { setCount: 39000, catalogSetCount: 39000, validatedCount: 12, activeCount: 4 },
    progress: { phase: "initial", ready: false, setsDone: 10, setsTotal: 39000 },
  }), /processing 38990/);
  assert.doesNotMatch(formatEffectiveSets({
    histTest: { enabled: false, ownsCatalog: false, phase: "ready", internSetCount: 1 },
    sets: { catalogSetCount: 39000, setCount: 39000, internSetCount: 1, validatedCount: 0, activeCount: 0 },
  }), /skipped/);
});

test("a running Test Historic reports its own processing, not intern 1", () => {
  const running = effectiveSetCounts({
    histTest: {
      enabled: true, ownsCatalog: true, running: true, phase: "evaluate",
      internSetCount: 1, validatedCount: 0, processingCount: 47, setsDone: 3, setsTotal: 50,
    },
    sets: { setCount: 1, catalogSetCount: 39000, internSetCount: 1, validatedCount: 0, activeCount: 0, processingCount: 0 },
  });
  assert.equal(running.on, true);
  assert.equal(running.running, true);
  assert.equal(running.setCount, 50);
  assert.equal(running.processing, 47);
  assert.match(formatEffectiveSets({
    histTest: {
      enabled: true, ownsCatalog: true, running: true, phase: "evaluate",
      internSetCount: 1, validatedCount: 0, processingCount: 47, setsTotal: 50, setsDone: 3,
    },
    sets: { catalogSetCount: 39000, internSetCount: 1, validatedCount: 0, activeCount: 0, processingCount: 0 },
  }), /processing 47/);
  assert.doesNotMatch(formatEffectiveSets({
    histTest: {
      enabled: true, ownsCatalog: true, running: true, phase: "evaluate",
      processingCount: 47, setsTotal: 50, setsDone: 3, validatedCount: 0,
    },
    sets: { catalogSetCount: 39000, internSetCount: 1 },
  }), /skipped/);
});

test("a live catalog replay is not hidden behind a finished historic test", () => {
  const stats = {
    histTest: {
      enabled: true, ownsCatalog: true, running: true, phase: "score", stale: true,
      internSetCount: 358, validatedCount: 60, processingCount: 1, setsDone: 358, setsTotal: 358,
    },
    sets: {
      setCount: 780, catalogSetCount: 780, internSetCount: 358, validatedCount: 60,
      activeCount: 0, processingCount: 780,
    },
    progress: { phase: "replay", ready: false, setsDone: 0, setsTotal: 780, symbolsDone: 30, symbolsTotal: 48 },
  };
  const counts = effectiveSetCounts(stats);
  assert.equal(counts.catalogBusy, true);
  assert.equal(counts.setCount, 780);
  assert.equal(counts.intern, 358);
  const line = formatEffectiveSets(stats);
  assert.match(line, /catalog 780/);
  assert.match(line, /symbols 30\/48/);
  assert.match(line, /intern 358/);
  assert.doesNotMatch(line, /processing 780/);
  assert.doesNotMatch(line, /sets 358/);
});

test("lane view keeps realized, foreign and unknown PnL % from the lane summary", () => {
  const overall = { running: true, connType: "overall", pnlPct: null,
    lanes: [{ type: "live", id: "bingx-x01", running: true, equity: 10, wins: 1, losses: 0, openCount: 0,
      realizedPnl: 1.5, systemRealized: 1.5, foreignRealized: 0.75, foreignUnrealized: -0.25, pnlPct: null }],
    open: [], closed: [] } as unknown as LiveStats;
  const lane = viewFromSnapshot(overall, "live")!;
  assert.equal(lane.realizedPnl, 1.5);
  assert.equal(lane.foreignRealized, 0.75);
  assert.equal(lane.pnlPct, null);
  const reported = viewFromSnapshot({ ...overall, lanes: [{ ...overall.lanes![0], pnlPct: 2.5 }] } as LiveStats, "live")!;
  assert.equal(reported.pnlPct, 2.5);
});
