// Research signals ("r-…"): short-term entry signals from OHLCV that the literature and practitioners describe
// for crypto perpetuals on 1–30 minute bars (volume-confirmed breakouts, squeeze release, stop-run reversals,
// forced-flow fades, VWAP reclaim, CLV / volume-delta divergence, RSI(2) and Bollinger reversion with regime
// gates, session-conditioned trend, volatility-regime breakouts). None of them clears the round-trip cost on its
// own (measured: docs/signals-validation.md); they are candidates the causal ranking, the entry filters
// (signals.filter) and the confirmation coordination select from. Every state is non-zero exactly on the bars the
// signal fires (the "follow" bot enters on onsets) and uses bars 0..i only. Each comes in a short and a slower
// "-m" range.
import type { SeriesCache } from "./cache.ts";
import type { IndicationSpec } from "./registry.ts";
import type { IndicationKind } from "../domain/types.ts";
import * as I from "../math/indicators.ts";

type F64 = Float64Array;
const fin = (x: number) => Number.isFinite(x);
const H = 3_600_000;

/** volume z-score over p bars */
function vz(k: SeriesCache, p: number): F64 {
  return k.memo(`rvz${p}`, () => {
    const v = k.b.v;
    const m = I.sma(v, p);
    const s = I.stdev(v, p);
    const out = new Float64Array(v.length).fill(NaN);
    for (let i = 0; i < v.length; i++) if (s[i] > 0) out[i] = (v[i] - m[i]) / s[i];
    return out;
  });
}

/** close-location value: +1 closed at the high, −1 at the low */
function clv(k: SeriesCache): F64 {
  return k.memo("rclv", () => {
    const { h, l, c, n } = k.b;
    const out = new Float64Array(n);
    for (let i = 0; i < n; i++)
      out[i] = h[i] > l[i] ? (c[i] - l[i] - (h[i] - c[i])) / (h[i] - l[i]) : 0;
    return out;
  });
}

/** UTC-day anchored VWAP (typical price × volume, reset at 00:00 UTC) */
function sessionVwap(k: SeriesCache): F64 {
  return k.memo("rsvwap", () => {
    const { t, h, l, c, v, n } = k.b;
    const out = new Float64Array(n).fill(NaN);
    let day = -1;
    let pv = 0;
    let vv = 0;
    for (let i = 0; i < n; i++) {
      const d = Math.floor(t[i] / (24 * H));
      if (d !== day) {
        day = d;
        pv = 0;
        vv = 0;
      }
      pv += ((h[i] + l[i] + c[i]) / 3) * v[i];
      vv += v[i];
      out[i] = vv > 0 ? pv / vv : NaN;
    }
    return out;
  });
}

/** rolling sum of x over p bars */
function rsum(x: F64, p: number): F64 {
  const out = new Float64Array(x.length).fill(NaN);
  let s = 0;
  for (let i = 0; i < x.length; i++) {
    s += x[i];
    if (i >= p) s -= x[i - p];
    if (i >= p - 1) out[i] = s;
  }
  return out;
}

const mk = (
  kind: IndicationKind,
  id: string,
  label: string,
  params: Record<string, number>,
  fn: (k: SeriesCache) => Int8Array,
): IndicationSpec => ({ id, kind, label, params, fn });

function ev(n: number, f: (i: number) => number): Int8Array {
  const out = new Int8Array(n);
  for (let i = 0; i < n; i++) {
    const v = f(i);
    out[i] = v > 0 ? 1 : v < 0 ? -1 : 0;
  }
  return out;
}

export function researchSpecs(): IndicationSpec[] {
  const out: IndicationSpec[] = [];
  const both = (
    kind: IndicationKind,
    id: string,
    label: string,
    short: Record<string, number>,
    medium: Record<string, number>,
    fn: (k: SeriesCache, p: Record<string, number>) => Int8Array,
  ) => {
    out.push(mk(kind, id, `${label} (short)`, short, (k) => fn(k, short)));
    out.push(mk(kind, `${id}-m`, `${label} (medium)`, medium, (k) => fn(k, medium)));
  };

  // 1 TTM squeeze release: Bollinger bands inside the Keltner channel for `bars` bars, then out; direction of the
  // momentum (close against the mean of the channel midpoint and the SMA)
  both(
    "bollinger",
    "r-squeeze",
    "Squeeze release",
    { bars: 4, kc: 1.5 },
    { bars: 8, kc: 1.5 },
    (k, p) => {
      const { c, n } = k.b;
      const bb = k.bb(20, 2);
      const kc = I.keltner(k.b.h, k.b.l, c, 20, p.kc);
      const hi = I.donchianPrior(k.b.h, k.b.l, 20);
      let run = 0;
      return ev(n, (i) => {
        if (![bb.up[i], bb.lo[i], kc.up[i], kc.lo[i], hi.hi[i], hi.lo[i]].every(fin)) return 0;
        const inside = bb.up[i] < kc.up[i] && bb.lo[i] > kc.lo[i];
        const wasSqueezed = run >= p.bars;
        run = inside ? run + 1 : 0;
        if (inside || !wasSqueezed) return 0;
        const mom = c[i] - ((hi.hi[i] + hi.lo[i]) / 2 + bb.mid[i]) / 2;
        return mom;
      });
    },
  );

  // 2 volume-confirmed Donchian breakout with a range-expansion and close-location confirmation
  both(
    "break",
    "r-donch-vol",
    "Donchian + volume + CLV",
    { p: 20, vz: 2, rng: 1.5 },
    { p: 40, vz: 2.5, rng: 1.8 },
    (k, p) => {
      const { h, l, c, n } = k.b;
      const d = I.donchianPrior(h, l, p.p);
      const a = k.atr(14);
      const z = vz(k, 20);
      const q = clv(k);
      return ev(n, (i) => {
        if (!(fin(d.hi[i]) && fin(a[i]) && fin(z[i]))) return 0;
        if (z[i] < p.vz || h[i] - l[i] < p.rng * a[i]) return 0;
        if (c[i] > d.hi[i] && q[i] > 0.5) return 1;
        if (c[i] < d.lo[i] && q[i] < -0.5) return -1;
        return 0;
      });
    },
  );

  // 3 liquidity sweep / stop-run reversal: the bar pierces the prior extreme, closes back inside on a long wick
  both(
    "move",
    "r-sweep",
    "Stop-run reversal",
    { p: 20, wick: 0.6, vz: 1.5 },
    { p: 40, wick: 0.6, vz: 2 },
    (k, p) => {
      const { o, h, l, c, n } = k.b;
      const d = I.donchianPrior(h, l, p.p);
      const z = vz(k, 20);
      return ev(n, (i) => {
        if (!(fin(d.hi[i]) && fin(z[i])) || z[i] < p.vz || h[i] <= l[i]) return 0;
        const rng = h[i] - l[i];
        const up = (h[i] - Math.max(o[i], c[i])) / rng;
        const dn = (Math.min(o[i], c[i]) - l[i]) / rng;
        if (h[i] > d.hi[i] && c[i] < d.hi[i] && up > p.wick) return -1;
        if (l[i] < d.lo[i] && c[i] > d.lo[i] && dn > p.wick) return 1;
        return 0;
      });
    },
  );

  // 4 capitulation fade: a bar of ≥ mul × ATR on a volume spike that closes off its extreme → fade it
  both("move", "r-capit", "Forced-flow fade", { mul: 3, vz: 3 }, { mul: 3.5, vz: 3.5 }, (k, p) => {
    const { o, c, n } = k.b;
    const a = k.atr(14);
    const z = vz(k, 20);
    const q = clv(k);
    return ev(n, (i) => {
      if (i < 1 || !(fin(a[i - 1]) && fin(z[i])) || z[i] < p.vz) return 0;
      const mv = c[i] - o[i];
      if (mv < -p.mul * a[i - 1] && q[i] > -0.2) return 1;
      if (mv > p.mul * a[i - 1] && q[i] < 0.2) return -1;
      return 0;
    });
  });

  // 5 session VWAP reclaim (anchored at 00:00 UTC): the close crosses the VWAP on volume and holds
  both(
    "volume",
    "r-vwap-reclaim",
    "VWAP reclaim",
    { vz: 1, hold: 1 },
    { vz: 1.5, hold: 2 },
    (k, p) => {
      const { c, n } = k.b;
      const w = sessionVwap(k);
      const z = vz(k, 20);
      return ev(n, (i) => {
        const j = i - p.hold;
        if (j < 1 || !(fin(w[i]) && fin(w[j]) && fin(w[j - 1]) && fin(z[j]))) return 0;
        if (z[j] < p.vz) return 0;
        // the reclaim bar is j, the following `hold` bars stay on the same side
        let up = c[j - 1] < w[j - 1] && c[j] > w[j];
        let dn = c[j - 1] > w[j - 1] && c[j] < w[j];
        for (let m = j + 1; m <= i; m++) {
          if (c[m] <= w[m]) up = false;
          if (c[m] >= w[m]) dn = false;
        }
        return up ? 1 : dn ? -1 : 0;
      });
    },
  );

  // 6 CLV volume-delta divergence: a new p-bar extreme on a weaker cumulative delta (V × CLV) → fade
  both("volume", "r-cvd-div", "Volume-delta divergence", { p: 20 }, { p: 40 }, (k, p) => {
    const { h, l, v, n } = k.b;
    const q = clv(k);
    const delta = new Float64Array(n);
    for (let i = 0; i < n; i++) delta[i] = v[i] * q[i];
    const s = rsum(delta, p.p);
    const d = I.donchianPrior(h, l, p.p);
    // the delta sum at the previous extreme of the window
    return ev(n, (i) => {
      if (!(fin(d.hi[i]) && fin(s[i])) || i < p.p * 2) return 0;
      let smax = -Infinity;
      let smin = Infinity;
      for (let j = i - p.p; j < i; j++) {
        if (s[j] > smax) smax = s[j];
        if (s[j] < smin) smin = s[j];
      }
      if (h[i] > d.hi[i] && s[i] < smax) return -1;
      if (l[i] < d.lo[i] && s[i] > smin) return 1;
      return 0;
    });
  });

  // 7 RSI(2) reversion with the slow trend (long only above the EMA, short only below)
  both(
    "rsi",
    "r-rsi2",
    "RSI(2) reversion + trend",
    { p: 2, lo: 5, ema: 200 },
    { p: 3, lo: 10, ema: 100 },
    (k, p) => {
      const { c, n } = k.b;
      const r = k.rsi(p.p);
      const e = k.ema(p.ema);
      return ev(n, (i) => {
        if (!(fin(r[i]) && fin(e[i]))) return 0;
        if (r[i] < p.lo && c[i] > e[i]) return 1;
        if (r[i] > 100 - p.lo && c[i] < e[i]) return -1;
        return 0;
      });
    },
  );

  // 8 Bollinger %B extreme on a quiet trend (ADX below the cap) with RSI(2) confirmation
  both(
    "bollinger",
    "r-bb-adx",
    "Bollinger extreme, ADX low",
    { adx: 25, rsi: 10 },
    { adx: 20, rsi: 15 },
    (k, p) => {
      const { c, n } = k.b;
      const bb = k.bb(20, 2);
      const { adx } = k.dmi(14);
      const r = k.rsi(2);
      return ev(n, (i) => {
        if (!(fin(bb.up[i]) && fin(adx[i]) && fin(r[i])) || adx[i] >= p.adx) return 0;
        if (c[i] < bb.lo[i] && r[i] < p.rsi) return 1;
        if (c[i] > bb.up[i] && r[i] > 100 - p.rsi) return -1;
        return 0;
      });
    },
  );

  // 9 Bollinger bandwidth expansion: the width rises from the bottom fifth of its history to above the 40th
  // percentile within 5 bars, direction = close against the mid band
  both(
    "bollinger",
    "r-bbw-expand",
    "Bandwidth expansion",
    { look: 500, from: 0.2, to: 0.4 },
    { look: 800, from: 0.15, to: 0.4 },
    (k, p) => {
      const { c, n } = k.b;
      const bb = k.bb(20, 2);
      const w = bb.width;
      const pct = k.memo(`rbbwp${p.look}`, () => {
        const o = new Float64Array(n).fill(NaN);
        for (let i = 40; i < n; i++) {
          const a = Math.max(20, i - p.look);
          let below = 0;
          let cnt = 0;
          for (let j = a; j < i; j += 2) {
            cnt++;
            if (w[j] < w[i]) below++;
          }
          o[i] = cnt ? below / cnt : NaN;
        }
        return o;
      });
      return ev(n, (i) => {
        if (i < 46 || !fin(pct[i]) || pct[i] < p.to || pct[i - 1] >= p.to) return 0;
        let low = false;
        for (let j = Math.max(0, i - 5); j < i; j++) if (fin(pct[j]) && pct[j] < p.from) low = true;
        return low ? Math.sign(c[i] - bb.mid[i]) : 0;
      });
    },
  );

  // 10 session-conditioned trend: EMA trend + a 12-bar move of ≥ mul × ATR, only in the liquid sessions
  // (US/EU overlap 12–17 UTC, Asia open 0–2 UTC)
  both(
    "direction",
    "r-session-trend",
    "Session trend",
    { bars: 12, mul: 1.5, ema: 50 },
    { bars: 24, mul: 2, ema: 100 },
    (k, p) => {
      const { t, c, n } = k.b;
      const e = k.ema(p.ema);
      const a = k.atr(14);
      return ev(n, (i) => {
        if (i < p.bars || !(fin(e[i]) && fin(a[i]))) return 0;
        const hr = new Date(t[i]).getUTCHours();
        if (!((hr >= 12 && hr < 17) || hr < 2)) return 0;
        const mv = c[i] - c[i - p.bars];
        if (c[i] > e[i] && mv > p.mul * a[i]) return 1;
        if (c[i] < e[i] && mv < -p.mul * a[i]) return -1;
        return 0;
      });
    },
  );

  // 11 distance-from-mean z-score fade with a volatility floor
  both(
    "move",
    "r-zdist",
    "Distance z-score fade",
    { z: 2.5, look: 100 },
    { z: 3, look: 200 },
    (k, p) => {
      const { c, n } = k.b;
      const m = k.sma(20);
      const d = new Float64Array(n).fill(NaN);
      for (let i = 0; i < n; i++) if (fin(m[i]) && m[i] > 0) d[i] = (c[i] - m[i]) / m[i];
      const s = I.stdev(
        d.map((x) => (fin(x) ? x : 0)),
        p.look,
      );
      return ev(n, (i) => {
        if (!(fin(d[i]) && fin(s[i])) || s[i] <= 0 || i < p.look) return 0;
        const z = d[i] / s[i];
        return z > p.z ? -1 : z < -p.z ? 1 : 0;
      });
    },
  );

  // 12 volatility-regime breakout: Donchian break only while ATR(14) ÷ ATR(100) shows expanding volatility
  both(
    "break",
    "r-vol-regime",
    "Regime breakout",
    { p: 20, ratio: 1.1 },
    { p: 40, ratio: 1.25 },
    (k, p) => {
      const { h, l, c, n } = k.b;
      const d = I.donchianPrior(h, l, p.p);
      const a14 = k.atr(14);
      const a100 = k.atr(100);
      return ev(n, (i) => {
        if (!(fin(d.hi[i]) && fin(a14[i]) && fin(a100[i])) || a14[i] < p.ratio * a100[i]) return 0;
        return c[i] > d.hi[i] ? 1 : c[i] < d.lo[i] ? -1 : 0;
      });
    },
  );

  // 13 close-location thrust: a strong bar closing at its extreme on a volume spike (continuation)
  both(
    "active",
    "r-clv-thrust",
    "CLV thrust",
    { clv: 0.8, vz: 1.5, rng: 1 },
    { clv: 0.9, vz: 2, rng: 1.3 },
    (k, p) => {
      const { h, l, n } = k.b;
      const q = clv(k);
      const z = vz(k, 20);
      const a = k.atr(14);
      return ev(n, (i) => {
        if (!(fin(z[i]) && fin(a[i])) || z[i] < p.vz || h[i] - l[i] < p.rng * a[i]) return 0;
        return q[i] > p.clv ? 1 : q[i] < -p.clv ? -1 : 0;
      });
    },
  );

  // 14 narrow-range (NR7) breakout: the bar after the narrowest of seven bars closes beyond its range
  both("break", "r-nr-break", "NR7 breakout", { n: 7 }, { n: 4 }, (k, p) => {
    const { h, l, c, n } = k.b;
    return ev(n, (i) => {
      if (i < p.n + 1) return 0;
      const r = h[i - 1] - l[i - 1];
      for (let j = i - p.n; j < i - 1; j++) if (h[j] - l[j] <= r) return 0;
      return c[i] > h[i - 1] ? 1 : c[i] < l[i - 1] ? -1 : 0;
    });
  });

  return out;
}

export const RESEARCH_SOURCES: ReadonlyArray<{ name: string; label: string }> = [
  { name: "r-squeeze", label: "Squeeze release" },
  { name: "r-donch-vol", label: "Donchian + volume + CLV" },
  { name: "r-sweep", label: "Stop-run reversal" },
  { name: "r-capit", label: "Forced-flow fade" },
  { name: "r-vwap-reclaim", label: "VWAP reclaim" },
  { name: "r-cvd-div", label: "Volume-delta divergence" },
  { name: "r-rsi2", label: "RSI(2) reversion + trend" },
  { name: "r-bb-adx", label: "Bollinger extreme, ADX low" },
  { name: "r-bbw-expand", label: "Bandwidth expansion" },
  { name: "r-session-trend", label: "Session trend" },
  { name: "r-zdist", label: "Distance z-score fade" },
  { name: "r-vol-regime", label: "Regime breakout" },
  { name: "r-clv-thrust", label: "CLV thrust" },
  { name: "r-nr-break", label: "NR7 breakout" },
];
