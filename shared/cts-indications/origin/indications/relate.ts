// Relate is a bounded, first-class indication family. It reuses the engine's causal trend/pullback/breakout ideas,
// then qualifies each source with an exact 1/5/15m trend subset, symbol and market drawdown ratios, and independent
// activity-part / last-bar / acceleration ratios. Every TF subset and threshold profile has its own ID and PF gate.
import type { Bars, IndicationKind } from "../domain/types.ts";
import type { IndicationSpec } from "./registry.ts";
import { SeriesCache } from "./cache.ts";
import type { MarketRef } from "./market.ts";

export const RELATE_TIMEFRAMES = [1, 5, 15] as const;
export const RELATE_SUBSETS = [
  [1],
  [5],
  [15],
  [1, 5],
  [1, 15],
  [5, 15],
  [1, 5, 15],
] as const;

export const RELATE_MODES = [
  { id: "trend", label: "Trend", description: "Established EMA, ADX and directional-movement trend." },
  { id: "adaptive", label: "Adaptive", description: "Trend, pullback, range turn or confirmed breakout by regime." },
] as const;
export type RelateMode = (typeof RELATE_MODES)[number]["id"];

export const RELATE_PROFILES = [
  {
    id: "guarded",
    label: "Guarded",
    priceDrawdownMax: 0.9,
    marketDrawdownMax: 0.9,
    lastActivityMin: 0.55,
    lastActivityMax: 2.25,
    activityPartMin: 0.65,
    activityChangeMin: 0.65,
    marketActivityMax: 3.0,
  },
  {
    id: "balanced",
    label: "Balanced",
    priceDrawdownMax: 1.7,
    marketDrawdownMax: 1.5,
    lastActivityMin: 0.8,
    lastActivityMax: 3.25,
    activityPartMin: 0.5,
    activityChangeMin: 0.5,
    marketActivityMax: 4.0,
  },
] as const;
export type RelateProfile = (typeof RELATE_PROFILES)[number]["id"];

const subsetCode = (tfs: readonly number[]) => tfs.join("x");
const relateId = (tfs: readonly number[], mode: RelateMode, profile: RelateProfile, micro: boolean) =>
  `relate-${micro ? "mc-" : ""}${subsetCode(tfs)}-${mode}-${profile}`;

/** Stable exact identities shared by Settings focus and the runtime registry. */
export const RELATE_CONFIG_IDS = RELATE_SUBSETS.flatMap((tfs) =>
  RELATE_MODES.flatMap((mode) =>
    RELATE_PROFILES.flatMap((profile) => [
      relateId(tfs, mode.id, profile.id, false),
      ...(Math.min(...tfs) >= 5 ? [relateId(tfs, mode.id, profile.id, true)] : []),
    ]),
  ),
);

type Trend = { ready: Uint8Array; dir: Int8Array };
type Context = Trend & {
  adaptive: Int8Array;
  priceDdLong: Float32Array;
  priceDdShort: Float32Array;
  marketDdLong: Float32Array;
  marketDdShort: Float32Array;
  activityLast: Float32Array;
  activityPart: Float32Array;
  activityChange: Float32Array;
};

const WARMUP = 39;
const DRAW_WINDOW = 24;
const VOL_WINDOW = 20;
const ACT_WINDOW = 20;
const finite2 = (a: number, b: number) => Number.isFinite(a) && Number.isFinite(b);
const finite3 = (a: number, b: number, c: number) => Number.isFinite(a) && Number.isFinite(b) && Number.isFinite(c);
const finite6 = (a: number, b: number, c: number, d: number, e: number, f: number) =>
  Number.isFinite(a) && Number.isFinite(b) && Number.isFinite(c) && Number.isFinite(d) && Number.isFinite(e) && Number.isFinite(f);
const finite5 = (a: number, b: number, c: number, d: number, e: number) =>
  Number.isFinite(a) && Number.isFinite(b) && Number.isFinite(c) && Number.isFinite(d) && Number.isFinite(e);
const clamp = (x: number) => (x > 0 ? x : 0);

export const isRelateInd = (base: string) => base.startsWith("relate-");
export const isRelateMicroInd = (base: string) => base.startsWith("relate-mc-");

/** The exact anchor lane for this relation: the smallest (fastest) timeframe in its subset. */
export function relateAnchorTf(base: string): number | null {
  if (!isRelateInd(base)) return null;
  const match = /^relate-(?:mc-)?(1|5|15)(?:x(5|15))?(?:x(15))?-(?:trend|adaptive)-(?:guarded|balanced)$/.exec(base);
  if (!match) return null;
  return Number(match[1]);
}

function valid(b: Bars, i: number): boolean {
  return (
    finite6(b.t[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i]) &&
    b.l[i] > 0 &&
    b.h[i] >= Math.max(b.o[i], b.c[i]) &&
    b.l[i] <= Math.min(b.o[i], b.c[i]) &&
    b.v[i] >= 0
  );
}

function spans(b: Bars): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  let start = -1;
  for (let i = 0; i < b.n; i++) {
    const adjacent = i > 0 && b.t[i] - b.t[i - 1] === b.tfMin * 60_000;
    if (start >= 0 && (!valid(b, i) || !adjacent)) {
      out.push([start, i]);
      start = -1;
    }
    if (valid(b, i) && start < 0) start = i;
  }
  if (start >= 0) out.push([start, b.n]);
  return out;
}

function sliceBars(b: Bars, from: number, to: number): Bars {
  return {
    ...b,
    n: to - from,
    t: b.t.slice(from, to),
    o: b.o.slice(from, to),
    h: b.h.slice(from, to),
    l: b.l.slice(from, to),
    c: b.c.slice(from, to),
    v: b.v.slice(from, to),
  };
}

function trendRaw(k: SeriesCache): Trend {
  const b = k.b;
  const fast = k.ema(12);
  const slow = k.ema(36);
  const { adx, pdi, mdi } = k.dmi(14);
  const ready = new Uint8Array(b.n);
  const dir = new Int8Array(b.n);
  for (let i = WARMUP - 1; i < b.n; i++) {
    if (!finite6(fast[i], slow[i], slow[i - 3], adx[i], pdi[i], mdi[i])) continue;
    ready[i] = 1;
    if (adx[i] < 18) continue;
    if (fast[i] > slow[i] && slow[i] > slow[i - 3] && pdi[i] > mdi[i]) dir[i] = 1;
    else if (fast[i] < slow[i] && slow[i] < slow[i - 3] && mdi[i] > pdi[i]) dir[i] = -1;
  }
  return { ready, dir };
}

function trendSeries(k: SeriesCache): Trend {
  return k.memo("relate:trend:v1", () => {
    const b = k.b;
    const runs = spans(b);
    if (runs.length === 1 && runs[0][0] === 0 && runs[0][1] === b.n) return trendRaw(k);
    const out: Trend = { ready: new Uint8Array(b.n), dir: new Int8Array(b.n) };
    for (const [from, to] of runs) {
      if (to - from < WARMUP) continue;
      const part = trendRaw(new SeriesCache(sliceBars(b, from, to)));
      out.ready.set(part.ready, from);
      out.dir.set(part.dir, from);
    }
    return out;
  });
}

function rollingExtremes(x: Float64Array, n: number, maximum: boolean): Float64Array {
  const out = new Float64Array(x.length).fill(NaN);
  const q = new Int32Array(x.length);
  let head = 0;
  let tail = 0;
  for (let i = 0; i < x.length; i++) {
    const lastPrior = i - 1;
    while (head < tail && q[head] < i - n) head++;
    if (lastPrior >= 0 && Number.isFinite(x[lastPrior])) {
      while (tail > head && (maximum ? x[q[tail - 1]] <= x[lastPrior] : x[q[tail - 1]] >= x[lastPrior])) tail--;
      q[tail++] = lastPrior;
    }
    if (head < tail) out[i] = x[q[head]];
  }
  return out;
}

function marketDrawdowns(b: Bars, m: MarketRef | null): { long: Float32Array; short: Float32Array } {
  const n = b.n;
  const long = new Float32Array(n);
  const short = new Float32Array(n);
  if (!m || m.syms < 2) return { long, short };
  long.fill(NaN);
  short.fill(NaN);
  const peak = rollingExtremes(m.idx, DRAW_WINDOW, true);
  const trough = rollingExtremes(m.idx, DRAW_WINDOW, false);
  let count = 0;
  let sum = 0;
  let sumSq = 0;
  for (let i = 0; i < n; i++) {
    if (i > 0 && Number.isFinite(m.ret[i - 1])) {
      const x = m.ret[i - 1];
      count++;
      sum += x;
      sumSq += x * x;
    }
    if (i > VOL_WINDOW && Number.isFinite(m.ret[i - VOL_WINDOW - 1])) {
      const x = m.ret[i - VOL_WINDOW - 1];
      count--;
      sum -= x;
      sumSq -= x * x;
    }
    if (count < 15 || !finite3(peak[i], trough[i], m.idx[i])) continue;
    const variance = Math.max(0, sumSq / count - (sum / count) ** 2);
    const scale = Math.sqrt(variance) * Math.sqrt(DRAW_WINDOW);
    if (!(scale > 1e-12)) continue;
    long[i] = clamp((peak[i] - m.idx[i]) / scale);
    short[i] = clamp((m.idx[i] - trough[i]) / scale);
  }
  return { long, short };
}

function rawContext(k: SeriesCache, market: MarketRef | null): Context {
  const b = k.b;
  const { ready, dir } = trendSeries(k);
  const n = b.n;
  const fast = k.ema(12);
  const { adx, pdi, mdi } = k.dmi(14);
  const atr = k.atr(14);
  const z = k.z(20);
  const { hi, lo } = k.don(20);
  const ranges = k.rangeSma(20);
  const marketDd = marketDrawdowns(b, market);
  const adaptive = new Int8Array(n);
  const priceDdLong = new Float32Array(n).fill(NaN);
  const priceDdShort = new Float32Array(n).fill(NaN);
  const activityLast = new Float32Array(n).fill(NaN);
  const activityPart = new Float32Array(n).fill(NaN);
  const activityChange = new Float32Array(n).fill(NaN);
  const prefixVolume = new Float64Array(n + 1);
  const prefixAtr = new Float64Array(n + 1);
  for (let i = 0; i < n; i++) {
    prefixVolume[i + 1] = prefixVolume[i] + (Number.isFinite(b.v[i]) ? b.v[i] : 0);
    prefixAtr[i + 1] = prefixAtr[i] + (Number.isFinite(atr[i]) ? atr[i] : 0);
  }
  const peaks = rollingExtremes(b.h, DRAW_WINDOW, true);
  const troughs = rollingExtremes(b.l, DRAW_WINDOW, false);
  const atrRatio = new Float32Array(n).fill(NaN);
  const volRatio = new Float32Array(n).fill(NaN);
  for (let i = WARMUP - 1; i < n; i++) {
    if (!ready[i] || !finite2(atr[i], b.c[i]) || !(atr[i] > 0)) continue;
    const atrFrom = Math.max(0, i - ACT_WINDOW);
    const priorAtrCount = i - atrFrom;
    const priorAtr = priorAtrCount > 0 ? (prefixAtr[i] - prefixAtr[atrFrom]) / priorAtrCount : NaN;
    if (priorAtr > 0) atrRatio[i] = atr[i] / priorAtr;
    const volFrom = Math.max(0, i - ACT_WINDOW);
    const priorVolCount = i - volFrom;
    const priorVol = priorVolCount > 0 ? (prefixVolume[i] - prefixVolume[volFrom]) / priorVolCount : NaN;
    if (priorVol > 0) volRatio[i] = b.v[i] / priorVol;
    if (i >= DRAW_WINDOW && finite3(peaks[i], troughs[i], atr[i - 1]) && atr[i - 1] > 0) {
      priceDdLong[i] = clamp((peaks[i] - b.c[i]) / atr[i - 1]);
      priceDdShort[i] = clamp((b.c[i] - troughs[i]) / atr[i - 1]);
    }
    if (i >= ACT_WINDOW + 4) {
      const recent4 = prefixVolume[i + 1] - prefixVolume[i - 3];
      const prior4 = prefixVolume[i - 3] - prefixVolume[i - 7];
      const prior16 = prefixVolume[i - 3] - prefixVolume[i - 19];
      const prior20Mean = (prefixVolume[i] - prefixVolume[i - ACT_WINDOW]) / ACT_WINDOW;
      if (prior20Mean > 0) activityLast[i] = b.v[i] / prior20Mean;
      if (prior16 > 0) activityPart[i] = (recent4 / 4) / (prior16 / 16);
      if (prior4 > 0) activityChange[i] = recent4 / prior4;
    }
    if (
      !Number.isFinite(adx[i]) || !Number.isFinite(pdi[i]) || !Number.isFinite(mdi[i]) ||
      !Number.isFinite(fast[i]) || !Number.isFinite(atrRatio[i]) || !Number.isFinite(volRatio[i]) ||
      !Number.isFinite(hi[i]) || !Number.isFinite(lo[i]) || !Number.isFinite(ranges[i - 1])
    ) continue;
    const d = dir[i];
    const move = b.c[i] - b.c[i - 1];
    const extension = Math.abs(b.c[i] - fast[i]) / atr[i];
    const span = b.h[i] - b.l[i];
    const location = span > 0 ? (b.c[i] - b.l[i]) / span : 0.5;
    const breakout = b.c[i] > hi[i] ? 1 : b.c[i] < lo[i] ? -1 : 0;
    const trendOk =
      d !== 0 &&
      adx[i] >= 22 &&
      d * (pdi[i] - mdi[i]) > 0 &&
      d * (b.c[i] - fast[i]) > 0 &&
      extension <= 2 &&
      volRatio[i] >= 0.7 &&
      atrRatio[i] <= 2.5;
    const pullback =
      d !== 0 &&
      adx[i] >= 18 &&
      d * (b.c[i - 1] - fast[i - 1]) <= 0 &&
      d * (b.c[i] - fast[i]) > 0 &&
      d * move > 0 &&
      volRatio[i] >= 0.6 &&
      atrRatio[i] <= 2;
    const meanRev =
      adx[i] <= 22 && atrRatio[i] <= 1.5
        ? z[i - 1] <= -1.5 && move > 0 && z[i] > z[i - 1]
        ? 1
        : z[i - 1] >= 1.5 && move < 0 && z[i] < z[i - 1]
          ? -1
          : 0
        : 0;
    const volumeBreak =
      breakout !== 0 &&
      breakout * (b.c[i] - b.o[i]) > 0 &&
      (breakout > 0 ? location >= 0.7 : location <= 0.3) &&
      volRatio[i] >= 1.15 &&
      atrRatio[i] >= 0.8;
    if (adx[i] >= 22 && d !== 0) {
      if (volumeBreak) adaptive[i] = breakout;
      else if (pullback) adaptive[i] = d;
      else if (trendOk) adaptive[i] = d;
    } else if (adx[i] <= 18 && meanRev !== 0) adaptive[i] = meanRev;
    else if (volumeBreak && volRatio[i] >= 1.3) adaptive[i] = breakout;
  }
  return {
    ready,
    dir,
    adaptive,
    priceDdLong,
    priceDdShort,
    marketDdLong: marketDd.long,
    marketDdShort: marketDd.short,
    activityLast,
    activityPart,
    activityChange,
  };
}

function context(k: SeriesCache): Context {
  return k.memo("relate:context:v1", () => {
    const b = k.b;
    const runs = spans(b);
    const market = k.market();
    if (runs.length === 1 && runs[0][0] === 0 && runs[0][1] === b.n) return rawContext(k, market);
    const out: Context = {
      ready: new Uint8Array(b.n),
      dir: new Int8Array(b.n),
      adaptive: new Int8Array(b.n),
      priceDdLong: new Float32Array(b.n).fill(NaN),
      priceDdShort: new Float32Array(b.n).fill(NaN),
      marketDdLong: new Float32Array(b.n),
      marketDdShort: new Float32Array(b.n),
      activityLast: new Float32Array(b.n).fill(NaN),
      activityPart: new Float32Array(b.n).fill(NaN),
      activityChange: new Float32Array(b.n).fill(NaN),
    };
    for (const [from, to] of runs) {
      if (to - from < WARMUP) continue;
      const partMarket = market
        ? {
            ret: market.ret.subarray(from, to),
            idx: market.idx.subarray(from, to),
            act: market.act.subarray(from, to),
            syms: market.syms,
          }
        : null;
      const part = rawContext(new SeriesCache(sliceBars(b, from, to)), partMarket);
      for (const key of ["ready", "dir", "adaptive", "priceDdLong", "priceDdShort", "marketDdLong", "marketDdShort", "activityLast", "activityPart", "activityChange"] as const)
        out[key].set(part[key], from);
    }
    return out;
  });
}

function trendViews(k: SeriesCache, tfs: readonly number[]) {
  const baseTf = k.b.tfMin;
  const out: Array<{ trend: Trend; map: Int32Array | null }> = [];
  for (const tf of tfs) {
    if (tf === baseTf) out.push({ trend: trendSeries(k), map: null });
    else if (tf > baseTf && tf % baseTf === 0) {
      const view = k.htf(tf / baseTf);
      out.push({ trend: trendSeries(view.k), map: view.map });
    } else return null;
  }
  return out;
}

function relateState(
  k: SeriesCache,
  tfs: readonly number[],
  mode: RelateMode,
  profile: RelateProfile,
): Int8Array {
  const key = `relate:${tfs.join("x")}:${mode}:${profile}:v1`;
  return k.memo(key, () => {
    const n = k.b.n;
    const none = new Int8Array(n);
    if (k.b.tfMin !== Math.min(...tfs)) return none;
    const views = trendViews(k, tfs);
    if (!views) return none;
    const x = context(k);
    const config = RELATE_PROFILES.find((p) => p.id === profile)!;
    const market = k.market();
    const out = new Int8Array(n);
    const source = mode === "trend" ? x.dir : x.adaptive;
    for (let i = WARMUP - 1; i < n; i++) {
      const d = source[i];
      if (!d || !x.ready[i]) continue;
      let aligned = true;
      for (const v of views) {
        const j = v.map ? v.map[i] : i;
        if (j < 0 || !v.trend.ready[j] || v.trend.dir[j] !== d) {
          aligned = false;
          break;
        }
      }
      if (!aligned) continue;
      const priceDd = d > 0 ? x.priceDdLong[i] : x.priceDdShort[i];
      const marketDd = d > 0 ? x.marketDdLong[i] : x.marketDdShort[i];
      if (
        !finite5(priceDd, marketDd, x.activityLast[i], x.activityPart[i], x.activityChange[i]) ||
        priceDd > config.priceDrawdownMax ||
        marketDd > config.marketDrawdownMax ||
        x.activityLast[i] < config.lastActivityMin ||
        x.activityLast[i] > config.lastActivityMax ||
        x.activityPart[i] < config.activityPartMin ||
        x.activityChange[i] < config.activityChangeMin
      )
        continue;
      if (market?.syms && market.syms > 1 && (!Number.isFinite(market.act[i]) || market.act[i] > config.marketActivityMax)) continue;
      out[i] = d;
    }
    return out;
  });
}

const makeSpec = (tfs: readonly number[], mode: RelateMode, profile: RelateProfile, micro: boolean): IndicationSpec => {
  const code = subsetCode(tfs);
  const base = relateId(tfs, mode, profile, micro);
  const description = `${RELATE_MODES.find((x) => x.id === mode)!.label}; ${code.replaceAll("x", "+")}m trend; ${RELATE_PROFILES.find((x) => x.id === profile)!.label} drawdown/activity ratios`;
  return {
    id: base,
    kind: "relate" as IndicationKind,
    label: `Relate · ${description}${micro ? " · Micro/Minimal" : " · Short+"}`,
    params: {
      tf1: tfs.includes(1) ? 1 : 0,
      tf5: tfs.includes(5) ? 1 : 0,
      tf15: tfs.includes(15) ? 1 : 0,
      mode: mode === "trend" ? 0 : 1,
      priceDrawdownMax: RELATE_PROFILES.find((x) => x.id === profile)!.priceDrawdownMax,
      marketDrawdownMax: RELATE_PROFILES.find((x) => x.id === profile)!.marketDrawdownMax,
      activityLastMin: RELATE_PROFILES.find((x) => x.id === profile)!.lastActivityMin,
      activityLastMax: RELATE_PROFILES.find((x) => x.id === profile)!.lastActivityMax,
      activityPartMin: RELATE_PROFILES.find((x) => x.id === profile)!.activityPartMin,
      activityChangeMin: RELATE_PROFILES.find((x) => x.id === profile)!.activityChangeMin,
      marketActivityMax: RELATE_PROFILES.find((x) => x.id === profile)!.marketActivityMax,
    },
    fn: (k) => relateState(k, tfs, mode, profile),
  };
};

export function relateSpecs(): IndicationSpec[] {
  const out: IndicationSpec[] = [];
  for (const tfs of RELATE_SUBSETS)
    for (const mode of RELATE_MODES)
      for (const profile of RELATE_PROFILES) {
        out.push(makeSpec(tfs, mode.id, profile.id, false));
        // The default Micro grid only evaluates native Micro indications on Micro and Minimal cells. These aliases
        // preserve a separate, exact identity while allowing the 5m and 15m relation configs into those ranges.
        if (Math.min(...tfs) >= 5) out.push(makeSpec(tfs, mode.id, profile.id, true));
      }
  return out;
}

export { relateState };
