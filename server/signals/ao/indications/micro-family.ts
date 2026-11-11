// First-class Micro indication. Independent of the legacy mc-* Active sources and of strategy range tags.
// All windows use completed bars; shared O(n) features do not share signal/config qualification.
import type { IndicationSpec } from "./registry.ts";
import type { SeriesCache } from "./cache.ts";
import { RT_COST } from "../model-cost.ts";
import { tacticState } from "./tactic.ts";
import { INTRADAY_SUBSETS, intradayAnchor } from "./intraday-timeframes.ts";

export const MICRO_SUBSETS = INTRADAY_SUBSETS;
export const MICRO_PROFILES = [
  { id: "fast", window: 12, stretch: 1, residual: 1.25, activity: 1.4, recovery: 0.12 },
  { id: "selective", window: 24, stretch: 1.6, residual: 1.8, activity: 1.8, recovery: 0.25 },
] as const;
export type MicroProfile = (typeof MICRO_PROFILES)[number]["id"];
export const MICRO_METHODS = [
  "drawdown-turn",
  "drawdown-trend",
  "range-reclaim",
  "impulse-retest",
  "squeeze-break",
  "residual-revert",
  "market-follow",
  "activity-absorb",
  "drawdown-residual",
  "drawdown-market",
  "pullback-activity",
  "adaptive",
  "break-close",
  "break-retest",
  "activity-acceleration",
  "activity-flow",
  "change-ema",
  "change-roc",
  "break-activity",
  "break-direction",
  "market-momentum",
  "relative-trend",
  "flow-break",
  "trend-reclaim",
  "market-continuation",
  "direction-persist",
  "break-channel",
  "break-channel-short",
  "break-channel-wide",
  "break-channel-activity",
  "break-channel-failure",
  "break-close-tactic",
  "break-retest-tactic",
  "activity-acceleration-relative",
  "break-retest-market",
] as const;
/** Additional independently qualified children; the original primitives remain unchanged. */
export const MICRO_CONTEXT_METHODS = [
  "break-close-tactic",
  "break-retest-tactic",
  "activity-acceleration-relative",
  "break-retest-market",
] as const;
export type MicroMethod = (typeof MICRO_METHODS)[number];
export const microFamilyId = (tfs: readonly number[], method: MicroMethod, profile: MicroProfile) =>
  `micro-${tfs.join("x")}-${method}-${profile}`;
export const MICRO_CONFIG_IDS = MICRO_SUBSETS.flatMap((tfs) =>
  MICRO_METHODS.flatMap((method) => MICRO_PROFILES.map((p) => microFamilyId(tfs, method, p.id))),
);
export const isMicroFamilyInd = (id: string) => id.startsWith("micro-");
export const isMicroCalculationInd = (id: string) =>
  isMicroFamilyInd(id) || id.startsWith("inverse-micro-");
export const INVERSE_MICRO_CONFIG_IDS = MICRO_CONFIG_IDS.map((id) => `inverse-${id}`);
export function microAnchorTf(id: string): number | null {
  const match = /^(?:inverse-)?micro-([\dx]+)-/.exec(id);
  return match ? intradayAnchor(match[1]) : null;
}

type Frame = {
  states: Record<MicroMethod, Int8Array>;
  bias: Record<MicroMethod, Int8Array>;
  ready: Uint8Array;
  priceDdLong: Float64Array;
  priceDdShort: Float64Array;
  marketDdLong: Float64Array;
  marketDdShort: Float64Array;
  residualZ: Float64Array;
};
const direction = (x: number) => (x > 0 ? 1 : x < 0 ? -1 : 0);
const agree = (a: number, b: number) => (a !== 0 && a === b ? a : 0);

/** Prior-window extrema in O(n). The current bar never sets its own breakout/drawdown reference. */
export function microPriorExtrema(xs: Float64Array, window: number) {
  if (!Number.isSafeInteger(window) || window < 1)
    throw new RangeError("positive extrema window required");
  const high = new Float64Array(xs.length).fill(NaN);
  const low = new Float64Array(xs.length).fill(NaN);
  const hi = new Int32Array(xs.length),
    lo = new Int32Array(xs.length);
  let hh = 0,
    ht = 0,
    lh = 0,
    lt = 0,
    run = 0;
  for (let i = 0; i < xs.length; i++) {
    while (hh < ht && hi[hh] < i - window) hh++;
    while (lh < lt && lo[lh] < i - window) lh++;
    if (run >= window && hh < ht && lh < lt) {
      high[i] = xs[hi[hh]];
      low[i] = xs[lo[lh]];
    }
    if (!Number.isFinite(xs[i])) {
      hh = ht = lh = lt = run = 0;
      continue;
    }
    while (ht > hh && xs[hi[ht - 1]] <= xs[i]) ht--;
    while (lt > lh && xs[lo[lt - 1]] >= xs[i]) lt--;
    hi[ht++] = i;
    lo[lt++] = i;
    run++;
  }
  return { high, low };
}

/** Completed OHLC channel, separate from close-only extrema. Read-only features may be shared across profiles. */
function priorChannel(k: SeriesCache, window: number) {
  return k.memo(`micro-family:ohlc-channel:${window}`, () => ({
    high: microPriorExtrema(k.b.h, window).high,
    low: microPriorExtrema(k.b.l, window).low,
  }));
}

/** Also accepts genuine subminute Bars (e.g. tfMin=1/12 from recorded trades), without inventing minute paths. */
export function microFrame(k: SeriesCache, profile: MicroProfile): Frame {
  return k.memo(`micro-family:frame:v4:${profile}`, () => {
    const p = MICRO_PROFILES.find((x) => x.id === profile);
    if (!p) throw new Error(`unknown Micro profile ${profile}`);
    const { b } = k,
      n = b.n,
      w = p.window,
      span = b.tfMin * 60_000;
    const states = Object.fromEntries(
      MICRO_METHODS.map((m) => [m, new Int8Array(n)]),
    ) as Frame["states"];
    const bias = Object.fromEntries(
      MICRO_METHODS.map((m) => [m, new Int8Array(n)]),
    ) as Frame["bias"];
    const ready = new Uint8Array(n);
    const priceDdLong = new Float64Array(n).fill(NaN),
      priceDdShort = priceDdLong.slice();
    const marketDdLong = priceDdLong.slice(),
      marketDdShort = priceDdLong.slice(),
      residualZ = priceDdLong.slice();
    const log = new Float64Array(n).fill(NaN),
      mr = new Float64Array(n).fill(NaN);
    const market = k.market();
    const tacticRange = profile === "fast" ? "short" : "medium";
    const adaptiveTactic = tacticState(k, "adaptive", tacticRange);
    const trendTactic = tacticState(k, "trend", tacticRange);
    for (let i = 0; i < n; i++) {
      if (b.c[i] > 0) log[i] = Math.log(b.c[i]);
      if (market && market.syms > 1 && Number.isFinite(market.ret[i])) mr[i] = market.idx[i];
    }
    const px = microPriorExtrema(log, w),
      mx = microPriorExtrema(mr, w);
    const shortChannel = priorChannel(k, w / 2),
      channel = priorChannel(k, w),
      wideChannel = priorChannel(k, 2 * w);
    // Prefix moments end BEFORE the move: current price cannot inflate its own residual threshold.
    const rp = new Float64Array(n + 1),
      rq = new Float64Array(n + 1);
    let lastActivity = 1,
      signedFlow = 0,
      lastCross = -Infinity,
      lastCrossSide = 0;
    let run = 0,
      mrun = 0,
      fast = 0,
      slow = 0,
      atr = 0,
      vol = 0,
      rangeFast = 0,
      rangeSlow = 0;
    for (let i = 0; i < n; i++) {
      const valid =
        Number.isSafeInteger(b.t[i]) &&
        Number.isFinite(b.h[i]) &&
        Number.isFinite(b.l[i]) &&
        Number.isFinite(log[i]) &&
        b.o[i] > 0 &&
        b.l[i] > 0 &&
        b.h[i] >= Math.max(b.o[i], b.c[i]) &&
        b.l[i] <= Math.min(b.o[i], b.c[i]) &&
        Number.isFinite(b.v[i]) &&
        b.v[i] >= 0;
      const contiguous = i > 0 && b.t[i] - b.t[i - 1] === span;
      if (!valid || !contiguous || run === 0) {
        lastActivity = 1;
        signedFlow = 0;
        lastCross = -Infinity;
        lastCrossSide = 0;
        run = 0;
        mrun = 0;
        fast = slow = b.c[i];
        atr = b.h[i] - b.l[i];
        vol = b.v[i];
        rangeFast = rangeSlow = atr;
      }
      rp[i + 1] = rp[i];
      rq[i + 1] = rq[i];
      if (!valid) continue;
      const delta = i && Number.isFinite(log[i - 1]) ? log[i] - log[i - 1] : 0;
      const res = Number.isFinite(market?.ret[i]) ? delta - market!.ret[i] : 0;
      rp[i + 1] += res;
      rq[i + 1] += res * res;
      run++;
      mrun = contiguous && Number.isFinite(mr[i]) && Number.isFinite(mr[i - 1]) ? mrun + 1 : 0;
      const prevFast = fast,
        prevSlow = slow,
        prevAtr = atr,
        prevVol = vol;
      const range = b.h[i] - b.l[i];
      const tr =
        run > 1
          ? Math.max(range, Math.abs(b.h[i] - b.c[i - 1]), Math.abs(b.l[i] - b.c[i - 1]))
          : range;
      fast += ((b.c[i] - fast) * 2) / 9;
      slow += ((b.c[i] - slow) * 2) / 22;
      atr += (tr - atr) / 14;
      vol += ((b.v[i] - vol) * 2) / 21;
      const turnEma =
        prevFast <= prevSlow && fast > slow ? 1 : prevFast >= prevSlow && fast < slow ? -1 : 0;
      if (turnEma) {
        lastCross = i;
        lastCrossSide = turnEma;
      }
      // Signed participation is measured at the completed close, with an independent rolling state per profile.
      const location = range > 0 ? (2 * b.c[i] - b.h[i] - b.l[i]) / range : 0;
      signedFlow += ((location * b.v[i] - signedFlow) * 2) / (w + 1);
      const compression = rangeSlow > 0 && rangeFast / rangeSlow < 0.85;
      rangeFast += ((range - rangeFast) * 2) / 9;
      rangeSlow += ((range - rangeSlow) * 2) / 25;
      if (run < Math.max(32, 2 * w) || !(prevAtr > 0) || !Number.isFinite(px.high[i])) continue;
      ready[i] = 1;
      const unit = prevAtr / b.c[i - 1];
      const ddL = (priceDdLong[i] = (px.high[i] - log[i - 1]) / unit);
      const ddS = (priceDdShort[i] = (log[i - 1] - px.low[i]) / unit);
      const turn = direction(delta);
      const trend =
        prevFast > prevSlow && b.c[i] > slow ? 1 : prevFast < prevSlow && b.c[i] < slow ? -1 : 0;
      const activity = prevVol > 0 ? b.v[i] / prevVol : 0;
      const recovery = Math.abs(delta) / unit;
      const draw =
        turn === 1 && ddL >= p.stretch && recovery >= p.recovery
          ? 1
          : turn === -1 && ddS >= p.stretch && recovery >= p.recovery
            ? -1
            : 0;
      states["drawdown-turn"][i] = draw;
      states["drawdown-trend"][i] = agree(draw, trend);
      states["range-reclaim"][i] =
        i > w && b.c[i - 1] < Math.exp(px.low[i - 1]) && b.c[i] > Math.exp(px.low[i - 1])
          ? 1
          : i > w && b.c[i - 1] > Math.exp(px.high[i - 1]) && b.c[i] < Math.exp(px.high[i - 1])
            ? -1
            : 0;
      states["impulse-retest"][i] =
        trend === 1 && b.l[i] <= prevFast && b.c[i] > prevFast && turn === 1
          ? 1
          : trend === -1 && b.h[i] >= prevFast && b.c[i] < prevFast && turn === -1
            ? -1
            : 0;
      states["squeeze-break"][i] =
        compression && activity >= p.activity && recovery > p.stretch
          ? log[i] > px.high[i]
            ? 1
            : log[i] < px.low[i]
              ? -1
              : 0
          : 0;
      states["activity-absorb"][i] =
        activity >= p.activity && range > 0 && range / prevAtr >= p.stretch
          ? b.o[i] - b.l[i] > range * 0.55 && b.c[i] > b.l[i] + range * 0.65
            ? 1
            : b.h[i] - b.o[i] > range * 0.55 && b.c[i] < b.l[i] + range * 0.35
              ? -1
              : 0
          : 0;
      // Independent primitives. Their combinations are a finite dependency graph: parents are computed once,
      // children have their own IDs; no recursive config spawning or shared qualification.
      const breakout = log[i] > px.high[i] ? 1 : log[i] < px.low[i] ? -1 : 0;
      const rocNow = log[i] - log[i - 3],
        rocBefore = log[i - 1] - log[i - 4];
      const turnRoc = rocBefore <= 0 && rocNow > 0 ? 1 : rocBefore >= 0 && rocNow < 0 ? -1 : 0;
      states["break-close"][i] = agree(breakout, trend);
      states["break-retest"][i] =
        i > 0 && log[i - 1] > px.high[i - 1] && b.l[i] <= b.c[i - 1] && turn === 1
          ? 1
          : i > 0 && log[i - 1] < px.low[i - 1] && b.h[i] >= b.c[i - 1] && turn === -1
            ? -1
            : 0;
      states["activity-acceleration"][i] =
        activity >= p.activity && activity >= lastActivity * 1.2 && recovery >= p.recovery
          ? turn
          : 0;
      states["activity-flow"][i] =
        activity >= p.activity && turn === trend && recovery >= p.recovery ? trend : 0;
      states["change-ema"][i] = recovery >= p.recovery ? turnEma : 0;
      states["change-roc"][i] = recovery >= p.recovery ? turnRoc : 0;
      states["break-activity"][i] = agree(states["break-close"][i], states["activity-flow"][i]);
      states["break-direction"][i] = agree(states["break-activity"][i], turnRoc || turnEma);
      // Context children are additional techniques. They read the same closed bar, never trade outcomes.
      states["break-close-tactic"][i] = agree(states["break-close"][i], adaptiveTactic[i]);
      states["break-retest-tactic"][i] = agree(states["break-retest"][i], trendTactic[i]);
      // Additional independent OHLC breakouts: prior w and 2w ranges, participation and failed auctions.
      // The firing bar never moves its own channel; long/short ambiguities remain neutral.
      const channelBreak = b.c[i] > channel.high[i] ? 1 : b.c[i] < channel.low[i] ? -1 : 0;
      states["break-channel"][i] = channelBreak;
      states["break-channel-short"][i] =
        b.c[i] > shortChannel.high[i] ? 1 : b.c[i] < shortChannel.low[i] ? -1 : 0;
      states["break-channel-wide"][i] =
        b.c[i] > wideChannel.high[i] ? 1 : b.c[i] < wideChannel.low[i] ? -1 : 0;
      states["break-channel-activity"][i] =
        activity >= p.activity && tr >= p.stretch * prevAtr ? agree(channelBreak, turn) : 0;
      const rejectedHigh = b.h[i] > channel.high[i] && b.c[i] < channel.high[i],
        rejectedLow = b.l[i] < channel.low[i] && b.c[i] > channel.low[i];
      states["break-channel-failure"][i] =
        b.c[i] > channel.low[i] && b.c[i] < channel.high[i]
          ? rejectedHigh && !rejectedLow && b.c[i] < b.o[i]
            ? -1
            : rejectedLow && !rejectedHigh && b.c[i] > b.o[i]
              ? 1
              : 0
          : 0;
      // Additional cost-aware techniques, never replacements for existing siblings. The observed prior range
      // must span three maximum modeled round trips; this is an opportunity hurdle, not a profit prediction.
      const room = px.high[i] - px.low[i] >= 3 * RT_COST;
      const flow = vol > 0 ? signedFlow / vol : 0;
      states["flow-break"][i] =
        room &&
        activity >= 1 &&
        Math.abs(flow) >= p.recovery &&
        direction(flow) === trend &&
        breakout === trend &&
        recovery >= p.recovery
          ? trend
          : 0;
      states["trend-reclaim"][i] =
        room && activity >= 1 && recovery >= p.recovery
          ? trend === 1 && b.c[i - 1] <= prevFast && b.c[i] > fast
            ? 1
            : trend === -1 && b.c[i - 1] >= prevFast && b.c[i] < fast
              ? -1
              : 0
          : 0;
      states["direction-persist"][i] =
        room &&
        activity >= 1 &&
        i - lastCross <= 3 &&
        lastCrossSide === trend &&
        direction(rocNow) === trend &&
        turn === trend &&
        Math.abs(b.c[i] - fast) <= prevAtr
          ? trend
          : 0;
      lastActivity = activity;
      let marketTurn = 0,
        residualTurn = 0;
      if (market && mrun >= 2 * w && Number.isFinite(mx.high[i])) {
        const end = i - 3;
        const mean = (rp[end] - rp[end - w]) / w;
        const variance = Math.max(0, (rq[end] - rq[end - w]) / w - mean * mean);
        const move = log[i] - log[i - 3] - (mr[i] - mr[i - 3]) - 3 * mean;
        const z = (residualZ[i] = variance > 1e-18 ? move / Math.sqrt(3 * variance) : 0);
        states["activity-acceleration-relative"][i] = agree(
          states["activity-acceleration"][i],
          direction(z),
        );
        residualTurn = z < -p.residual && turn === 1 ? 1 : z > p.residual && turn === -1 ? -1 : 0;
        // Market index is a log-price index; use its own prior RMS return, not the symbol's ATR.
        let sum = 0;
        for (let j = i - 3; j < i; j++) sum += market.ret[j] * market.ret[j];
        const mu = Math.sqrt(sum / 3);
        if (mu > 1e-12) {
          marketDdLong[i] = (mx.high[i] - mr[i - 1]) / mu;
          marketDdShort[i] = (mr[i - 1] - mx.low[i]) / mu;
          marketTurn =
            market.ret[i] > 0 && marketDdLong[i] >= p.stretch
              ? 1
              : market.ret[i] < 0 && marketDdShort[i] >= p.stretch
                ? -1
                : 0;
          const broad = mr[i] - mr[i - 3];
          const momentum = Math.abs(broad) >= p.stretch * mu * Math.sqrt(3) ? direction(broad) : 0;
          states["break-retest-market"][i] = agree(states["break-retest"][i], momentum);
          // Zero is neutral, never an agreement. Comparing two zero results admitted market momentum
          // on a flat own close even when the own trend opposed it or had no direction.
          states["market-momentum"][i] = agree(agree(momentum, trend), turn);
          states["market-continuation"][i] =
            room &&
            activity >= 1 &&
            momentum === trend &&
            trend === turn &&
            Math.abs(prevFast - prevSlow) / prevAtr >= p.recovery &&
            Math.abs(b.c[i] - fast) / prevAtr <= 2 * p.stretch
              ? trend
              : 0;
          states["relative-trend"][i] =
            Math.abs(z) >= p.residual &&
            direction(z) === trend &&
            trend === momentum &&
            activity >= 1
              ? trend
              : 0;
        }
        states["residual-revert"][i] = residualTurn;
        states["market-follow"][i] = agree(marketTurn, turn);
        states["drawdown-residual"][i] = agree(draw, residualTurn);
        states["drawdown-market"][i] = agree(draw, marketTurn);
      }
      states["pullback-activity"][i] = activity >= p.activity ? states["drawdown-trend"][i] : 0;
      // Regime choice is causal and explicit; it is an additional ID, never a replacement for its siblings.
      states.adaptive[i] =
        Math.abs(prevFast - prevSlow) / prevAtr > 0.5 ? states["impulse-retest"][i] : draw;
      for (const method of MICRO_METHODS) {
        bias[method][i] =
          method === "break-close-tactic"
            ? agree(trend, adaptiveTactic[i])
            : method === "break-retest-tactic"
              ? agree(trend, trendTactic[i])
              : method === "activity-acceleration-relative"
                ? direction(residualZ[i])
                : method === "break-retest-market"
                  ? states["market-momentum"][i]
                  : method === "break-channel-failure"
                    ? states[method][i]
                    : method.startsWith("break-channel")
                      ? direction(
                          b.c[i] -
                            (method === "break-channel-wide"
                              ? (wideChannel.high[i] + wideChannel.low[i]) / 2
                              : method === "break-channel-short"
                                ? (shortChannel.high[i] + shortChannel.low[i]) / 2
                                : (channel.high[i] + channel.low[i]) / 2),
                        )
                      : method === "residual-revert" || method === "drawdown-residual"
                        ? residualTurn
                        : method === "market-follow" || method === "drawdown-market"
                          ? marketTurn
                          : method === "drawdown-turn" ||
                              method === "range-reclaim" ||
                              method === "activity-absorb"
                            ? ddL >= p.stretch && turn === 1
                              ? 1
                              : ddS >= p.stretch && turn === -1
                                ? -1
                                : 0
                            : trend;
      }
    }
    return {
      states,
      bias,
      ready,
      priceDdLong,
      priceDdShort,
      marketDdLong,
      marketDdShort,
      residualZ,
    };
  });
}

export function microFamilyState(
  k: SeriesCache,
  method: MicroMethod,
  profile: MicroProfile,
  tfs: readonly number[],
): Int8Array {
  const anchor = Math.min(...tfs);
  if (k.b.tfMin !== anchor) return new Int8Array(k.b.n);
  return k.memo(`micro-family:state:v4:${method}:${profile}:${tfs.join("x")}`, () => {
    const out = Int8Array.from(microFrame(k, profile).states[method]);
    for (const tf of tfs) {
      if (tf === anchor) continue;
      const { k: hk, map } = k.htf(tf / anchor);
      const high = microFrame(hk, profile);
      for (let i = 0; i < out.length; i++) {
        const j = map[i];
        // Never retain a higher view across a missing/incomplete higher bucket.
        if (
          out[i] &&
          (j < 0 ||
            !high.ready[j] ||
            high.bias[method][j] !== out[i] ||
            k.b.t[i] + anchor * 60_000 - (hk.b.t[j] + tf * 60_000) >= tf * 60_000)
        )
          out[i] = 0;
      }
    }
    return out;
  });
}

export function microFamilySpecs(): IndicationSpec[] {
  return MICRO_SUBSETS.flatMap((tfs) =>
    MICRO_METHODS.flatMap((method) =>
      MICRO_PROFILES.map((p) => ({
        id: microFamilyId(tfs, method, p.id),
        kind: "micro" as const,
        label: `Micro · ${method} · ${p.id} · ${tfs.join("+")}m`,
        params: {
          window:
            p.window *
            (method === "break-channel-wide" ? 2 : method === "break-channel-short" ? 0.5 : 1),
          stretch: p.stretch,
          residual: p.residual,
          activity: p.activity,
          recovery: p.recovery,
          anchor: Math.min(...tfs),
          timeframes: tfs.length,
        },
        fn: (k: SeriesCache) => microFamilyState(k, method, p.id, tfs),
      })),
    ),
  );
}

/** Invert entries, never realized returns. Every mirrored technique/subset/profile qualifies independently. */
export function inverseMicroSpecs(): IndicationSpec[] {
  return microFamilySpecs().map((source) => ({
    ...source,
    id: `inverse-${source.id}`,
    kind: "inverse" as const,
    label: `Inverse · ${source.label}`,
    fn: (k: SeriesCache) =>
      k.memo(`inverse-micro:v1:${source.id}`, () => Int8Array.from(source.fn(k), (side) => -side)),
  }));
}
