// Break's relational grid: independent causal breakout/reclaim setups × eligible 1/5/15-minute trend subsets × two drawdown windows ×
// two independent activity/risk profiles. Micro aliases are separate identities, anchored at 5m or 15m so the
// default Micro lane floor remains intact. Shared features stay in one small cache across Break configs during Base,
// then are released with the rest of the indicator cache.
import type { Bars, IndicationKind } from "../domain/types.ts";
import type { IndicationSpec } from "./registry.ts";
import { SeriesCache } from "./cache.ts";
import type { MarketRef } from "./market.ts";
import {
  BREAK_CORES,
  BREAK_DRAWDOWN_WINDOWS,
  BREAK_PROFILES,
  BREAK_SUBSETS,
  breakConfigId,
  type BreakCore,
  type BreakDrawdownWindow,
  type BreakProfile,
} from "./break-config.ts";

export {
  BREAK_CONFIG_IDS,
  BREAK_DEFAULT_MICRO_CONFIG_IDS,
  BREAK_CORES,
  BREAK_DRAWDOWN_WINDOWS,
  BREAK_PROFILES,
  BREAK_SUBSETS,
  BREAK_TIMEFRAMES,
  breakAnchorTf,
  isBreakMicroInd,
  isBreakRelInd,
} from "./break-config.ts";

const profileById = new Map<string, (typeof BREAK_PROFILES)[number]>(BREAK_PROFILES.map((x) => [x.id, x]));
const subsetCode = (tfs: readonly number[]) => tfs.join("x");

type SharedContext = {
  trend1: Int8Array;
  trend5: Int8Array;
  trend15: Int8Array;
  don: Int8Array;
  don8: Int8Array;
  squeeze: Int8Array;
  retest: Int8Array;
  retest8: Int8Array;
  reclaim8: Int8Array;
  priceDd24Long: Float32Array;
  priceDd24Short: Float32Array;
  priceDd48Long: Float32Array;
  priceDd48Short: Float32Array;
  marketDd24Long: Float32Array;
  marketDd24Short: Float32Array;
  marketDd48Long: Float32Array;
  marketDd48Short: Float32Array;
  marketZ: Float32Array;
  residualZ: Float32Array;
  activityLast: Float32Array;
  activityPart: Float32Array;
  activityChange: Float32Array;
  marketActivity: Float32Array;
  marketActivityPart: Float32Array;
  atrRatio: Float32Array;
  rangeRatio: Float32Array;
};

const WARMUP = 39;
const WINDOW = 24;
const LONG_WINDOW = 48;
const ACT_WINDOW = 20;
const REL_WINDOW = 60;
const REL_MOVE = 12;
const finite2 = (a: number, b: number) => Number.isFinite(a) && Number.isFinite(b);
const finite3 = (a: number, b: number, c: number) => Number.isFinite(a) && Number.isFinite(b) && Number.isFinite(c);
const finite4 = (a: number, b: number, c: number, d: number) =>
  Number.isFinite(a) && Number.isFinite(b) && Number.isFinite(c) && Number.isFinite(d);
const finite6 = (a: number, b: number, c: number, d: number, e: number, f: number) =>
  Number.isFinite(a) && Number.isFinite(b) && Number.isFinite(c) && Number.isFinite(d) && Number.isFinite(e) && Number.isFinite(f);
const finite7 = (a: number, b: number, c: number, d: number, e: number, f: number, g: number) =>
  finite6(a, b, c, d, e, f) && Number.isFinite(g);
const clamp0 = (x: number) => (x > 0 ? x : 0);

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

/** Prior-only rolling maximum/minimum. The current bar is deliberately excluded. */
function priorExtreme(x: Float64Array, width: number, maximum: boolean): Float64Array {
  const out = new Float64Array(x.length).fill(NaN);
  const q = new Int32Array(x.length);
  let head = 0;
  let tail = 0;
  for (let i = 0; i < x.length; i++) {
    while (head < tail && q[head] < i - width) head++;
    const j = i - 1;
    if (j >= 0 && Number.isFinite(x[j])) {
      while (tail > head && (maximum ? x[q[tail - 1]] <= x[j] : x[q[tail - 1]] >= x[j])) tail--;
      q[tail++] = j;
    }
    if (head < tail) out[i] = x[q[head]];
  }
  return out;
}

function setMarketDrawdown(
  i: number,
  window: number,
  peak: Float64Array,
  trough: Float64Array,
  market: MarketRef,
  retPrefix: Float64Array,
  retSqPrefix: Float64Array,
  retCount: Uint32Array,
  minCount: number,
  longOut: Float32Array,
  shortOut: Float32Array,
): void {
  const from = Math.max(0, i - window);
  const count = retCount[i] - retCount[from];
  if (count < minCount) return;
  const sum = retPrefix[i] - retPrefix[from];
  const sumSq = retSqPrefix[i] - retSqPrefix[from];
  const mean = sum / count;
  const variance = Math.max(0, sumSq / count - mean * mean);
  const scale = Math.sqrt(variance) * Math.sqrt(window);
  if (!(scale > 1e-12) || !finite3(peak[i], trough[i], market.idx[i])) return;
  longOut[i] = clamp0((peak[i] - market.idx[i]) / scale);
  shortOut[i] = clamp0((market.idx[i] - trough[i]) / scale);
}

/** Trend on this lane, or on a higher lane mapped from its last completed candle. */
function trendOn(k: SeriesCache, tf: number): Int8Array {
  const n = k.b.n;
  if (tf < k.b.tfMin || tf % k.b.tfMin !== 0) return new Int8Array(n);
  let base = k;
  let map: Int32Array | null = null;
  if (tf > k.b.tfMin) ({ k: base, map } = k.htf(tf / k.b.tfMin));
  const b = base.b;
  const fast = base.ema(12);
  const slow = base.ema(36);
  const { adx, pdi, mdi } = base.dmi(14);
  const dir = new Int8Array(b.n);
  for (let i = WARMUP - 1; i < b.n; i++) {
    if (!finite6(fast[i], slow[i], slow[i - 3], adx[i], pdi[i], mdi[i]) || adx[i] < 18) continue;
    if (fast[i] > slow[i] && slow[i] > slow[i - 3] && pdi[i] > mdi[i]) dir[i] = 1;
    else if (fast[i] < slow[i] && slow[i] < slow[i - 3] && mdi[i] > pdi[i]) dir[i] = -1;
  }
  if (!map) return dir;
  const mapped = new Int8Array(n);
  for (let i = 0; i < n; i++) if (map[i] >= 0) mapped[i] = dir[map[i]];
  return mapped;
}

function blankContext(n: number, market: MarketRef | null): SharedContext {
  const f = () => new Float32Array(n).fill(NaN);
  return {
    trend1: new Int8Array(n),
    trend5: new Int8Array(n),
    trend15: new Int8Array(n),
    don: new Int8Array(n),
    don8: new Int8Array(n),
    squeeze: new Int8Array(n),
    retest: new Int8Array(n),
    retest8: new Int8Array(n),
    reclaim8: new Int8Array(n),
    priceDd24Long: f(),
    priceDd24Short: f(),
    priceDd48Long: f(),
    priceDd48Short: f(),
    marketDd24Long: market && market.syms > 1 ? f() : new Float32Array(n),
    marketDd24Short: market && market.syms > 1 ? f() : new Float32Array(n),
    marketDd48Long: market && market.syms > 1 ? f() : new Float32Array(n),
    marketDd48Short: market && market.syms > 1 ? f() : new Float32Array(n),
    marketZ: market && market.syms > 1 ? f() : new Float32Array(n),
    residualZ: market && market.syms > 1 ? f() : new Float32Array(n),
    activityLast: f(),
    activityPart: f(),
    activityChange: f(),
    marketActivity: market && market.syms > 1 ? f() : new Float32Array(n).fill(1),
    marketActivityPart: market && market.syms > 1 ? f() : new Float32Array(n).fill(1),
    atrRatio: f(),
    rangeRatio: f(),
  };
}

function rawContext(k: SeriesCache, market: MarketRef | null): SharedContext {
  const b = k.b;
  const n = b.n;
  const out = blankContext(n, market);
  if (b.tfMin <= 1) out.trend1 = trendOn(k, 1);
  if (b.tfMin <= 5) out.trend5 = trendOn(k, 5);
  if (b.tfMin <= 15) out.trend15 = trendOn(k, 15);

  const dn = k.don(20);
  const dn8 = k.don(8);
  const bb = k.bb(20, 2);
  const minW60 = priorExtreme(bb.width, 60, false);
  const atr = k.atr(14);
  const volumePrefix = new Float64Array(n + 1);
  const atrPrefix = new Float64Array(n + 1);
  const rangePrefix = new Float64Array(n + 1);
  const marketActPrefix = market ? new Float64Array(n + 1) : null;
  const marketActCount = market ? new Uint32Array(n + 1) : null;
  const hh24 = priorExtreme(b.h, WINDOW, true);
  const ll24 = priorExtreme(b.l, WINDOW, false);
  const hh48 = priorExtreme(b.h, LONG_WINDOW, true);
  const ll48 = priorExtreme(b.l, LONG_WINDOW, false);
  const marketPeak = market && market.syms > 1 ? priorExtreme(market.idx, WINDOW, true) : null;
  const marketTrough = market && market.syms > 1 ? priorExtreme(market.idx, WINDOW, false) : null;
  const marketPeak48 = market && market.syms > 1 ? priorExtreme(market.idx, LONG_WINDOW, true) : null;
  const marketTrough48 = market && market.syms > 1 ? priorExtreme(market.idx, LONG_WINDOW, false) : null;
  const marketRetPrefix = market && market.syms > 1 ? new Float64Array(n + 1) : null;
  const marketRetSqPrefix = market && market.syms > 1 ? new Float64Array(n + 1) : null;
  const marketRetCount = market && market.syms > 1 ? new Uint32Array(n + 1) : null;

  let lastUp = -1;
  let lastDn = -1;
  let levelUp = 0;
  let levelDn = 0;
  let retestUpUsed = false;
  let retestDnUsed = false;
  let lastUp8 = -1;
  let lastDn8 = -1;
  let levelUp8 = 0;
  let levelDn8 = 0;
  let retestUp8Used = false;
  let retestDn8Used = false;
  let sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0, relCount = 0;
  const relTerms = new Float64Array(REL_WINDOW * 5);
  const relValid = new Uint8Array(REL_WINDOW);
  const removeRelation = (slotIndex: number) => {
    if (!relValid[slotIndex]) return;
    const slot = slotIndex * 5;
    sx -= relTerms[slot]; sy -= relTerms[slot + 1]; sxx -= relTerms[slot + 2];
    syy -= relTerms[slot + 3]; sxy -= relTerms[slot + 4]; relCount--;
    relValid[slotIndex] = 0;
  };
  const addRelation = (i: number) => {
    const slotIndex = i % REL_WINDOW;
    const slot = slotIndex * 5;
    const x = market?.ret[i] ?? NaN;
    const y = i > 0 ? Math.log(b.c[i] / b.c[i - 1]) : NaN;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    const x2 = x * x, y2 = y * y, xy = x * y;
    sx += x; sy += y; sxx += x2; syy += y2; sxy += xy;
    relCount++;
    relTerms[slot] = x;
    relTerms[slot + 1] = y;
    relTerms[slot + 2] = x2;
    relTerms[slot + 3] = y2;
    relTerms[slot + 4] = xy;
    relValid[slotIndex] = 1;
  };

  for (let i = 0; i < n; i++) {
    volumePrefix[i + 1] = volumePrefix[i] + b.v[i];
    atrPrefix[i + 1] = atrPrefix[i] + (Number.isFinite(atr[i]) ? atr[i] : 0);
    rangePrefix[i + 1] = rangePrefix[i] + (Number.isFinite(b.h[i] - b.l[i]) ? b.h[i] - b.l[i] : 0);
    if (marketActPrefix && marketActCount && market) {
      const act = market.act[i];
      marketActPrefix[i + 1] = marketActPrefix[i] + (Number.isFinite(act) ? act : 0);
      marketActCount[i + 1] = marketActCount[i] + (Number.isFinite(act) ? 1 : 0);
    }
    if (marketRetPrefix && marketRetSqPrefix && marketRetCount && market) {
      const ret = market.ret[i];
      const validRet = Number.isFinite(ret);
      marketRetPrefix[i + 1] = marketRetPrefix[i] + (validRet ? ret : 0);
      marketRetSqPrefix[i + 1] = marketRetSqPrefix[i] + (validRet ? ret * ret : 0);
      marketRetCount[i + 1] = marketRetCount[i] + (validRet ? 1 : 0);
    }

    if (i >= WARMUP - 1 && Number.isFinite(atr[i]) && atr[i] > 0 && b.c[i] > 0) {
      const from = Math.max(0, i - ACT_WINDOW);
      const count = i - from;
      const priorAtr = count ? (atrPrefix[i] - atrPrefix[from]) / count : NaN;
      const priorRange = count ? (rangePrefix[i] - rangePrefix[from]) / count : NaN;
      const priorVol = count ? (volumePrefix[i] - volumePrefix[from]) / count : NaN;
      if (priorAtr > 0) out.atrRatio[i] = atr[i] / priorAtr;
      if (priorRange > 0) out.rangeRatio[i] = (b.h[i] - b.l[i]) / priorRange;
      if (priorVol > 0) out.activityLast[i] = b.v[i] / priorVol;

      if (i >= ACT_WINDOW) {
        const recent4 = volumePrefix[i + 1] - volumePrefix[i - 3];
        const prior16 = volumePrefix[i - 3] - volumePrefix[i - 19];
        const prior4 = volumePrefix[i - 3] - volumePrefix[i - 7];
        if (prior16 > 0) out.activityPart[i] = (recent4 / 4) / (prior16 / 16);
        if (prior4 > 0) out.activityChange[i] = recent4 / prior4;
      }

      const priorAtrForDd = i > 0 ? atr[i - 1] : NaN;
      if (finite3(hh24[i], ll24[i], priorAtrForDd) && priorAtrForDd > 0) {
        out.priceDd24Long[i] = clamp0((hh24[i] - b.c[i]) / priorAtrForDd);
        out.priceDd24Short[i] = clamp0((b.c[i] - ll24[i]) / priorAtrForDd);
      }
      if (finite3(hh48[i], ll48[i], priorAtrForDd) && priorAtrForDd > 0) {
        out.priceDd48Long[i] = clamp0((hh48[i] - b.c[i]) / priorAtrForDd);
        out.priceDd48Short[i] = clamp0((b.c[i] - ll48[i]) / priorAtrForDd);
      }

      if (market && market.syms > 1 && marketPeak && marketTrough && marketPeak48 && marketTrough48 &&
          marketActPrefix && marketActCount && marketRetPrefix && marketRetSqPrefix && marketRetCount) {
        const actFrom = Math.max(0, i - ACT_WINDOW);
        const actCount = marketActCount[i] - marketActCount[actFrom];
        const priorMarketAct = actCount ? (marketActPrefix[i] - marketActPrefix[actFrom]) / actCount : NaN;
        if (priorMarketAct > 0 && Number.isFinite(market.act[i]))
          out.marketActivity[i] = market.act[i] / priorMarketAct;
        const recentMarketN = marketActCount[i] - marketActCount[Math.max(0, i - 4)];
        const priorMarketN = marketActCount[Math.max(0, i - 4)] - marketActCount[Math.max(0, i - 20)];
        const recentMarket = recentMarketN
          ? (marketActPrefix[i] - marketActPrefix[Math.max(0, i - 4)]) / recentMarketN
          : NaN;
        const priorMarket = priorMarketN
          ? (marketActPrefix[Math.max(0, i - 4)] - marketActPrefix[Math.max(0, i - 20)]) / priorMarketN
          : NaN;
        if (priorMarket > 0 && Number.isFinite(recentMarket)) out.marketActivityPart[i] = recentMarket / priorMarket;

        setMarketDrawdown(i, WINDOW, marketPeak, marketTrough, market,
          marketRetPrefix, marketRetSqPrefix, marketRetCount, 15,
          out.marketDd24Long, out.marketDd24Short);
        setMarketDrawdown(i, LONG_WINDOW, marketPeak48, marketTrough48, market,
          marketRetPrefix, marketRetSqPrefix, marketRetCount, 30,
          out.marketDd48Long, out.marketDd48Short);

        const j = i - REL_MOVE;
        if (j >= 0 && relCount >= 30 && finite2(market.idx[i], market.idx[j])) {
          const mx = sx / relCount, my = sy / relCount;
          const vx = sxx / relCount - mx * mx;
          const vy = syy / relCount - my * my;
          const cov = sxy / relCount - mx * my;
          if (vx > 1e-12 && vy > 1e-12) {
            const beta = cov / vx;
            const sdMarket = Math.sqrt(vx);
            const sdResidual = Math.sqrt(Math.max(0, vy - (cov * cov) / vx));
            const marketMove = market.idx[i] - market.idx[j];
            if (sdMarket > 1e-12) out.marketZ[i] = marketMove / (sdMarket * Math.sqrt(REL_MOVE));
            if (sdResidual > 1e-12) {
              const symbolMove = Math.log(b.c[i] / b.c[j]);
              out.residualZ[i] = (symbolMove - beta * marketMove) / (sdResidual * Math.sqrt(REL_MOVE));
            }
          }
        }
      }
    }

    // Keep the rolling market/symbol regression strictly prior-only. The current bar is the
    // observation being scored; including it in the baseline would partially normalize away
    // the very shock the market-relative z-scores are meant to detect.
    if (market && market.syms > 1) {
      // Evict the old slot before writing the current return into the same ring position.
      if (i >= REL_WINDOW) removeRelation(i % REL_WINDOW);
      addRelation(i);
    }

    if (i < 1 || !finite6(dn.hi[i], dn.lo[i], b.o[i], b.h[i], b.l[i], b.c[i])) continue;
    const range = b.h[i] - b.l[i];
    if (!(range > 0)) continue;
    const location = (b.c[i] - b.l[i]) / range;
    const up = b.c[i] > dn.hi[i] && b.c[i] > b.o[i] && location >= 0.65;
    const down = b.c[i] < dn.lo[i] && b.c[i] < b.o[i] && location <= 0.35;
    // Donchian emits the crossing only. A staircase of new highs is one breakout run, not an entry each bar.
    if (up && !(b.c[i - 1] > dn.hi[i - 1])) out.don[i] = 1;
    else if (down && !(b.c[i - 1] < dn.lo[i - 1])) out.don[i] = -1;

    if (i >= 61 && Number.isFinite(minW60[i - 1]) && Number.isFinite(bb.width[i - 1]) &&
        bb.width[i - 1] <= minW60[i - 1] * 1.15) {
      if (b.c[i] > bb.up[i] && b.c[i] > b.o[i] && location >= 0.65) out.squeeze[i] = 1;
      else if (b.c[i] < bb.lo[i] && b.c[i] < b.o[i] && location <= 0.35) out.squeeze[i] = -1;
    }

    const upCross = b.c[i] > dn.hi[i] && !(b.c[i - 1] > dn.hi[i - 1]);
    const downCross = b.c[i] < dn.lo[i] && !(b.c[i - 1] < dn.lo[i - 1]);
    // A fresh break in the opposite direction invalidates the pending retest. Otherwise a rebound through an old
    // level could emit a stale long/short after the market has already broken the other side of the range.
    if (upCross) { lastUp = i; levelUp = dn.hi[i]; retestUpUsed = false; lastDn = -1; }
    if (downCross) { lastDn = i; levelDn = dn.lo[i]; retestDnUsed = false; lastUp = -1; }
    if (lastUp >= 0 && !retestUpUsed && i - lastUp >= 1 && i - lastUp <= 8 &&
        b.l[i] <= levelUp * 1.001 && b.c[i] > levelUp && b.c[i] > b.o[i] && location >= 0.6) {
      out.retest[i] = 1;
      retestUpUsed = true;
    }
    if (lastDn >= 0 && !retestDnUsed && i - lastDn >= 1 && i - lastDn <= 8 &&
        b.h[i] >= levelDn * 0.999 && b.c[i] < levelDn && b.c[i] < b.o[i] && location <= 0.4) {
      out.retest[i] = -1;
      retestDnUsed = true;
    }

    // Short channels make the Micro family responsive without weakening its independent trend, activity,
    // market-relative or drawdown gates. As with the 20-bar source, a staircase is one run; retests are one-shot.
    if (!finite4(dn8.hi[i], dn8.lo[i], dn8.hi[i - 1], dn8.lo[i - 1])) continue;
    const up8 = b.c[i] > dn8.hi[i] && b.c[i] > b.o[i] && location >= 0.65;
    const down8 = b.c[i] < dn8.lo[i] && b.c[i] < b.o[i] && location <= 0.35;
    const upCross8 = b.c[i] > dn8.hi[i] && !(b.c[i - 1] > dn8.hi[i - 1]);
    const downCross8 = b.c[i] < dn8.lo[i] && !(b.c[i - 1] < dn8.lo[i - 1]);
    // Same-bar failed-break reclaim: the channel and volatility baseline exclude the scored candle.
    // This source has no pending setup to carry across a gap or an opposite break. Each completed sweep
    // must independently recover its prior channel edge, keep a directional body and close near its extreme.
    const priorAtr = atr[i - 1];
    const rangeAtr = range / priorAtr;
    if (priorAtr > 0 && Number.isFinite(rangeAtr) && rangeAtr > 0) {
      const lowerPenetration = (dn8.lo[i] - b.l[i]) / priorAtr;
      const upperPenetration = (b.h[i] - dn8.hi[i]) / priorAtr;
      if (lowerPenetration > 0 && lowerPenetration <= 1.5 &&
          b.c[i] > dn8.lo[i] && b.c[i] > b.o[i] && location >= 0.65) out.reclaim8[i] = 1;
      else if (upperPenetration > 0 && upperPenetration <= 1.5 &&
          b.c[i] < dn8.hi[i] && b.c[i] < b.o[i] && location <= 0.35) out.reclaim8[i] = -1;
    }
    if (up8 && !(b.c[i - 1] > dn8.hi[i - 1])) out.don8[i] = 1;
    else if (down8 && !(b.c[i - 1] < dn8.lo[i - 1])) out.don8[i] = -1;
    if (upCross8) { lastUp8 = i; levelUp8 = dn8.hi[i]; retestUp8Used = false; lastDn8 = -1; }
    if (downCross8) { lastDn8 = i; levelDn8 = dn8.lo[i]; retestDn8Used = false; lastUp8 = -1; }
    if (lastUp8 >= 0 && !retestUp8Used && i - lastUp8 >= 1 && i - lastUp8 <= 8 &&
        b.l[i] <= levelUp8 * 1.001 && b.c[i] > levelUp8 && b.c[i] > b.o[i] && location >= 0.6) {
      out.retest8[i] = 1;
      retestUp8Used = true;
    }
    if (lastDn8 >= 0 && !retestDn8Used && i - lastDn8 >= 1 && i - lastDn8 <= 8 &&
        b.h[i] >= levelDn8 * 0.999 && b.c[i] < levelDn8 && b.c[i] < b.o[i] && location <= 0.4) {
      out.retest8[i] = -1;
      retestDn8Used = true;
    }
  }
  return out;
}

function buildContext(k: SeriesCache): SharedContext {
  const b = k.b;
  const market = k.market();
  const runs = spans(b);
  if (runs.length === 1 && runs[0][0] === 0 && runs[0][1] === b.n) return rawContext(k, market);
  const out = blankContext(b.n, market);
  for (const [from, to] of runs) {
    if (to - from < WARMUP) continue;
    const marketSlice = market
      ? {
          ret: market.ret.slice(from, to),
          idx: market.idx.subarray(from, to),
          act: market.act.subarray(from, to),
          syms: market.syms,
        }
      : null;
    // MarketSource is indexed by bars and can calculate a return across a timestamp gap. The first bar of this
    // isolated segment has no in-segment predecessor, so exclude that cross-gap return from relation/volatility.
    if (marketSlice?.ret.length) marketSlice.ret[0] = NaN;
    const part = rawContext(
      new SeriesCache(sliceBars(b, from, to)),
      marketSlice,
    );
    for (const key of Object.keys(out) as Array<keyof SharedContext>) out[key].set(part[key], from);
  }
  return out;
}

function context(k: SeriesCache): SharedContext {
  // This cache prefix is deliberately retained by Base's bounded-memory release loop. One shared context replaces
  // the same arrays being recomputed once per Break profile and timeframe subset.
  return k.memo("break:shared:context:v1", () => buildContext(k));
}

function relationState(
  k: SeriesCache,
  core: BreakCore,
  tfs: readonly number[],
  ddWindow: BreakDrawdownWindow,
  profileId: BreakProfile,
): Int8Array {
  const n = k.b.n;
  const out = new Int8Array(n);
  const anchor = Math.min(...tfs);
  if (k.b.tfMin !== anchor) return out;
  const x = context(k);
  const profile = profileById.get(profileId)!;
  const longDrawdown = ddWindow === "48";
  const events = core === "don"
    ? x.don
    : core === "don8"
      ? x.don8
      : core === "squeeze"
        ? x.squeeze
        : core === "retest8"
          ? x.retest8
          : core === "reclaim8"
            ? x.reclaim8
            : x.retest;
  const market = k.market();
  for (let i = WARMUP - 1; i < n; i++) {
    const dir = events[i];
    if (!dir) continue;
    let aligned = true;
    for (const tf of tfs) {
      const trend = tf === 1 ? x.trend1 : tf === 5 ? x.trend5 : x.trend15;
      if (trend[i] !== dir) { aligned = false; break; }
    }
    if (!aligned) continue;
    const priceDd = dir > 0
      ? longDrawdown ? x.priceDd48Long[i] : x.priceDd24Long[i]
      : longDrawdown ? x.priceDd48Short[i] : x.priceDd24Short[i];
    const marketDd = dir > 0
      ? longDrawdown ? x.marketDd48Long[i] : x.marketDd24Long[i]
      : longDrawdown ? x.marketDd48Short[i] : x.marketDd24Short[i];
    if (!finite7(priceDd, marketDd, x.activityLast[i], x.activityPart[i], x.activityChange[i], x.atrRatio[i], x.rangeRatio[i])) continue;
    if (
      priceDd > profile.priceDrawdownMax || marketDd > profile.marketDrawdownMax ||
      x.activityLast[i] < profile.activityLastMin || x.activityLast[i] > profile.activityLastMax ||
      x.activityPart[i] < profile.activityPartMin || x.activityChange[i] < profile.activityChangeMin ||
      x.atrRatio[i] < profile.atrRatioMin || x.atrRatio[i] > profile.atrRatioMax ||
      x.rangeRatio[i] < profile.rangeRatioMin || x.rangeRatio[i] > profile.rangeRatioMax
    ) continue;
    if (market && market.syms > 1) {
      if (!finite4(x.marketActivity[i], x.marketActivityPart[i], x.marketZ[i], x.residualZ[i])) continue;
      if (x.marketActivity[i] > profile.marketActivityMax || x.marketActivityPart[i] > profile.marketActivityPartMax) continue;
      if (dir * x.marketZ[i] < -profile.marketAgainstZMax || dir * x.residualZ[i] < profile.residualMin) continue;
    }
    out[i] = dir;
  }
  return out;
}

const spec = (
  core: BreakCore,
  tfs: readonly number[],
  ddWindow: BreakDrawdownWindow,
  profileId: BreakProfile,
  micro: boolean,
): IndicationSpec => {
  const profile = profileById.get(profileId)!;
  const id = breakConfigId(core, tfs, ddWindow, profileId, micro);
  const tfCode = subsetCode(tfs);
  const coreLabel = BREAK_CORES.find((x) => x.id === core)!.label;
  const label = `Break · ${coreLabel} · ${tfCode.replaceAll("x", "+")}m trend · ${ddWindow}bar DD · ${profile.label}${micro ? " · Micro" : ""}`;
  return {
    id,
    kind: "break" as IndicationKind,
    label,
    params: {
      tf1: tfs.includes(1) ? 1 : 0,
      tf5: tfs.includes(5) ? 1 : 0,
      tf15: tfs.includes(15) ? 1 : 0,
      core: BREAK_CORES.findIndex((x) => x.id === core),
      profile: BREAK_PROFILES.findIndex((x) => x.id === profileId),
      donPeriod: core === "don8" || core === "retest8" || core === "reclaim8" ? 8 : 20,
      ...(core === "reclaim8" ? { reclaimMaxAtr: 1.5, reclaimCloseLocation: 0.65 } : {}),
      squeezeLookback: 60,
      retestBars: 8,
      drawdownBars: Number(ddWindow),
      priceDrawdownMax: profile.priceDrawdownMax,
      marketDrawdownMax: profile.marketDrawdownMax,
      marketAgainstZMax: profile.marketAgainstZMax,
      residualMin: profile.residualMin,
      activityLastMin: profile.activityLastMin,
      activityLastMax: profile.activityLastMax,
      activityPartMin: profile.activityPartMin,
      activityChangeMin: profile.activityChangeMin,
      marketActivityMax: profile.marketActivityMax,
      marketActivityPartMax: profile.marketActivityPartMax,
      atrRatioMin: profile.atrRatioMin,
      atrRatioMax: profile.atrRatioMax,
      rangeRatioMin: profile.rangeRatioMin,
      rangeRatioMax: profile.rangeRatioMax,
    },
    fn: (k) => relationState(k, core, tfs, ddWindow, profileId),
  };
};

export function breakSpecs(): IndicationSpec[] {
  const out: IndicationSpec[] = [];
  for (const tfs of BREAK_SUBSETS)
    for (const core of BREAK_CORES)
      for (const ddWindow of BREAK_DRAWDOWN_WINDOWS)
        for (const profile of BREAK_PROFILES) {
          if (!core.microOnly) out.push(spec(core.id, tfs, ddWindow.id, profile.id, false));
          // Micro keeps independent 5m and 15m anchors; no 1m alias slips below the standard Micro floor.
          if (Math.min(...tfs) >= 5) out.push(spec(core.id, tfs, ddWindow.id, profile.id, true));
        }
  return out;
}

export { relationState as breakState };
