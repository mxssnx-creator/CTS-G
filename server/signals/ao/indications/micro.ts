// Micro indications ("mc-…"): fast reversal entries for the Micro range. Active net targets use the 0.2% PositionCost baseline
// basis; price targets add the current configured execution cost. A Micro target is cost-sensitive, so only entries
// with a high hit rate on a short pullback can clear it: each fires on the one bar its stretch shows (RSI(2) extreme,
// a close outside a wide Bollinger band that turns back, a rejected spike, a run of same-direction closes, a large
// distance from the rolling VWAP or mean) and points back against the stretch.
//
// Trend-aligned variants ("mc-t…", "mc-turn-…"): the same stretch taken only in the direction of the 4× timeframe's
// trend (its close against its EMA(50), from completed higher bars) — a dip bought in an uptrend, a rip sold in a
// downtrend — and the turn variants only once the next bar closes beyond the stretched bar (the confirmation, not the
// extreme bar). Measured on 50 BingX symbols × 20 days (14 Sep – 4 Oct 2026, every Micro cell, engine simulation,
// 0.2 % cost): the plain events have no gross edge (the move after them is symmetric: 15m RSI(2) MFE 0.33 % vs MAE
// 0.33 % over 15 minutes), Micro's Base cell (0.45 % / 0.9 %) PF 0.27–0.50 on every lane; the trend-aligned ones
// lift it to 0.35–0.58 and the best cell of a lane to 0.87 (30m), still below PF 1 — a 0.3–0.6 % target needs a win
// rate of 80–90 % against a 1–2 % stop after the cost. What edge they have shows at larger distances: 1.5–2 ATR
// (15m / 30m) targets PF 1.15–1.2 in each of the three weeks (every signal, overlapping); in the engine with fixed
// targets the best is z 2.5 with the trend on 30m at 1.6 % / 2× (PF 1.15; weeks 1.68 / 1.10 / 1.04).
//
// Quiet-market variant ("mc-qrsi2-5"): RSI(2) 5 / 95 only while ADX(14) < 12 and the Bollinger width is below its
// 100-bar median — the closest of ~190 tested Micro entries to break-even at Micro's distances: 15m, 0.6 % / 3.5×
// PF 1.11 (204 trades, weeks 0.98 / 1.13 / 1.22, win rate 85 %), but an isolated peak (ADX < 10: 0.76, < 14: 0.96,
// without the width filter 1.00; 2 of its 273 cells ≥ 1.05), so Base decides per window.
//
// Market relations ("mc-lag-…", "mc-rsrev-…", "mc-mturn-…", "mc-act-…"): the events above see one symbol, and its
// stretch is mostly the market's own move (crypto symbols move together: the same RSI(2) dip on every symbol is a
// market dip, with nothing to revert to). These read the universe's equal-weight market of the same timeframe
// (market.ts: mean bar log return over the symbols, its cumulative index and the mean activity v / SMA60(v)) and
// fire on what the symbol does AGAINST it — the part a single-symbol event cannot separate. Beta and volatilities
// over the last 60 bars (microRel); a move over n bars is measured from the window that ends where it starts.
//   mc-lag-m        lead-lag catch-up: the market moved ≥ 2 of its volatilities over m bars (3 / 6 / 12) while the
//                   symbol's residual (its return − beta × the market's) lagged ≥ 1.5 residual volatilities: enter
//                   in the market's direction — a slower symbol follows a broad move with a delay (arbitrage,
//                   index and basket flows reach it last).
//   mc-rsrev-n      relative-strength reversion: the residual over n bars (3 / 6 / 12) beyond ±2.5 of its
//                   volatility, faded — an idiosyncratic overshoot without the market behind it mean-reverts
//                   (liquidity-driven, not news-driven, on most bars).
//   mc-mturn-lo     market turn: the market's RSI(2) was below lo (5 / 10; above 100 − lo) and the market turns
//                   back on this bar, the symbol's bar with it: enter with the turn — the market's reversal is the
//                   broad, persistent one, the symbol confirms it is taking part.
//   mc-act-idio-x   idiosyncratic activity: the symbol's activity is > x (2 / 3) × the market's on a bar beyond 2
//                   of its volatilities: fade the bar — a symbol-only burst (one large order, a liquidation) is
//                   absorbed and gives back part of its move.
//   mc-act-mkt-y    market-wide surge: the market's activity > y (1.8 / 2.5) × normal and its bar moves ≥ 1
//                   volatility while the symbol's bar goes the other way: enter with the market — a broad flow
//                   reaches the stragglers.
// AND combinations of a stretch with a relation filter: "mc-irsi2-10" RSI(2) 10 / 90 only while the market's RSI(2)
// is not beyond 25 / 75 the same way (an idiosyncratic stretch, not a market dip); "mc-mrsi2-10" RSI(2) 10 / 90
// with the market's bar already pointing the fade's way; "mc-iz-25" the 30-bar z 2.5 only when the symbol's 6-bar
// residual is ≥ 1 volatility the same way; "mc-ivwapd-2" the 2 ATR VWAP distance only on the symbol's own flow
// (its activity > 2 × the market's). Not yet measured on the exchange: Base decides per window like the others.
// Without a market reference (a series computed on its own) or alone in its timeframe they are neutral (no event).
//
// Causal: bar i reads bars 0..i only (the market at bar i: the universe's bars with open time ≤ its own). With grid.micro.ownInds (default on) the Micro range trades only these, and
// they trade only Micro cells.
import type { SeriesCache } from "./cache.ts";
import type { Bars } from "../domain/types.ts";
import { MARKET_ACT_P, type MarketRef } from "./market.ts";
import type { IndicationSpec } from "./registry.ts";

const fin = (...xs: number[]) => xs.every((x) => Number.isFinite(x));

/** one-bar event series: f returns +1 / −1 on the firing bar, 0 otherwise (NaN in warm-up = 0) */
function events(n: number, f: (i: number) => number): Int8Array {
  const out = new Int8Array(n);
  for (let i = 0; i < n; i++) {
    const v = f(i);
    out[i] = v > 0 ? 1 : v < 0 ? -1 : 0;
  }
  return out;
}

const spec = (id: string, label: string, params: Record<string, number>, fn: (k: SeriesCache) => Int8Array) =>
  ({ id, kind: "active", label, params, fn }) as IndicationSpec;

export const isMicroInd = (base: string) =>
  base.startsWith("mc-") || base.startsWith("relate-mc-") || base.startsWith("break-mc-rel-") || base.startsWith("inverse-mc-");

/**
 * Which cells the Micro indications take (grid.micro.ownInds, grid.minimal.microInds): false = any indication on any
 * cell; true = Micro indications on Micro cells only and Micro cells on them only; "minimal" = as true, and the Micro
 * indications also take Minimal cells (Minimal then runs every indication: its own and Micro's).
 */
export type MicroIndRule = boolean | "minimal";

/** May an indication (Micro or not) take a cell of this range tag under the rule? */
export function microIndFits(rule: MicroIndRule | undefined, tag: string | undefined, microInd: boolean, firstClassMicro = false): boolean {
  // First-class Micro takes all ranges independently of legacy mc-* exclusivity.
  if (firstClassMicro) return true;
  if (!rule) return true;
  if (microInd) return tag === "mc" || (rule === "minimal" && tag === "mn");
  return tag !== "mc";
}

// ── the stretch events (memoized per series: the trend variants reuse them) ─────────────────────────────────────

/** RSI(2) below lo (+1) / above 100 − lo (−1) */
const rsi2Ev = (k: SeriesCache, lo: number) =>
  k.memo(`mc:rsi2:${lo}`, () => {
    const r = k.rsi(2);
    return events(k.b.n, (i) => (fin(r[i]) ? (r[i] < lo ? 1 : r[i] > 100 - lo ? -1 : 0) : 0));
  });

/** a close outside Bollinger(20, m) and the next bar turning back */
const bbxEv = (k: SeriesCache, m: number) =>
  k.memo(`mc:bbx:${m}`, () => {
    const { up, lo } = k.bb(20, m);
    const c = k.b.c;
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(up[i - 1], lo[i - 1])) return 0;
      if (c[i - 1] < lo[i - 1] && c[i] > c[i - 1]) return 1;
      if (c[i - 1] > up[i - 1] && c[i] < c[i - 1]) return -1;
      return 0;
    });
  });

/** a spike bar (range ≥ x × the prior 20-bar average) rejected: it closes in its opposite third */
const spikeEv = (k: SeriesCache, x: number) =>
  k.memo(`mc:spike:${x}`, () => {
    const rs = k.rangeSma(20);
    const { o, h, l, c } = k.b;
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(rs[i - 1])) return 0;
      const r = h[i] - l[i];
      if (!(r >= x * rs[i - 1]) || !(r > 0)) return 0;
      const pos = (c[i] - l[i]) / r;
      if (c[i] < o[i] && pos > 0.6) return 1;
      if (c[i] > o[i] && pos < 0.4) return -1;
      return 0;
    });
  });

/** n closes in a row the same way: fade the run on its n-th bar */
const streakEv = (k: SeriesCache, n: number) =>
  k.memo(`mc:streak:${n}`, () => {
    const c = k.b.c;
    return events(k.b.n, (i) => {
      if (i < n) return 0;
      let up = 0;
      let dn = 0;
      for (let j = i - n + 1; j <= i; j++) {
        if (c[j] > c[j - 1]) up++;
        else if (c[j] < c[j - 1]) dn++;
      }
      return dn === n ? 1 : up === n ? -1 : 0;
    });
  });

/** distance from the rolling 60-bar VWAP beyond d × ATR(14) */
const vwapdEv = (k: SeriesCache, d: number) =>
  k.memo(`mc:vwapd:${d}`, () => {
    const vw = k.vwap(60);
    const a = k.atr(14);
    const c = k.b.c;
    return events(k.b.n, (i) => {
      if (!fin(vw[i], a[i]) || !(a[i] > 0)) return 0;
      const z = (c[i] - vw[i]) / a[i];
      return z < -d ? 1 : z > d ? -1 : 0;
    });
  });

/** z-score of the close over 30 bars beyond z */
const zEv = (k: SeriesCache, z: number) =>
  k.memo(`mc:z:${z}`, () => {
    const zs = k.z(30);
    return events(k.b.n, (i) => (fin(zs[i]) ? (zs[i] < -z ? 1 : zs[i] > z ? -1 : 0) : 0));
  });

// ── trend and confirmation ───────────────────────────────────────────────────────────────────────────────────────

/** Higher-timeframe factor and EMA period of the Micro trend filter. */
export const MICRO_TREND = { factor: 4, ema: 50 } as const;

/**
 * Trend of the 4× timeframe at each bar: +1 when the last COMPLETED higher bar closed above its EMA(50), −1 below,
 * 0 before the first one (or warm-up). Causal: a higher bar counts only from the lane bar it closes with.
 */
export function microTrend(k: SeriesCache): Int8Array {
  return k.memo(`mc:trend:${MICRO_TREND.factor}:${MICRO_TREND.ema}`, () => {
    const { k: hk, map } = k.htf(MICRO_TREND.factor);
    const e = hk.ema(MICRO_TREND.ema);
    const hc = hk.b.c;
    const out = new Int8Array(k.b.n);
    for (let i = 0; i < k.b.n; i++) {
      const j = map[i];
      if (j < 0 || !fin(e[j])) continue;
      out[i] = hc[j] > e[j] ? 1 : hc[j] < e[j] ? -1 : 0;
    }
    return out;
  });
}

/** the stretch event only where it points with the trend */
function withTrend(k: SeriesCache, ev: Int8Array): Int8Array {
  const tr = microTrend(k);
  return events(k.b.n, (i) => (ev[i] !== 0 && ev[i] === tr[i] ? ev[i] : 0));
}

/**
 * The turn after a stretch, with the trend: the stretch fired on the previous bar, this bar closes beyond the
 * stretched bar's high (long) / low (short), and the trend points the same way.
 */
function turnWithTrend(k: SeriesCache, ev: Int8Array): Int8Array {
  const tr = microTrend(k);
  const { h, l, c } = k.b;
  return events(k.b.n, (i) => {
    if (i < 1) return 0;
    const d = ev[i - 1];
    if (!d || tr[i] !== d) return 0;
    return d > 0 && c[i] > h[i - 1] ? 1 : d < 0 && c[i] < l[i - 1] ? -1 : 0;
  });
}

/** Quiet-market filter of the Micro range reversion: ADX(14) below this and the Bollinger width below its median. */
export const MICRO_QUIET = { adx: 12, widthRank: 0.5, lookback: 100 } as const;

/**
 * Rank of the Bollinger(20, 2) width at each bar among the previous `lookback` widths (0 = the narrowest, NaN in
 * warm-up). Causal: bar i compares with bars i − lookback … i − 1.
 */
export function bbWidthRank(k: SeriesCache, lookback: number = MICRO_QUIET.lookback): Float64Array {
  return k.memo(`mc:wrank:${lookback}`, () => {
    const { width } = k.bb(20, 2);
    const out = new Float64Array(k.b.n).fill(NaN);
    for (let i = lookback; i < k.b.n; i++) {
      if (!Number.isFinite(width[i])) continue;
      let below = 0;
      let cnt = 0;
      for (let j = i - lookback; j < i; j++)
        if (Number.isFinite(width[j])) {
          cnt++;
          if (width[j] < width[i]) below++;
        }
      if (cnt > lookback / 2) out[i] = below / cnt;
    }
    return out;
  });
}

/** the stretch event only in a quiet, trendless market (ADX(14) < 12 and the band narrower than its median) */
function inQuiet(k: SeriesCache, ev: Int8Array): Int8Array {
  const { adx } = k.dmi(14);
  const wr = bbWidthRank(k);
  return events(k.b.n, (i) => (ev[i] !== 0 && fin(adx[i], wr[i]) && adx[i] < MICRO_QUIET.adx && wr[i] < MICRO_QUIET.widthRank ? ev[i] : 0));
}

// ── market relations (the universe's market reference: market.ts) ──────────────────────────────────────────────

/** Rolling window (bars) of the beta and the volatilities of the market relations, and the pairs it needs. */
export const MICRO_REL = { win: 60, min: 40 } as const;

export interface MicroRel {
  /** the market at this series' bars */
  m: MarketRef;
  /** Number of contiguous, finite positive-close bars ending here (a gap starts again at one). */
  contiguousBars: Uint32Array;
  /** beta of the symbol's bar log returns on the market's over the last win bars (bar i included) */
  beta: Float64Array;
  /** volatility (sd) of the market's bar log returns over the same bars */
  sdm: Float64Array;
  /** volatility of the symbol's bar log returns */
  sds: Float64Array;
  /** volatility of the symbol's residual bar returns (symbol − beta × market) */
  sde: Float64Array;
}

/** The symbol's one-bar return; a missing candle cannot become a multi-bar return in a one-bar fit. */
const lr = (b: Bars, i: number) =>
  i >= 1 && b.t[i] - b.t[i - 1] === b.tfMin * 60_000 &&
  Number.isFinite(b.c[i]) && Number.isFinite(b.c[i - 1]) && b.c[i] > 0 && b.c[i - 1] > 0
    ? Math.log(b.c[i] / b.c[i - 1]) : NaN;

/**
 * The series' relation to its market: rolling beta and volatilities over the last MICRO_REL.win bars (pairs where
 * both returns exist; NaN until MICRO_REL.min of them). null without a market reference, or with a market of one
 * series (the series itself): every relation indication is then neutral.
 */
export function microRel(k: SeriesCache): MicroRel | null {
  const m = k.market();
  if (!m || m.syms < 2) return null;
  return k.memo(`mc:rel:${MICRO_REL.win}`, () => {
    const n = k.b.n;
    const b = k.b;
    const W = MICRO_REL.win;
    const contiguousBars = new Uint32Array(n);
    const beta = new Float64Array(n).fill(NaN);
    const sdm = new Float64Array(n).fill(NaN);
    const sds = new Float64Array(n).fill(NaN);
    const sde = new Float64Array(n).fill(NaN);
    let cnt = 0;
    let sx = 0;
    let sy = 0;
    let sxx = 0;
    let syy = 0;
    let sxy = 0;
    const add = (i: number, s: number) => {
      const x = m.ret[i];
      const y = lr(b, i);
      if (!fin(x, y)) return;
      cnt += s;
      sx += s * x;
      sy += s * y;
      sxx += s * x * x;
      syy += s * y * y;
      sxy += s * x * y;
    };
    for (let i = 0; i < n; i++) {
      if (Number.isFinite(b.t[i]) && Number.isFinite(b.c[i]) && b.c[i] > 0)
        contiguousBars[i] = i > 0 && b.t[i] - b.t[i - 1] === b.tfMin * 60_000
          ? contiguousBars[i - 1] + 1 : 1;
      add(i, 1);
      if (i >= W) add(i - W, -1);
      if (cnt < MICRO_REL.min) continue;
      const mx = sx / cnt;
      const my = sy / cnt;
      const vx = sxx / cnt - mx * mx;
      const vy = syy / cnt - my * my;
      const cv = sxy / cnt - mx * my;
      if (!(vx > 0) || !(vy > 0)) continue;
      beta[i] = cv / vx;
      sdm[i] = Math.sqrt(vx);
      sds[i] = Math.sqrt(vy);
      sde[i] = Math.sqrt(Math.max(0, vy - (cv * cv) / vx));
    }
    return { m, contiguousBars, beta, sdm, sds, sde };
  });
}

// (the relation values below are computed per bar from microRel's arrays, not stored: a stored series per window
// length cost 8 bytes × bars × series each, and these are O(1) per bar)

/**
 * The symbol's residual return over the last n bars (log(c_i / c_{i−n}) − beta × the market's), in units of its
 * residual volatility × √n; beta and volatility from the window that ends where the move starts (bar i − n), so the
 * move does not explain itself. + = the symbol outperformed its market. NaN in warm-up.
 */
export function residZ(k: SeriesCache, r: MicroRel, n: number, i: number): number {
  const j = i - n;
  if (j < 0 || r.contiguousBars[i] < n + 1 || !fin(r.beta[j], r.sde[j], r.m.idx[i], r.m.idx[j]) || !(r.sde[j] > 1e-12)) return NaN;
  return (Math.log(k.b.c[i] / k.b.c[j]) - r.beta[j] * (r.m.idx[i] - r.m.idx[j])) / (r.sde[j] * Math.sqrt(n));
}

/** The market's move over the last n bars in units of its bar volatility × √n (volatility from bar i − n). */
export function marketZ(r: MicroRel, n: number, i: number): number {
  const j = i - n;
  if (j < 0 || r.contiguousBars[i] < n + 1 || !fin(r.sdm[j], r.m.idx[i], r.m.idx[j]) || !(r.sdm[j] > 1e-12)) return NaN;
  return (r.m.idx[i] - r.m.idx[j]) / (r.sdm[j] * Math.sqrt(n));
}

/**
 * RSI(2) of the market index (Wilder, on the market's bar log returns; a bar without a market return counts as no
 * move). An own-series timestamp gap restarts the two-return seed. Mirrors exactly: the mirrored market's RSI
 * is 100 − RSI. NaN in warm-up or without a relation.
 */
export function marketRsi2(k: SeriesCache): Float64Array {
  return k.memo("mc:mrsi2", () => {
    const n = k.b.n;
    const out = new Float64Array(n).fill(NaN);
    const r = microRel(k);
    if (!r) return out;
    const p = 2;
    let su = 0;
    let sd = 0;
    let au = 0;
    let ad = 0;
    let warm = 0;
    for (let i = 1; i < n; i++) {
      if (r.contiguousBars[i] < 2) {
        su = sd = au = ad = warm = 0;
        continue;
      }
      const x = Number.isFinite(r.m.ret[i]) ? r.m.ret[i] : 0;
      const u = x > 0 ? x : 0;
      const d = x < 0 ? -x : 0;
      if (warm < p) {
        su += u;
        sd += d;
        warm++;
        if (warm < p) continue;
        au = su / p;
        ad = sd / p;
      } else {
        au = (au * (p - 1) + u) / p;
        ad = (ad * (p - 1) + d) / p;
      }
      out[i] = ad === 0 ? (au === 0 ? 50 : 100) : 100 - 100 / (1 + au / ad);
    }
    return out;
  });
}

/** the symbol's activity over the market's at bar i: (v / SMA60(v)) ÷ the market activity (NaN in warm-up) */
function relActivity(k: SeriesCache, r: MicroRel, i: number): number {
  if (r.contiguousBars[i] < MARKET_ACT_P) return NaN;
  const vs = k.volSma(MARKET_ACT_P)[i];
  const a = r.m.act[i];
  return fin(vs, a) && vs > 0 && a > 0 ? k.b.v[i] / vs / a : NaN;
}

/**
 * Lead-lag catch-up: the market moved ≥ z0 of its volatility over the last m bars while the symbol lagged it (its
 * residual ≤ −r0 against an up-market, ≥ r0 against a down-market): enter in the market's direction.
 */
function lagEv(k: SeriesCache, m: number, z0: number, r0: number): Int8Array {
  const r = microRel(k);
  if (!r) return new Int8Array(k.b.n);
  return events(k.b.n, (i) => {
    const mz = marketZ(r, m, i);
    if (!(Math.abs(mz) > z0)) return 0;
    const rz = residZ(k, r, m, i);
    return mz > 0 && rz < -r0 ? 1 : mz < 0 && rz > r0 ? -1 : 0;
  });
}

/** Relative-strength reversion: the residual return over n bars beyond ±z0 of its volatility — fade it. */
function rsrevEv(k: SeriesCache, n: number, z0: number): Int8Array {
  const r = microRel(k);
  if (!r) return new Int8Array(k.b.n);
  return events(k.b.n, (i) => {
    const rz = residZ(k, r, n, i);
    return rz > z0 ? -1 : rz < -z0 ? 1 : 0;
  });
}

/**
 * Market turn: the market's RSI(2) was below lo (above 100 − lo) on the previous bar and the market turns up (down)
 * on this one, the symbol's bar turning with it: enter with the turn.
 */
function mturnEv(k: SeriesCache, lo: number): Int8Array {
  const r = microRel(k);
  if (!r) return new Int8Array(k.b.n);
  const mr = marketRsi2(k);
  return events(k.b.n, (i) => {
    if (i < 1 || !fin(mr[i - 1], r.m.ret[i])) return 0;
    const s = lr(k.b, i);
    if (mr[i - 1] < lo && r.m.ret[i] > 0 && s > 0) return 1;
    if (mr[i - 1] > 100 - lo && r.m.ret[i] < 0 && s < 0) return -1;
    return 0;
  });
}

/**
 * Idiosyncratic activity spike: the symbol's activity is > x × the market's while its bar is stretched (its return
 * beyond zb of its own bar volatility before it): a symbol-only flow — fade the bar.
 */
function actIdioEv(k: SeriesCache, x: number, zb: number): Int8Array {
  const r = microRel(k);
  if (!r) return new Int8Array(k.b.n);
  return events(k.b.n, (i) => {
    if (i < 1 || !(relActivity(k, r, i) > x) || !(r.sds[i - 1] > 1e-12)) return 0;
    const z = lr(k.b, i) / r.sds[i - 1];
    return z > zb ? -1 : z < -zb ? 1 : 0;
  });
}

/**
 * Market-wide activity surge: the market's activity is > y × normal and its bar moves ≥ 1 volatility while the
 * symbol's bar goes the other way: enter with the market (the symbol's counter-move is the lag of a broad flow).
 */
function actMktEv(k: SeriesCache, y: number): Int8Array {
  const r = microRel(k);
  if (!r) return new Int8Array(k.b.n);
  return events(k.b.n, (i) => {
    if (i < 1 || !fin(r.m.act[i], r.m.ret[i], r.sdm[i - 1]) || !(r.m.act[i] > y) || !(r.sdm[i - 1] > 1e-12)) return 0;
    const z = r.m.ret[i] / r.sdm[i - 1];
    const s = lr(k.b, i);
    return z > 1 && s < 0 ? 1 : z < -1 && s > 0 ? -1 : 0;
  });
}

/** a stretch event kept only where `keep(i, dir)` holds (the relation filter of an AND combination) */
function relFilter(
  k: SeriesCache,
  ev: Int8Array,
  keep: (r: MicroRel, i: number, d: number) => boolean,
): Int8Array {
  const r = microRel(k);
  if (!r) return new Int8Array(k.b.n);
  return events(k.b.n, (i) => (ev[i] !== 0 && r.contiguousBars[i] >= 2 && keep(r, i, ev[i]) ? ev[i] : 0));
}

/** Market-relation thresholds of the AND combinations. */
export const MICRO_REL_AND = { mrsi: 25, rz: 1, rzN: 6, act: 2 } as const;

// ── continuation, break, own activity, RSI and pattern events (operator, 5 Oct: "extend indications — trend, break,
// active, rsi and others"). Every one is causal (bar i reads bars ≤ i) and mirrors by construction; Base decides
// per window which of them carry an edge at Micro's distances. ───────────────────────────────────────────────────

/** pullback to the EMA(p) in the 4× trend, resumed: the previous close at or through the EMA, this close back beyond it */
const tpullEv = (k: SeriesCache, p: number) =>
  k.memo(`mc:tpull:${p}`, () => {
    const tr = microTrend(k);
    const e = k.ema(p);
    const c = k.b.c;
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(e[i - 1], e[i])) return 0;
      if (tr[i] > 0 && c[i - 1] <= e[i - 1] && c[i] > e[i]) return 1;
      if (tr[i] < 0 && c[i - 1] >= e[i - 1] && c[i] < e[i]) return -1;
      return 0;
    });
  });

/** EMA(f) crossing the EMA(s) with the trend */
const tcrossEv = (k: SeriesCache, f: number, s: number) =>
  k.memo(`mc:tcross:${f}:${s}`, () => {
    const tr = microTrend(k);
    const ef = k.ema(f);
    const es = k.ema(s);
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(ef[i - 1], es[i - 1], ef[i], es[i])) return 0;
      if (tr[i] > 0 && ef[i - 1] <= es[i - 1] && ef[i] > es[i]) return 1;
      if (tr[i] < 0 && ef[i - 1] >= es[i - 1] && ef[i] < es[i]) return -1;
      return 0;
    });
  });

/** n closes in a row with the trend right after a counter-trend close: the resumption bar */
const tmomEv = (k: SeriesCache, n: number) =>
  k.memo(`mc:tmom:${n}`, () => {
    const tr = microTrend(k);
    const c = k.b.c;
    return events(k.b.n, (i) => {
      if (i < n + 1 || tr[i] === 0) return 0;
      const d = tr[i];
      for (let j = i - n + 1; j <= i; j++) if (!(d * (c[j] - c[j - 1]) > 0)) return 0;
      const j0 = i - n;
      return d * (c[j0] - c[j0 - 1]) < 0 ? d : 0;
    });
  });

/** close beyond the prior p-bar high / low on a bar at least 1.2 × the average range */
const brkEv = (k: SeriesCache, p: number) =>
  k.memo(`mc:brk:${p}`, () => {
    const { hi, lo } = k.don(p);
    const rs = k.rangeSma(20);
    const { h, l, c } = k.b;
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(hi[i], lo[i], rs[i - 1]) || !(h[i] - l[i] >= 1.2 * rs[i - 1])) return 0;
      return c[i] > hi[i] ? 1 : c[i] < lo[i] ? -1 : 0;
    });
  });

/** inside bar (bar i − 1 within bar i − 2) broken by this close */
const ibrkEv = (k: SeriesCache) =>
  k.memo("mc:ibrk", () => {
    const { h, l, c } = k.b;
    return events(k.b.n, (i) => {
      if (i < 2 || !(h[i - 1] <= h[i - 2] && l[i - 1] >= l[i - 2])) return 0;
      return c[i] > h[i - 2] ? 1 : c[i] < l[i - 2] ? -1 : 0;
    });
  });

/** a close breaking out of Bollinger(20, 2) with the range expanding (the breakout; mc-bbx fades the turn back) */
const bbrkEv = (k: SeriesCache) =>
  k.memo("mc:bbrk", () => {
    const { up, lo } = k.bb(20, 2);
    const rs = k.rangeSma(20);
    const { h, l, c } = k.b;
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(up[i - 1], lo[i - 1], up[i], lo[i], rs[i - 1]) || !(h[i] - l[i] > rs[i - 1])) return 0;
      if (c[i - 1] <= up[i - 1] && c[i] > up[i]) return 1;
      if (c[i - 1] >= lo[i - 1] && c[i] < lo[i]) return -1;
      return 0;
    });
  });

/** the close crossing the rolling 60-bar VWAP on volume ≥ a × its 60-bar average */
const vwapxEv = (k: SeriesCache, a: number) =>
  k.memo(`mc:vwapx:${a}`, () => {
    const vw = k.vwap(60);
    const vs = k.volSma(60);
    const { c, v } = k.b;
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(vw[i - 1], vw[i], vs[i - 1]) || !(v[i] >= a * vs[i - 1])) return 0;
      if (c[i - 1] <= vw[i - 1] && c[i] > vw[i]) return 1;
      if (c[i - 1] >= vw[i - 1] && c[i] < vw[i]) return -1;
      return 0;
    });
  });

/** own activity (volume × range) of the bar against its 60-bar average, up to the previous bar */
const activity = (k: SeriesCache) =>
  k.memo("mc:activity", () => {
    const { h, l, v } = k.b;
    const act = new Float64Array(k.b.n);
    for (let i = 0; i < k.b.n; i++) act[i] = v[i] * (h[i] - l[i]);
    const avg = new Float64Array(k.b.n).fill(NaN);
    let s = 0;
    for (let i = 0; i < k.b.n; i++) {
      s += act[i];
      if (i >= 60) s -= act[i - 60];
      if (i >= 59) avg[i] = s / 60;
    }
    return { act, avg };
  });

/** an activity burst (≥ x × the average) on a directional bar, entered with the bar */
const burstEv = (k: SeriesCache, x: number) =>
  k.memo(`mc:burst:${x}`, () => {
    const { act, avg } = activity(k);
    const { o, c } = k.b;
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(avg[i - 1]) || !(avg[i - 1] > 0) || !(act[i] >= x * avg[i - 1])) return 0;
      return c[i] > o[i] ? 1 : c[i] < o[i] ? -1 : 0;
    });
  });

/** an activity burst out of a quiet stretch (the band narrower than its median on the previous bar) */
const qburstEv = (k: SeriesCache, x: number) =>
  k.memo(`mc:qburst:${x}`, () => {
    const ev = burstEv(k, x);
    const wr = bbWidthRank(k);
    return events(k.b.n, (i) => (i >= 1 && ev[i] !== 0 && fin(wr[i - 1]) && wr[i - 1] < MICRO_QUIET.widthRank ? ev[i] : 0));
  });

/** RSI(p) below lo (+1) / above 100 − lo (−1) */
const rsiEv = (k: SeriesCache, p: number, lo: number) =>
  k.memo(`mc:rsi:${p}:${lo}`, () => {
    const r = k.rsi(p);
    return events(k.b.n, (i) => (fin(r[i]) ? (r[i] < lo ? 1 : r[i] > 100 - lo ? -1 : 0) : 0));
  });

/**
 * RSI(p) turning back out of its extreme: below lo on the previous bar and at or above it now (+1), above 100 − lo
 * and back at or below it (−1) — the confirmation bar, where the level form fires on every stretched bar
 */
const rsiTurnEv = (k: SeriesCache, p: number, lo: number) =>
  k.memo(`mc:rsit:${p}:${lo}`, () => {
    const r = k.rsi(p);
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(r[i - 1], r[i])) return 0;
      if (r[i - 1] < lo && r[i] >= lo) return 1;
      if (r[i - 1] > 100 - lo && r[i] <= 100 - lo) return -1;
      return 0;
    });
  });

/**
 * The Micro RSI grid (operator, 6 Oct: "RSI completely, fine tuned for Micro"): every period × threshold, as the
 * level (RSI beyond lo / 100 − lo) and as the turn back out of it. Base tunes it — each variant is judged at Micro's
 * own cells per pair and lane, only the ones that pass build sets.
 */
export const MICRO_RSI_P = [2, 3, 4, 5, 7, 9, 14] as const;
export const MICRO_RSI_LO = [5, 10, 15, 20, 25, 30] as const;
/**
 * the most extreme threshold each period reaches: a longer RSI travels less far, so RSI(14) below 5 / 10 / 15 / 20
 * or RSI(7) below 5 / 10 practically never fires — dead variants, not tuning
 */
export const MICRO_RSI_MIN_LO: Readonly<Record<number, number>> = { 2: 5, 3: 5, 4: 5, 5: 10, 7: 15, 9: 15, 14: 25 };

/** RSI(p) divergence over `look` bars: a new closing low with the RSI above its low of the window (and the mirror) */
const rsidivEv = (k: SeriesCache, p: number, look: number) =>
  k.memo(`mc:rsidiv:${p}:${look}`, () => {
    const r = k.rsi(p);
    const c = k.b.c;
    return events(k.b.n, (i) => {
      if (i < look || !fin(r[i])) return 0;
      let cmin = Infinity;
      let cmax = -Infinity;
      let rmin = Infinity;
      let rmax = -Infinity;
      for (let j = i - look; j < i; j++) {
        if (!fin(r[j])) return 0;
        if (c[j] < cmin) cmin = c[j];
        if (c[j] > cmax) cmax = c[j];
        if (r[j] < rmin) rmin = r[j];
        if (r[j] > rmax) rmax = r[j];
      }
      if (c[i] < cmin && r[i] > rmin + 2) return 1;
      if (c[i] > cmax && r[i] < rmax - 2) return -1;
      return 0;
    });
  });

/** RSI(p) crossing 50 with the trend */
const rsimidEv = (k: SeriesCache, p: number) =>
  k.memo(`mc:rsimid:${p}`, () => {
    const tr = microTrend(k);
    const r = k.rsi(p);
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(r[i - 1], r[i])) return 0;
      if (tr[i] > 0 && r[i - 1] <= 50 && r[i] > 50) return 1;
      if (tr[i] < 0 && r[i - 1] >= 50 && r[i] < 50) return -1;
      return 0;
    });
  });

/** MACD(12, 26, 9) histogram turning back toward zero from beyond it */
const macdhEv = (k: SeriesCache) =>
  k.memo("mc:macdh", () => {
    const { hist } = k.macd(12, 26, 9);
    return events(k.b.n, (i) => {
      if (i < 2 || !fin(hist[i - 2], hist[i - 1], hist[i])) return 0;
      if (hist[i] < 0 && hist[i - 1] < hist[i - 2] && hist[i] > hist[i - 1]) return 1;
      if (hist[i] > 0 && hist[i - 1] > hist[i - 2] && hist[i] < hist[i - 1]) return -1;
      return 0;
    });
  });

/** lowest low / highest high of the previous p bars (bar i excluded) */
const extremeOfPrior = (k: SeriesCache, p: number) =>
  k.memo(`mc:ext:${p}`, () => k.don(p));

/** an engulfing bar at a p-bar extreme: the body covers the previous bar's body the other way */
const engulfEv = (k: SeriesCache, p: number) =>
  k.memo(`mc:engulf:${p}`, () => {
    const { hi, lo } = extremeOfPrior(k, p);
    const { o, h, l, c } = k.b;
    return events(k.b.n, (i) => {
      if (i < 1 || !fin(hi[i], lo[i])) return 0;
      const bull = c[i - 1] < o[i - 1] && c[i] > o[i] && o[i] <= c[i - 1] && c[i] >= o[i - 1];
      const bear = c[i - 1] > o[i - 1] && c[i] < o[i] && o[i] >= c[i - 1] && c[i] <= o[i - 1];
      if (bull && Math.min(l[i - 1], l[i]) <= lo[i]) return 1;
      if (bear && Math.max(h[i - 1], h[i]) >= hi[i]) return -1;
      return 0;
    });
  });

/** a rejection wick ≥ x × the body at a 20-bar extreme, the close in the far half of the bar */
const wickEv = (k: SeriesCache, x: number) =>
  k.memo(`mc:wick:${x}`, () => {
    const { hi, lo } = extremeOfPrior(k, 20);
    const { o, h, l, c } = k.b;
    return events(k.b.n, (i) => {
      if (!fin(hi[i], lo[i])) return 0;
      const r = h[i] - l[i];
      if (!(r > 0)) return 0;
      const body = Math.abs(c[i] - o[i]);
      const lower = Math.min(o[i], c[i]) - l[i];
      const upper = h[i] - Math.max(o[i], c[i]);
      if (l[i] <= lo[i] && lower >= x * Math.max(body, r * 0.05) && (c[i] - l[i]) / r >= 0.5) return 1;
      if (h[i] >= hi[i] && upper >= x * Math.max(body, r * 0.05) && (h[i] - c[i]) / r >= 0.5) return -1;
      return 0;
    });
  });

/** ids of the Micro indications that read the market reference (neutral on a series without one) */
export const isMicroRelation = (base: string) =>
  base.startsWith("relate-mc-") || base.startsWith("break-mc-rel-") || /^mc-(lag|rsrev|mturn|act|irsi2|mrsi2|iz|ivwapd|rexh)-/.test(base);

// ── exhaustion (13 Oct): fade the move an entry would have chased ───────────────────────────────────────────────
// The PF gap analysis of the executed orders (PF-SEARCH.md section 20): the engine's entries came after a strong move
// in their own direction (+2.8 % in the hour before, random times +0.04 %) which then reversed (−0.5 % in the next 4 h),
// and the opposite side at the same entries won (PF 1.1–1.5 in every half). These enter that opposite side on purpose,
// at the bar where the move stalls — four situations of the same idea: a stalled run (situation), a rejected climax
// bar (activity), a failed break of the range (range) and an idiosyncratic overshoot on heavy flow (relation).

/** own activity: the bar's volume over the mean of the previous 60 bars (NaN in warm-up) */
const actOf = (k: SeriesCache) =>
  k.memo("mc:act60", () => {
    const v = k.b.v;
    const n = k.b.n;
    const out = new Float64Array(n).fill(NaN);
    let s = 0;
    for (let i = 0; i < n; i++) {
      if (i >= 60) {
        const m = s / 60;
        out[i] = m > 0 ? v[i] / m : NaN;
        s -= v[i - 60];
      }
      s += v[i];
    }
    return out;
  });

/** a run of ≥ x ATR(14) over h bars that stalls: the bar closes against it, below (above) the prior bar's midpoint */
const exhEv = (k: SeriesCache, h: number, x: number) =>
  k.memo(`mc:exh:${h}:${x}`, () => {
    const a = k.atr(14);
    const { h: hi, l: lo, c } = k.b;
    return events(k.b.n, (i) => {
      if (i < h + 2 || !fin(a[i - 1]) || !(a[i - 1] > 0)) return 0;
      const mv = c[i - 1] - c[i - 1 - h];
      const mid = (hi[i - 1] + lo[i - 1]) / 2;
      if (mv >= x * a[i - 1] && c[i] < c[i - 1] && c[i] < mid) return -1;
      if (mv <= -x * a[i - 1] && c[i] > c[i - 1] && c[i] > mid) return 1;
      return 0;
    });
  });

/** a climax bar: activity ≥ x × normal and range ≥ 1.5 ATR after 3 closes one way, closing in its opposite 40 % */
const climaxEv = (k: SeriesCache, x: number) =>
  k.memo(`mc:climax:${x}`, () => {
    const a = k.atr(14);
    const act = actOf(k);
    const { o, h, l, c } = k.b;
    return events(k.b.n, (i) => {
      if (i < 4 || !fin(a[i - 1], act[i]) || !(act[i] >= x)) return 0;
      const r = h[i] - l[i];
      if (!(r >= 1.5 * a[i - 1]) || !(r > 0)) return 0;
      const up = c[i - 1] > c[i - 2] && c[i - 2] > c[i - 3] && c[i - 3] > c[i - 4];
      const dn = c[i - 1] < c[i - 2] && c[i - 2] < c[i - 3] && c[i - 3] < c[i - 4];
      const pos = (c[i] - l[i]) / r;
      if (up && pos < 0.4 && c[i] < o[i]) return -1;
      if (dn && pos > 0.6 && c[i] > o[i]) return 1;
      return 0;
    });
  });

/** a failed break of the n-bar range: the bar trades beyond the prior n bars' high (low) and closes back inside, against its open */
const fboEv = (k: SeriesCache, n: number) =>
  k.memo(`mc:fbo:${n}`, () => {
    const { o, h, l, c } = k.b;
    const N = k.b.n;
    const out = new Int8Array(N);
    // monotone deques (index arrays with a head and a tail) of the prior n bars' highs and lows: O(1) per bar
    const qh = new Int32Array(N);
    const ql = new Int32Array(N);
    let hh0 = 0;
    let hh1 = 0;
    let lh0 = 0;
    let lh1 = 0;
    for (let i = 0; i < N; i++) {
      while (hh1 > hh0 && qh[hh0] < i - n) hh0++;
      while (lh1 > lh0 && ql[lh0] < i - n) lh0++;
      if (i >= n && hh1 > hh0 && lh1 > lh0) {
        const hh = h[qh[hh0]];
        const ll = l[ql[lh0]];
        if (h[i] > hh && c[i] < hh && c[i] < o[i]) out[i] = -1;
        else if (l[i] < ll && c[i] > ll && c[i] > o[i]) out[i] = 1;
      }
      while (hh1 > hh0 && h[qh[hh1 - 1]] <= h[i]) hh1--;
      qh[hh1++] = i;
      while (lh1 > lh0 && l[ql[lh1 - 1]] >= l[i]) lh1--;
      ql[lh1++] = i;
    }
    return out;
  });

/** an idiosyncratic overshoot on heavy flow: the n-bar residual beyond 2.5 volatilities, own activity ≥ 2, the market not along */
const rexhEv = (k: SeriesCache, n: number) =>
  k.memo(`mc:rexh:${n}`, () => {
    const r = microRel(k);
    const N = k.b.n;
    if (!r) return new Int8Array(N);
    const act = actOf(k);
    return events(N, (i) => {
      // actOf compares with the previous 60 volumes, so the current bar needs 60 contiguous predecessors.
      if (r.contiguousBars[i] <= MARKET_ACT_P || !fin(act[i]) || !(act[i] >= 2)) return 0;
      const rz = residZ(k, r, n, i);
      const mz = marketZ(r, n, i);
      if (!fin(rz, mz)) return 0;
      if (rz >= 2.5 && mz < 1) return -1;
      if (rz <= -2.5 && mz > -1) return 1;
      return 0;
    });
  });

/** The exhaustion family (Micro indications: Micro's cells, and Minimal's with grid.minimal.microInds). */
export function microExhaustionSpecs(): IndicationSpec[] {
  return [
    ...([4, 12] as const).flatMap((h) =>
      ([3, 5] as const).map((x) => spec(`mc-exh-${h}-${x}`, `Micro ${h}-bar run of ${x} ATR stalls: fade`, { h, x }, (k) => exhEv(k, h, x))),
    ),
    ...([3, 4] as const).map((x) => spec(`mc-climax-${x}`, `Micro ${x}× activity climax rejected: fade`, { x, p: 60 }, (k) => climaxEv(k, x))),
    ...([48, 96] as const).map((n) => spec(`mc-fbo-${n}`, `Micro failed ${n}-bar break: fade`, { n }, (k) => fboEv(k, n))),
    ...([6, 12] as const).map((n) => spec(`mc-rexh-${n}`, `Micro ${n}-bar idiosyncratic overshoot on flow: fade`, { n, z: 2.5, act: 2 }, (k) => rexhEv(k, n))),
  ];
}

export function microSpecs(): IndicationSpec[] {
  const base = microBaseSpecs();
  const have = new Set(base.map((x) => x.id));
  // the RSI grid: the level forms not already in the hand-picked set, and every turn
  const grid: IndicationSpec[] = [];
  for (const p of MICRO_RSI_P)
    for (const lo of MICRO_RSI_LO) {
      if (lo < (MICRO_RSI_MIN_LO[p] ?? 5)) continue;
      const id = `mc-rsi${p}-${lo}`;
      if (!have.has(id)) grid.push(spec(id, `Micro RSI${p} ${lo}/${100 - lo}`, { p, lo }, (k) => (p === 2 ? rsi2Ev(k, lo) : rsiEv(k, p, lo))));
      grid.push(spec(`mc-rsit${p}-${lo}`, `Micro RSI${p} ${lo}/${100 - lo} turn`, { p, lo }, (k) => rsiTurnEv(k, p, lo)));
    }
  return [...base, ...grid, ...microExhaustionSpecs()];
}

function microBaseSpecs(): IndicationSpec[] {
  return [
    // ── continuation with the 4× trend ──
    ...([8, 13] as const).map((p) => spec(`mc-tpull-${p}`, `Micro EMA ${p} pullback with the trend`, { p, htf: 4, ema: 50 }, (k) => tpullEv(k, p))),
    spec("mc-tcross-513", "Micro EMA 5/13 cross with the trend", { f: 5, s: 13, htf: 4, ema: 50 }, (k) => tcrossEv(k, 5, 13)),
    ...([3, 5] as const).map((n) => spec(`mc-tmom-${n}`, `Micro ${n}-bar resumption with the trend`, { n, htf: 4, ema: 50 }, (k) => tmomEv(k, n))),
    // ── breaks ──
    ...([10, 20] as const).map((p) => spec(`mc-brk-${p}`, `Micro ${p}-bar break`, { p, x: 1.2 }, (k) => brkEv(k, p))),
    spec("mc-ibrk", "Micro inside-bar break", { n: 3 }, (k) => ibrkEv(k)),
    spec("mc-bbrk-20", "Micro BB 2 breakout", { p: 20, m: 2 }, (k) => bbrkEv(k)),
    spec("mc-vwapx-15", "Micro VWAP cross on 1.5× volume", { p: 60, a: 1.5 }, (k) => vwapxEv(k, 1.5)),
    // ── own activity ──
    ...([2, 3] as const).map((x) => spec(`mc-burst-${x}`, `Micro activity ${x}× burst`, { p: 60, x }, (k) => burstEv(k, x))),
    spec("mc-qburst-2", "Micro activity 2× burst from quiet", { p: 60, x: 2, wq: 0.5 }, (k) => qburstEv(k, 2)),
    // ── RSI ──
    ...([3, 5] as const).flatMap((p) =>
      ([10, 20] as const).map((lo) => spec(`mc-rsi${p}-${lo}`, `Micro RSI${p} ${lo}/${100 - lo}`, { p, lo }, (k) => rsiEv(k, p, lo))),
    ),
    spec("mc-rsidiv-14", "Micro RSI14 divergence", { p: 14, look: 20 }, (k) => rsidivEv(k, 14, 20)),
    spec("mc-rsimid-14", "Micro RSI14 50 cross with the trend", { p: 14, htf: 4, ema: 50 }, (k) => rsimidEv(k, 14)),
    // ── patterns ──
    spec("mc-macdh", "Micro MACD histogram turn", { f: 12, s: 26, g: 9 }, (k) => macdhEv(k)),
    spec("mc-engulf-20", "Micro engulfing at a 20-bar extreme", { p: 20 }, (k) => engulfEv(k, 20)),
    ...([2, 3] as const).map((x) => spec(`mc-wick-${x}`, `Micro ${x}× rejection wick`, { p: 20, x }, (k) => wickEv(k, x))),
    // RSI(2) at an extreme: the last two bars stretched one way
    ...([5, 10] as const).map((lo) =>
      spec(`mc-rsi2-${lo}`, `Micro RSI2 ${lo}/${100 - lo}`, { p: 2, lo }, (k) => rsi2Ev(k, lo)),
    ),
    // a close outside Bollinger(20, k) and the next bar turning back
    ...([2, 2.5] as const).map((m) => spec(`mc-bbx-${m * 10}`, `Micro BB ${m} turn`, { p: 20, m }, (k) => bbxEv(k, m))),
    // a spike bar (range ≥ x × the prior 20-bar average) rejected: it closes in its opposite third
    ...([2, 2.5] as const).map((x) => spec(`mc-spike-${x}`, `Micro spike ${x}× fade`, { p: 20, x }, (k) => spikeEv(k, x))),
    // n closes in a row the same way: fade the run on its n-th bar
    ...([4, 6] as const).map((n) => spec(`mc-streak-${n}`, `Micro streak ${n} fade`, { n }, (k) => streakEv(k, n))),
    // distance from the rolling 60-bar VWAP in ATR(14) units
    ...([2, 3] as const).map((d) => spec(`mc-vwapd-${d}`, `Micro VWAP ${d} ATR`, { p: 60, d }, (k) => vwapdEv(k, d))),
    // z-score of the close over 30 bars
    ...([2, 2.5] as const).map((z) => spec(`mc-z-${z * 10}`, `Micro z ${z}`, { p: 30, z }, (k) => zEv(k, z))),
    // ── with the 4× timeframe's trend (a dip in an uptrend, a rip in a downtrend) ──
    spec("mc-trsi2-10", "Micro RSI2 10/90 with the trend", { p: 2, lo: 10, htf: 4, ema: 50 }, (k) =>
      withTrend(k, rsi2Ev(k, 10)),
    ),
    spec("mc-tbbx-25", "Micro BB 2.5 turn with the trend", { p: 20, m: 2.5, htf: 4, ema: 50 }, (k) =>
      withTrend(k, bbxEv(k, 2.5)),
    ),
    spec("mc-tvwapd-3", "Micro VWAP 3 ATR with the trend", { p: 60, d: 3, htf: 4, ema: 50 }, (k) =>
      withTrend(k, vwapdEv(k, 3)),
    ),
    spec("mc-tz-25", "Micro z 2.5 with the trend", { p: 30, z: 2.5, htf: 4, ema: 50 }, (k) => withTrend(k, zEv(k, 2.5))),
    // ── the turn bar after the stretch, with the trend (the confirmation, not the extreme bar) ──
    spec("mc-turn-10", "Micro RSI2 10/90 turn with the trend", { p: 2, lo: 10, htf: 4, ema: 50 }, (k) =>
      turnWithTrend(k, rsi2Ev(k, 10)),
    ),
    spec("mc-tstreak-4", "Micro streak 4 turn with the trend", { n: 4, htf: 4, ema: 50 }, (k) =>
      turnWithTrend(k, streakEv(k, 4)),
    ),
    // ── reversion only in a quiet, trendless market (the closest to break-even at Micro's distances) ──
    spec("mc-qrsi2-5", "Micro RSI2 5/95 in a quiet market", { p: 2, lo: 5, adx: 12, wq: 0.5 }, (k) =>
      inQuiet(k, rsi2Ev(k, 5)),
    ),
    // ── market relations: the symbol against its timeframe's equal-weight market (market.ts) ──
    // lead-lag: the market moved ≥ 2 volatilities over m bars, the symbol lagged by ≥ 1.5 residual volatilities
    ...([3, 6, 12] as const).map((m) =>
      spec(`mc-lag-${m}`, `Micro lag ${m} catch-up`, { m, z: 2, r: 1.5, win: MICRO_REL.win }, (k) => lagEv(k, m, 2, 1.5)),
    ),
    // relative-strength reversion: the residual return over n bars beyond 2.5 residual volatilities, faded
    ...([3, 6, 12] as const).map((n) =>
      spec(`mc-rsrev-${n}`, `Micro residual ${n} fade`, { n, z: 2.5, win: MICRO_REL.win }, (k) => rsrevEv(k, n, 2.5)),
    ),
    // market turn: the market's RSI(2) extreme turning back, the symbol turning with it
    ...([5, 10] as const).map((lo) =>
      spec(`mc-mturn-${lo}`, `Micro market RSI2 ${lo}/${100 - lo} turn`, { p: 2, lo }, (k) => mturnEv(k, lo)),
    ),
    // idiosyncratic activity spike (symbol activity ÷ market activity > x) on a bar beyond 2 volatilities: fade it
    ...([2, 3] as const).map((x) =>
      spec(`mc-act-idio-${x}`, `Micro own activity ${x}× market fade`, { p: MARKET_ACT_P, x, zb: 2 }, (k) =>
        actIdioEv(k, x, 2),
      ),
    ),
    // market-wide activity surge (> y × normal) with the symbol's bar against the market's: enter with the market
    ...([1.8, 2.5] as const).map((y) =>
      spec(`mc-act-mkt-${y * 10}`, `Micro market activity ${y}× catch-up`, { p: MARKET_ACT_P, y }, (k) => actMktEv(k, y)),
    ),
    // ── a stretch AND a market relation ──
    // an RSI(2) stretch the market does not share (its RSI(2) not beyond 25 / 75 the same way): idiosyncratic
    spec("mc-irsi2-10", "Micro RSI2 10/90, market not stretched", { p: 2, lo: 10, mlo: MICRO_REL_AND.mrsi }, (k) => {
      const mr = marketRsi2(k);
      const lo = MICRO_REL_AND.mrsi;
      return relFilter(k, rsi2Ev(k, 10), (_, i, d) => fin(mr[i]) && (d > 0 ? mr[i] > lo : mr[i] < 100 - lo));
    }),
    // an RSI(2) stretch while the market's bar already points the fade's way
    spec("mc-mrsi2-10", "Micro RSI2 10/90, market bar turning", { p: 2, lo: 10 }, (k) => {
      return relFilter(k, rsi2Ev(k, 10), (r, i, d) => d * r.m.ret[i] > 0);
    }),
    // a 30-bar z 2.5 stretch that is the symbol's own: its 6-bar residual ≥ 1 residual volatility the same way
    spec("mc-iz-25", "Micro z 2.5, residual stretch", { p: 30, z: 2.5, n: MICRO_REL_AND.rzN, rz: MICRO_REL_AND.rz }, (k) => {
      const t = MICRO_REL_AND.rz;
      return relFilter(k, zEv(k, 2.5), (r, i, d) => {
        const rz = residZ(k, r, MICRO_REL_AND.rzN, i);
        return d > 0 ? rz < -t : rz > t;
      });
    }),
    // a 2 ATR distance from the rolling VWAP made on the symbol's own flow: its activity > 2 × the market's
    spec("mc-ivwapd-2", "Micro VWAP 2 ATR, own activity", { p: 60, d: 2, act: MICRO_REL_AND.act }, (k) => {
      return relFilter(k, vwapdEv(k, 2), (r, i) => relActivity(k, r, i) > MICRO_REL_AND.act);
    }),
  ];
}
