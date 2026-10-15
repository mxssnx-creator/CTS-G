import assert from "node:assert/strict";
import { test } from "node:test";
import {
  CALC_RUNNING_PHASES,
  calcIsRunning,
  calcPollMs,
  calcStartLabel,
  calcStatusLine,
  forcedBestBySymbol,
  fetchHistCalc,
  fetchLocalForced,
  formatForcedPf,
  guardForcedEvidence,
  isCurrentForcedEvidence,
  hasCalcSnapshot,
  startHistCalc,
  type ForcedConfigRow,
  type HistCalcJob,
} from "./hist-calc.ts";

test("partial, deferred, paused and score-refresh stay in-flight so polling does not stop", () => {
  for (const phase of CALC_RUNNING_PHASES) {
    assert.equal(calcIsRunning(phase), true, phase);
  }
  for (const phase of ["idle", "ready", "error", "stopped", "", undefined]) {
    assert.equal(calcIsRunning(phase as string | undefined), false, String(phase));
  }
});

test("snapshot, poll cadence and labels follow a continuous lane", () => {
  assert.equal(hasCalcSnapshot(null), false);
  assert.equal(hasCalcSnapshot({ phase: "idle", pct: 0, detail: "" }), false);
  const ready: HistCalcJob = {
    phase: "ready",
    pct: 100,
    detail: "published complete hourly replay",
    ready: true,
    lastCompleteRun: 1_700_000_000,
    nextRunAt: Date.now() / 1000 + 3600,
    rows: [{ id: "general:1m:sl0.6:st8", kind: "base", pack: "general", slRatio: 0.6, step: 8, n: 30, wr: 0.6, last15Ratio: 1.2, last15N: 30, maxDdS: 12 }],
  };
  assert.equal(hasCalcSnapshot(ready), true);
  assert.equal(calcPollMs({ phase: "partial", pct: 40, detail: "3/20" }), 1200);
  assert.equal(calcPollMs({ phase: "deferred", pct: 20, detail: "peer" }), 1200);
  assert.equal(calcPollMs(ready), 8000);
  assert.equal(calcPollMs({ phase: "idle", pct: 0, detail: "" }), 8000);
  assert.equal(calcPollMs(ready, true), 8000);
  assert.equal(calcStartLabel(null), "Start continuous replay");
  assert.equal(calcStartLabel({ phase: "replay", pct: 40, detail: "" }), "Replaying…");
  assert.equal(calcStartLabel(ready), "Refresh now");
  assert.match(calcStatusLine(null, 48), /continuous replay/);
  assert.match(calcStatusLine({ phase: "partial", pct: 55, detail: "12/20 symbols" }), /partial 55% · 12\/20 symbols/);
  assert.match(calcStatusLine(ready), /next refresh/);
});

test("forcedBestBySymbol prefers stored winners then throughput", () => {
  const stored = forcedBestBySymbol({
    version: 4,
    bestBySymbol: {
      "SOL-USDT": { evidenceVersion: 4, trainN: 30, trainPf: 1.2, symbol: "SOL-USDT", tpPct: 0.55, slPct: 0.1, indication: "trend" },
      "XRP-USDT": { evidenceVersion: 4, trainN: 30, trainPf: 1.2, symbol: "XRP-USDT", tpPct: 0.6, slPct: 0.2, indication: "signals" },
    },
  });
  assert.deepEqual(stored.map((row) => row.symbol), ["SOL-USDT", "XRP-USDT"]);
  const row = (symbol: string, tph: number, sl: number, pf: number): ForcedConfigRow => ({
    evidenceVersion: 4,
    id: symbol, symbol, indication: "signals", direction: "LONG", tpPct: 0.6, slPct: sl, slRatio: sl / 0.6,
    n: 10, trainN: 8, holdoutN: 2, pf, trainPf: pf, holdoutPf: 1, costRatio: 1, netPct: 1,
    maxDrawdownR: 1, tradesPerHour: tph, avgHoldS: 60, eligible: true, status: "ok", source: "historical-market",
    settingsKey: "k",
  });
  const derived = forcedBestBySymbol({
    version: 4,
    rows: [row("BCH-USDT", 1.1, 0.25, 1.2), row("BCH-USDT", 1.4, 0.2, 1.08), row("XRP-USDT", 2.2, 0.2, 1.14)],
  });
  assert.equal(derived.length, 2);
  assert.equal(derived.find((item) => item.symbol === "BCH-USDT")?.slPct, 0.25);
  assert.equal(derived.find((item) => item.symbol === "BCH-USDT")?.trainPf, 1.2);
  const storedForced = forcedBestBySymbol({
    version: 4,
    forcedBest: { "BCH-USDT": { evidenceVersion: 4, trainN: 30, symbol: "BCH-USDT", tpPct: 0.75, slPct: 0.25, indication: "break", trainPf: 1.243 } },
    bestBySymbol: { "BCH-USDT": { evidenceVersion: 4, trainN: 30, symbol: "BCH-USDT", tpPct: 0.75, slPct: 0.2, indication: "break", trainPf: 1.08 } },
  });
  assert.equal(storedForced[0]?.slPct, 0.25);
});

test("forced PF formatting preserves observed zero and leaves empty or missing evidence blank", () => {
  assert.equal(formatForcedPf(0, 30), "0.000");
  assert.equal(formatForcedPf(-0.2, 30), "-0.200");
  assert.equal(formatForcedPf(1, 0), "—");
  assert.equal(formatForcedPf(null, 30), "—");
  assert.equal(formatForcedPf(undefined, 30), "—");
  assert.equal(formatForcedPf(Number.NaN, 30), "—");
});

test("legacy and mixed forced artifacts cannot supply winners or successes", () => {
  const winner = { evidenceVersion: 4, symbol: "SOL-USDT", tpPct: 0.6, slPct: 0.2, trainN: 30, trainPf: 99 };
  for (const version of [undefined, 3, 5]) {
    const data = { version, succeeded: ["SOL-USDT"], bestBySymbol: { "SOL-USDT": winner } };
    assert.equal(isCurrentForcedEvidence(data), false);
    assert.deepEqual(forcedBestBySymbol(data), []);
    const guarded = guardForcedEvidence(data);
    assert.equal(guarded?.needsReturnPfRecalculation, true);
    assert.equal(guarded?.rows, undefined);
    assert.equal(guarded?.succeeded, undefined);
    assert.match(guarded?.detail ?? "", /Needs return-PF recalculation/);
  }
  assert.equal(isCurrentForcedEvidence({ version: 4, bestBySymbol: { "SOL-USDT": { ...winner, evidenceVersion: 3 } } }), false);
  assert.equal(isCurrentForcedEvidence({ version: 4, rows: [{ evidenceVersion: 3 } as ForcedConfigRow] }), false);
  assert.equal(isCurrentForcedEvidence({ version: 4, rows: [] }), true);
  const valid = { version: 4, bestBySymbol: { "SOL-USDT": { ...winner, trainPf: 1.2 } } };
  assert.equal(isCurrentForcedEvidence(valid), true);
  assert.deepEqual(guardForcedEvidence(valid), valid);
  assert.equal(forcedBestBySymbol(valid).length, 1);
  for (const patch of [{ trainPf: 0 }, { trainPf: null }, { trainN: 0 }, { eligible: false }]) {
    assert.deepEqual(forcedBestBySymbol({ version: 4, bestBySymbol: { "SOL-USDT": { ...winner, ...patch } } }), []);
  }
});

test("local version 3 evidence is quarantined and never merged into version 4 jobs", async (t) => {
  let now = 1_000_000;
  t.mock.method(Date, "now", () => now);
  const legacy = { version: 3, succeeded: ["SOL-USDT"], forcedBest: { "SOL-USDT": { symbol: "SOL-USDT", tpPct: 0.6, slPct: 0.2, trainPf: 99 } } };
  const canonical = { version: 4, rows: [], succeeded: [], bestBySymbol: {} };
  let localArtifact: NonNullable<HistCalcJob["forcedConfigs"]> = legacy;
  let job: HistCalcJob = { phase: "ready", pct: 100, detail: "old forced replay", forcedConfigs: legacy };
  t.mock.method(globalThis, "fetch", async (url: string | URL) => new Response(JSON.stringify(String(url).includes('/forced-configs.json') ? localArtifact : job), { status: 200 }));
  const local = await fetchLocalForced();
  assert.equal(local?.needsReturnPfRecalculation, true);
  assert.equal(local?.forcedBest, undefined);
  const oldJob = await fetchHistCalc("vst");
  assert.equal(oldJob.forcedConfigs?.needsReturnPfRecalculation, true);
  assert.deepEqual(forcedBestBySymbol(oldJob.forcedConfigs), []);
  job = { ...job, detail: "canonical return replay", forcedConfigs: canonical };
  const currentJob = await fetchHistCalc("vst");
  assert.equal(currentJob.forcedConfigs?.version, 4);
  assert.deepEqual(currentJob.forcedConfigs?.succeeded, []);
  assert.deepEqual(currentJob.forcedConfigs?.bestBySymbol, {});
  assert.equal(currentJob.forcedConfigs?.needsReturnPfRecalculation, undefined);
  now += 31_000;
  localArtifact = canonical;
  const refreshed = await fetchLocalForced();
  assert.equal(refreshed?.version, 4);
  assert.equal(refreshed?.needsReturnPfRecalculation, undefined);
});

test("Start posts a continuous hourly generation and keeps allConfigs on", async (t) => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  t.mock.method(globalThis, "fetch", async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init || {} });
    return new Response(JSON.stringify({ ok: true, phase: "queued", pct: 0.5, detail: "queued", continuous: true, mode: "hourly" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  const job = await startHistCalc({ hours: 7, connection: "live", continuous: true });
  assert.equal(job.phase, "queued");
  assert.equal(job.continuous, true);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /hist-calc\.json\?conn=live/);
  const body = JSON.parse(String(calls[0].init.body));
  assert.equal(body.continuous, true);
  assert.equal(body.mode, "hourly");
  assert.equal(body.hours, 7);
  assert.equal(body.allConfigs, true);
});
