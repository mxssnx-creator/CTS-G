import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { setImmediate as flush } from "node:timers/promises";
import { fetchLiveStats, pickView, viewFromSnapshot, type LiveStats } from "./live-stats.ts";
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

test("stalled config reads time out and malformed settings cannot replace good values", async (t) => {
  const calls = transport(t);
  const stalled = fetchCtsBundle("vst");
  t.mock.timers.tick(4000);
  assert.equal((await stalled).ok, false);
  const invalid = fetchCtsBundle("vst");
  calls[1].reply({ ok: false, detail: "offline" });
  assert.equal((await invalid).ok, false);
});
