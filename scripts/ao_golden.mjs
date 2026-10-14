#!/usr/bin/env node
// Golden outputs of CTS-A-O for the hourly lane's Python port (server/pulse/ao_*.py).
// Runs A-O's own TypeScript read-only; nothing here re-implements A-O logic.
//
//   node --experimental-strip-types --no-warnings scripts/ao_golden.mjs \
//     --ao /path/to/cts-a-o --bars scripts/fixtures/ao_bars_1h.json --out scripts/fixtures/ao_golden.json
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const argv = process.argv.slice(2);
const arg = (k, d) => {
  const i = argv.indexOf(`--${k}`);
  return i >= 0 ? argv[i + 1] : d;
};
const ao = resolve(arg("ao", "../mxssnx-creator/cts-a-o"));
const imp = (p) => import(`${ao}/${p}`);
const { indicationState, mtfState, INDICATION_BY_ID, INDICATIONS } = await imp("src/core/indications/registry.ts");
const { SeriesCache } = await imp("src/core/indications/cache.ts");
const { comboSignal } = await imp("src/core/bots/bots.ts");
const { applyFilter } = await imp("src/core/indications/filters.ts");
const { simulate, splitSides, mergeSideTrades } = await imp("src/core/sim/backtest.ts");
const { protectGrid } = await imp("src/core/sim/walkforward.ts");
const { barsFromCandles } = await imp("src/core/market/bars.ts");
const { RT_COST } = await imp("src/core/config.ts");
const { statsOf } = await imp("src/core/metrics/stats.ts");

const PAIRS = [
  "follow|rsi-mom-10-25", "follow|rsi-mom-14-15", "follow|rsi-mom-14-20", "follow|rsi-mom-14-25",
  "follow|rsi-mom-21-15", "follow|rsi-mom-21-20", "follow|rsi-mom-21-25",
  "follow|bb-walk@x4", "follow|break-vol@x4", "follow|break-vol-2@x4", "follow|break-atr-2@x4",
  "follow|act-burst-2.5@x4",
  "revert|cci-40-200@x4", "revert|cci-14-200@x4", "revert|z-50-2.5@x4", "revert|act-chop@x4",
  "follow|kelt-20-2.5@x4", "follow|bb-walk-50@x4", "revert|cci-20-200@x4", "follow|squeeze-20@x4",
  "follow|macd-cross@x4", "follow|break-vol-1.3@x4", "follow|break-atr-1.5@x4",
  "revert|bb-bounce-20-2.5@x4", "revert|z-20-2.5@x4", "revert|bb-bounce-20-3@x4",
  "revert|rsi-14-25-75@x4", "revert|mfi-14-20@x4",
  "follow|break-atr-2+adx25", "revert|bb-bounce-20-3+volHi",
];
const TRADE_PAIRS = [
  "follow|rsi-mom-14-15", "follow|rsi-mom-21-25", "follow|bb-walk@x4", "follow|break-vol-2@x4",
  "revert|cci-14-200@x4", "revert|act-chop@x4", "follow|break-atr-2+adx25",
];
const GRID = { tp: [0.03, 0.05, 0.08], slOfTp: [1, 2], trailOfTp: [0, 0.5], minTrail: 0.006, minSl: 0.01, holdH: [16, 24] };
const FILTERS = ["volHi", "volLo", "euUs", "adx20", "adx25"];

// "<id>@x4" for ids outside A-O's COMBINED_BASE: registered in memory exactly as registry.ts registers its own
// (mtfState(id, k, [4])), so comboSignal and the walk-forward read them like any other id.
for (const pair of PAIRS) {
  const ind = pair.split("|")[1].split("+")[0];
  if (!ind.endsWith("@x4") || INDICATION_BY_ID.has(ind)) continue;
  const base = INDICATION_BY_ID.get(ind.slice(0, -3));
  const spec = { ...base, id: ind, params: { ...base.params, htf: 4 }, fn: (k) => mtfState(base.id, k, [4]) };
  INDICATIONS.push(spec);
  INDICATION_BY_ID.set(ind, spec);
}
const enc = (st) => Array.from(st, (x) => (x > 0 ? "+" : x < 0 ? "-" : "0")).join("");
const num = (x) => (Number.isFinite(x) ? +x.toPrecision(12) : null);
const pairSignal = (pair, k) => {
  const [bot, rest] = pair.split("|");
  const [ind, ...filters] = rest.split("+");
  return filters.reduce((s, f) => applyFilter(f, s, k), comboSignal(bot, ind, k));
};

const raw = JSON.parse(readFileSync(arg("bars"), "utf8"));
const cells = protectGrid(60, GRID, RT_COST).map((p) => ({ tp: p.tp, sl: p.sl, trail: p.trail, hold: p.hold, trailStep: p.trailStep ?? 1, trailFree: p.trailFree ?? false }));
const out = { grid: GRID, cost: RT_COST, cells, pairs: PAIRS, symbols: {} };
for (const [sym, candles] of Object.entries(raw)) {
  const b = barsFromCandles(sym, 60, candles);
  const k = new SeriesCache(b);
  const states = {};
  const bases = new Set(PAIRS.map((p) => p.split("|")[1].split("+")[0].replace(/@x4$/, "")));
  for (const id of bases) {
    states[id] = enc(indicationState(id, k));
    states[`${id}@x4`] = enc(mtfState(id, k, [4]));
  }
  const { k: hk, map } = k.htf(4);
  const signals = Object.fromEntries(PAIRS.map((p) => [p, enc(pairSignal(p, k))]));
  const filters = {};
  const all = new Int8Array(b.n);
  for (let i = 0; i < b.n; i++) all[i] = i % 2 ? 1 : -1;
  for (const f of FILTERS) filters[f] = enc(applyFilter(f, all, k));
  const sub = (a) => Array.from(a).filter((_, i) => i % 7 === 0).map(num);
  const bb20 = k.bb(20, 2), bb50 = k.bb(50, 2), dmi = k.dmi(14), mac = k.macd(), kc = k.kelt(20, 2.5);
  const series = {
    rsi10: sub(k.rsi(10)), rsi14: sub(k.rsi(14)), rsi21: sub(k.rsi(21)), rsi7: sub(k.rsi(7)),
    ema20: sub(k.ema(20)), ema50: sub(k.ema(50)), atr14: sub(k.atr(14)),
    bb20up: sub(bb20.up), bb20lo: sub(bb20.lo), bb20width: sub(bb20.width), bb50up: sub(bb50.up),
    cci14: sub(k.cci(14)), cci20: sub(k.cci(20)), cci40: sub(k.cci(40)), z20: sub(k.z(20)), z50: sub(k.z(50)),
    adx: sub(dmi.adx), pdi: sub(dmi.pdi), mdi: sub(dmi.mdi), vsma20: sub(k.volSma(20)), rsma20: sub(k.rangeSma(20)),
    mfi14: sub(k.mfi(14)), macdLine: sub(mac.line), macdSignal: sub(mac.signal), macdHist: sub(mac.hist),
    keltUp: sub(kc.up), keltLo: sub(kc.lo),
  };
  const trades = {};
  for (const pair of TRADE_PAIRS) {
    const sig = pairSignal(pair, k);
    trades[pair] = cells.map((_, ci) => {
      const p = protectGrid(60, GRID, RT_COST)[ci];
      const runs = splitSides(sig).map((s) => simulate(pair, b, s, p, { cost: RT_COST }).trades);
      return mergeSideTrades(runs).map((t) => [t.side, t.entryT, t.exitT, t.entry, t.exit, t.r, t.reason, t.bars, t.mfe, t.mae]);
    });
  }
  const st = statsOf(mergeSideTrades(splitSides(pairSignal(TRADE_PAIRS[0], k)).map((s) => simulate("x", b, s, protectGrid(60, GRID, RT_COST)[0], { cost: RT_COST }).trades)));
  out.symbols[sym] = {
    n: b.n,
    states,
    htf: { t: Array.from(hk.b.t), o: Array.from(hk.b.o), h: Array.from(hk.b.h), l: Array.from(hk.b.l), c: Array.from(hk.b.c), v: Array.from(hk.b.v), map: Array.from(map) },
    signals,
    filters,
    series,
    trades,
    stats: { n: st.n, pf: st.pf, net: st.net, wr: st.wr },
  };
}
writeFileSync(arg("out"), JSON.stringify(out));
console.error(`golden: ${Object.keys(out.symbols).length} symbols, ${cells.length} cells, ${PAIRS.length} pairs → ${arg("out")}`);
