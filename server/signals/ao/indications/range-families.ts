// Range families: entries built for the Short, General and Long ranges (1.8–6.4 % targets: hours of movement, the
// 15m / 30m lanes those ranges trade). The operator's 13 Oct request — indications "with special multi calcs and
// situations, relations". Three groups:
//
// Multi calcs (confluence): a signal only where independent calculations agree on the bar.
//   cfl-trend-p   EMA(p) up over 3 bars, the MACD(12, 26, 9) histogram above 0 and ADX(14) ≥ 20 with +DI over −DI
//                 (mirrored for short): a trend three measures confirm. A state while all three agree (follow enters
//                 on its onset).
//   cfl-break-p   a close beyond the prior p-bar Donchian band, on volume ≥ 1.5 × the prior p-bar average, closing in
//                 the outer 30 % of the bar's range: a breakout with participation, not a wick. One-bar event.
//   htfpb-f       a pullback in the f× higher timeframe's trend (its close over its EMA(50) and EMA(20) over EMA(50),
//                 from completed higher bars): the bar dips to this timeframe's EMA(20), closes back beyond it and in
//                 the trend's direction. One-bar event.
// Situations (regime, compression, session): the tactic follows the market's state.
//   rgm-p         ATR(14) ÷ close in the top quarter of its last 200 bars → a Donchian(p) break (with the move); in the
//                 bottom quarter → a z(p) ±2 stretch on the previous bar that turns back on this one (against it).
//   cmpx-L        Bollinger(20, 2) width in the lowest fifth of its last L bars on the previous bar, and this bar closes
//                 outside the band: compression, then the expansion's direction.
//   orb-s         the session's opening range (UTC: Asia 00:00, Europe 07:00, US 13:30; its first 60 min): the first
//                 close beyond its high / low within the next 6 h, once per session and side.
// Relations (cross-symbol: the universe's equal-weight market of the same timeframe, market.ts; beta and volatilities
// from the 60 bars before the move, microRel):
//   rel-str-n     the market's n-bar move ≥ 1 volatility (× √n) and the symbol's residual over the same bars ≥ 1
//                 residual volatility the same way: a leader in the market's direction. A state while true.
//   rel-div-n     the market's n-bar move ≥ 1.5 volatilities while the symbol's own n-bar return points the other way:
//                 enter with the market (a straggler catches up). One-bar event on the divergence's first bar.
//                 Without a market reference (a series computed alone) the relations are neutral.
//
// Causal: bar i reads bars 0..i only (higher timeframes from completed bars). Candidates, not proven edges: Base and
// the seat gates decide per window (docs/evaluation-2026-10/PROTOCOL-8.md).
import type { SeriesCache } from "./cache.ts";
import type { IndicationSpec } from "./registry.ts";
import type { IndicationKind } from "../domain/types.ts";
import { marketZ, microRel, residZ } from "./micro.ts";

const fin = (...xs: number[]) => xs.every((x) => Number.isFinite(x));

/** f returns +1 / −1 / 0 per bar (NaN = 0) */
function series(n: number, f: (i: number) => number): Int8Array {
  const out = new Int8Array(n);
  for (let i = 0; i < n; i++) {
    const v = f(i);
    out[i] = v > 0 ? 1 : v < 0 ? -1 : 0;
  }
  return out;
}

const spec = (
  kind: IndicationKind,
  id: string,
  label: string,
  params: Record<string, number>,
  fn: (k: SeriesCache) => Int8Array,
): IndicationSpec => ({ id, kind, label, params, fn });

/** The range-family ids (every id starts with one of these). */
export const RANGE_FAMILY_PREFIXES = ["cfl-trend-", "cfl-break-", "htfpb-", "rgm-", "cmpx-", "orb-", "rel-str-", "rel-div-"] as const;
export const isRangeFamily = (base: string) => RANGE_FAMILY_PREFIXES.some((p) => base.startsWith(p));

/** EMA(p), MACD histogram and ADX / DI agree. */
export function cflTrend(k: SeriesCache, p: number): Int8Array {
  const e = k.ema(p);
  const hist = k.macd(12, 26, 9).hist;
  const d = k.dmi(14);
  return series(k.b.n, (i) => {
    if (i < 3 || !fin(e[i], e[i - 3], hist[i], d.adx[i], d.pdi[i], d.mdi[i]) || d.adx[i] < 20) return 0;
    if (e[i] > e[i - 3] && hist[i] > 0 && d.pdi[i] > d.mdi[i]) return 1;
    if (e[i] < e[i - 3] && hist[i] < 0 && d.mdi[i] > d.pdi[i]) return -1;
    return 0;
  });
}

/** A close beyond the prior Donchian band on 1.5× volume, in the bar's outer 30 %. */
export function cflBreak(k: SeriesCache, p: number): Int8Array {
  const { h, l, c, v } = k.b;
  const dn = k.don(p);
  const vs = k.volSma(p);
  return series(k.b.n, (i) => {
    if (i < 1 || !fin(dn.hi[i], dn.lo[i], vs[i - 1]) || !(vs[i - 1] > 0) || v[i] < 1.5 * vs[i - 1]) return 0;
    const rng = h[i] - l[i];
    if (!(rng > 0)) return 0;
    const loc = (c[i] - l[i]) / rng;
    if (c[i] > dn.hi[i] && loc >= 0.7) return 1;
    if (c[i] < dn.lo[i] && loc <= 0.3) return -1;
    return 0;
  });
}

/** A pullback to EMA(20) in the f× timeframe's trend. */
export function htfPullback(k: SeriesCache, f: number): Int8Array {
  const { o, h, l, c } = k.b;
  const { k: hk, map } = k.htf(f);
  const hc = hk.b.c;
  const h20 = hk.ema(20);
  const h50 = hk.ema(50);
  const e20 = k.ema(20);
  return series(k.b.n, (i) => {
    const j = map[i];
    if (j < 0 || !fin(hc[j], h20[j], h50[j], e20[i])) return 0;
    const up = hc[j] > h50[j] && h20[j] > h50[j];
    const dn = hc[j] < h50[j] && h20[j] < h50[j];
    if (up && l[i] <= e20[i] && c[i] > e20[i] && c[i] > o[i]) return 1;
    if (dn && h[i] >= e20[i] && c[i] < e20[i] && c[i] < o[i]) return -1;
    return 0;
  });
}

/**
 * Percentile rank (0–1) of x[i] among x[i − w + 1 .. i], ties counted half (a flat stretch ranks 0.5, neither
 * extreme); NaN until w finite values.
 */
export function rollingRank(x: Float64Array, w: number): Float64Array {
  const out = new Float64Array(x.length).fill(NaN);
  for (let i = w - 1; i < x.length; i++) {
    if (!Number.isFinite(x[i])) continue;
    let below = 0;
    let cnt = 0;
    for (let j = i - w + 1; j <= i; j++) {
      if (!Number.isFinite(x[j])) continue;
      cnt++;
      if (x[j] < x[i]) below++;
      else if (x[j] === x[i]) below += 0.5;
    }
    if (cnt >= w) out[i] = below / cnt;
  }
  return out;
}

/** ATR(14) ÷ close, ranked over its last 200 bars. */
function atrRank(k: SeriesCache): Float64Array {
  return k.memo("rf:atrrank200", () => {
    const a = k.atr(14);
    const c = k.b.c;
    const rel = new Float64Array(k.b.n);
    for (let i = 0; i < k.b.n; i++) rel[i] = c[i] > 0 ? a[i] / c[i] : NaN;
    return rollingRank(rel, 200);
  });
}

/** High volatility: a Donchian(p) break; low: a z(p) ±2 stretch that turns back, faded. */
export function regimeTactic(k: SeriesCache, p: number): Int8Array {
  const c = k.b.c;
  const rank = atrRank(k);
  const dn = k.don(p);
  const z = k.z(p);
  return series(k.b.n, (i) => {
    if (i < 1 || !fin(rank[i])) return 0;
    if (rank[i] >= 0.75) {
      if (!fin(dn.hi[i], dn.lo[i])) return 0;
      return c[i] > dn.hi[i] ? 1 : c[i] < dn.lo[i] ? -1 : 0;
    }
    if (rank[i] <= 0.25) {
      if (!fin(z[i - 1])) return 0;
      if (z[i - 1] <= -2 && c[i] > c[i - 1]) return 1;
      if (z[i - 1] >= 2 && c[i] < c[i - 1]) return -1;
    }
    return 0;
  });
}

/** Bollinger(20, 2) width in the lowest fifth of its last L bars (previous bar), then a close outside the band. */
export function compressionBreak(k: SeriesCache, L: number): Int8Array {
  const c = k.b.c;
  const bb = k.bb(20, 2);
  const rank = k.memo(`rf:bbw20rank${L}`, () => rollingRank(bb.width, L));
  return series(k.b.n, (i) => {
    if (i < 1 || !fin(rank[i - 1], bb.up[i], bb.lo[i]) || rank[i - 1] > 0.2) return 0;
    return c[i] > bb.up[i] ? 1 : c[i] < bb.lo[i] ? -1 : 0;
  });
}

/** Session opening ranges, UTC minutes of the day. */
export const ORB_SESSIONS = { asia: 0, eu: 7 * 60, us: 13 * 60 + 30 } as const;
const MIN = 60_000;
const DAY = 24 * 60 * MIN;
const OR_MIN = 60;
const ORB_WINDOW_MIN = 6 * 60;

/** The first close beyond the session's opening range (its first 60 min) within the next 6 h, once per side. */
export function openingRangeBreak(k: SeriesCache, startMin: number): Int8Array {
  const { t, h, l, c, n } = k.b;
  const tfMs = k.b.tfMin * MIN;
  const out = new Int8Array(n);
  // a lane slower than the opening range cannot form one
  if (tfMs > OR_MIN * MIN) return out;
  let day = NaN;
  let hi = -Infinity;
  let lo = Infinity;
  let bars = 0;
  let firedUp = false;
  let firedDn = false;
  for (let i = 0; i < n; i++) {
    const d = Math.floor((t[i] - startMin * MIN) / DAY);
    if (d !== day) {
      day = d;
      hi = -Infinity;
      lo = Infinity;
      bars = 0;
      firedUp = false;
      firedDn = false;
    }
    const s0 = d * DAY + startMin * MIN;
    const orEnd = s0 + OR_MIN * MIN;
    if (t[i] >= s0 && t[i] + tfMs <= orEnd) {
      if (h[i] > hi) hi = h[i];
      if (l[i] < lo) lo = l[i];
      bars++;
      continue;
    }
    // the whole range is needed (a gap in the bars leaves it partial: no session)
    if (bars < (OR_MIN * MIN) / tfMs || t[i] < orEnd || t[i] >= orEnd + ORB_WINDOW_MIN * MIN) continue;
    if (!firedUp && c[i] > hi) {
      out[i] = 1;
      firedUp = true;
    } else if (!firedDn && c[i] < lo) {
      out[i] = -1;
      firedDn = true;
    }
  }
  return out;
}

/** A leader in the market's direction: the market's n-bar move ≥ 1 σ and the symbol's residual ≥ 1 σ the same way. */
export function relStrength(k: SeriesCache, n: number): Int8Array {
  const r = microRel(k);
  if (!r) return new Int8Array(k.b.n);
  return series(k.b.n, (i) => {
    const mz = marketZ(r, n, i);
    const rz = residZ(k, r, n, i);
    if (!fin(mz, rz)) return 0;
    return mz >= 1 && rz >= 1 ? 1 : mz <= -1 && rz <= -1 ? -1 : 0;
  });
}

/** A straggler: the market's n-bar move ≥ 1.5 σ, the symbol's own n-bar return the other way — enter with the market. */
export function relDivergence(k: SeriesCache, n: number): Int8Array {
  const r = microRel(k);
  const N = k.b.n;
  if (!r) return new Int8Array(N);
  const c = k.b.c;
  const cond = series(N, (i) => {
    if (i < n) return 0;
    const mz = marketZ(r, n, i);
    if (!fin(mz)) return 0;
    const own = Math.log(c[i] / c[i - n]);
    return mz >= 1.5 && own < 0 ? 1 : mz <= -1.5 && own > 0 ? -1 : 0;
  });
  // the divergence's first bar only
  return series(N, (i) => (cond[i] !== 0 && (i === 0 || cond[i - 1] !== cond[i]) ? cond[i] : 0));
}

export function rangeFamilySpecs(): IndicationSpec[] {
  const out: IndicationSpec[] = [];
  for (const p of [20, 50])
    out.push(spec("trend", `cfl-trend-${p}`, `Confluence trend EMA ${p} · MACD · ADX`, { p }, (k) => cflTrend(k, p)));
  for (const p of [20, 55])
    out.push(spec("break", `cfl-break-${p}`, `Confluence break Donchian ${p} · volume · close`, { p }, (k) => cflBreak(k, p)));
  for (const f of [4, 8])
    out.push(spec("trend", `htfpb-${f}`, `Pullback in the ${f}× timeframe's trend`, { f, p: 20 }, (k) => htfPullback(k, f)));
  for (const p of [20, 50])
    out.push(spec("active", `rgm-${p}`, `Volatility regime: break ${p} / fade z ${p}`, { p }, (k) => regimeTactic(k, p)));
  for (const look of [100, 250])
    out.push(spec("bollinger", `cmpx-${look}`, `Compression (BB width, ${look} bars) then expansion`, { look }, (k) => compressionBreak(k, look)));
  for (const [s, start] of Object.entries(ORB_SESSIONS))
    out.push(spec("break", `orb-${s}`, `Opening range break, ${s} session`, { p: OR_MIN, start }, (k) => openingRangeBreak(k, start)));
  for (const n of [12, 48])
    out.push(spec("active", `rel-str-${n}`, `Relative strength with the market, ${n} bars`, { n }, (k) => relStrength(k, n)));
  for (const n of [12, 48])
    out.push(spec("active", `rel-div-${n}`, `Divergence from the market, ${n} bars (catch-up)`, { n }, (k) => relDivergence(k, n)));
  return out;
}
