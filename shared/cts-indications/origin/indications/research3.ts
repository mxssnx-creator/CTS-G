// Research signals, third batch ("r-…"): what the first two batches did not cover.
//
// Clock-phase flow (new). Kim & Hansen, "The Quarter-Hour Effect: Periodic Algorithmic Trading and Return
// Predictability in Cryptocurrency Futures" (arXiv 2607.09426, July 2026; six Binance USDT perpetuals, 2021–2024):
// volume and volatility burst at every quarter-hour boundary (:00/:15/:30/:45), driven by algorithmic order flow;
// returns at the boundaries reverse at the quarter-hour phase, and quarter-hour ORDER IMBALANCE predicts returns over
// four to twelve hours. From 1m OHLCV the imbalance of a boundary bar is approximated by its close-location-weighted
// volume (the share of the bar's range the close sits above its middle, times the volume).
//
// Classic indicators missing from the registry: Ehlers' Laguerre RSI, DeMark's TD setup, the choppiness index (a
// range ending in a trend), the chandelier exit, Williams' ultimate oscillator and the Klinger volume oscillator.
//
// As with the first two batches, these are candidates for the causal ranking, the PF gates and the entry filters —
// not proven edges: the literature finds no single OHLCV signal that clears a 0.2 % round trip on its own. Every state
// is non-zero only on the bars the signal fires and reads bars 0..i only. Short and slower "-m" variant each.
import type { SeriesCache } from "./cache.ts";
import type { IndicationSpec } from "./registry.ts";
import type { IndicationKind } from "../domain/types.ts";
import * as I from "../math/indicators.ts";

type F64 = Float64Array;
const fin = (x: number) => Number.isFinite(x);
const QH = 15 * 60_000;

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

function rmax(x: F64, p: number): F64 {
  const out = new Float64Array(x.length).fill(NaN);
  for (let i = p - 1; i < x.length; i++) {
    let m = -Infinity;
    for (let j = i - p + 1; j <= i; j++) if (x[j] > m) m = x[j];
    out[i] = m;
  }
  return out;
}
function rmin(x: F64, p: number): F64 {
  const out = new Float64Array(x.length).fill(NaN);
  for (let i = p - 1; i < x.length; i++) {
    let m = Infinity;
    for (let j = i - p + 1; j <= i; j++) if (x[j] < m) m = x[j];
    out[i] = m;
  }
  return out;
}

/** Bar i opens on a quarter-hour boundary. On 15m and 30m bars every bar does; on 1m and 5m bars one in 15 / 3. */
export function isQuarterHour(t: number): boolean {
  return t % QH === 0;
}

/** Close location of a bar in [-1, 1]: +1 closed at the high, -1 at the low, 0 in the middle or a flat bar. */
const clv = (h: number, l: number, c: number) => (h > l ? (2 * c - h - l) / (h - l) : 0);

/** Ehlers' Laguerre RSI in [0, 1]. */
export function laguerreRsi(c: F64, g: number): F64 {
  const out = new Float64Array(c.length).fill(NaN);
  let l0 = c[0],
    l1 = c[0],
    l2 = c[0],
    l3 = c[0];
  for (let i = 1; i < c.length; i++) {
    const p0 = l0,
      p1 = l1,
      p2 = l2;
    l0 = (1 - g) * c[i] + g * l0;
    l1 = -g * l0 + p0 + g * l1;
    l2 = -g * l1 + p1 + g * l2;
    l3 = -g * l2 + p2 + g * l3;
    let cu = 0,
      cd = 0;
    for (const d of [l0 - l1, l1 - l2, l2 - l3]) {
      if (d >= 0) cu += d;
      else cd -= d;
    }
    if (i >= 8) out[i] = cu + cd > 0 ? cu / (cu + cd) : 0.5;
  }
  return out;
}

/** Choppiness index (0–100): high in a range, low in a trend. */
export function choppiness(h: F64, l: F64, c: F64, p: number): F64 {
  const tr = I.trueRange(h, l, c);
  const hh = rmax(h, p);
  const ll = rmin(l, p);
  const out = new Float64Array(c.length).fill(NaN);
  let s = 0;
  for (let i = 0; i < c.length; i++) {
    s += fin(tr[i]) ? tr[i] : 0;
    if (i >= p) s -= fin(tr[i - p]) ? tr[i - p] : 0;
    const rng = hh[i] - ll[i];
    if (i >= p && rng > 0) out[i] = (100 * Math.log10(s / rng)) / Math.log10(p);
  }
  return out;
}

/** Williams' ultimate oscillator (0–100) over three windows. */
export function ultimate(h: F64, l: F64, c: F64, a: number, b: number, d: number): F64 {
  const n = c.length;
  const bp = new Float64Array(n);
  const tr = new Float64Array(n);
  for (let i = 1; i < n; i++) {
    const lo = Math.min(l[i], c[i - 1]);
    bp[i] = c[i] - lo;
    tr[i] = Math.max(h[i], c[i - 1]) - lo;
  }
  const out = new Float64Array(n).fill(NaN);
  const avg = (p: number, i: number) => {
    let x = 0,
      y = 0;
    for (let j = i - p + 1; j <= i; j++) {
      x += bp[j];
      y += tr[j];
    }
    return y > 0 ? x / y : NaN;
  };
  for (let i = d; i < n; i++) {
    const x = avg(a, i),
      y = avg(b, i),
      z = avg(d, i);
    if (fin(x) && fin(y) && fin(z)) out[i] = (100 * (4 * x + 2 * y + z)) / 7;
  }
  return out;
}

/** Klinger volume oscillator and its signal line. */
export function klinger(h: F64, l: F64, c: F64, v: F64, f: number, s: number, sig: number): { kvo: F64; signal: F64 } {
  const n = c.length;
  const vf = new Float64Array(n);
  let trend = 0,
    cm = 0,
    dmPrev = 0;
  for (let i = 1; i < n; i++) {
    const tp = h[i] + l[i] + c[i];
    const tpPrev = h[i - 1] + l[i - 1] + c[i - 1];
    const t = tp > tpPrev ? 1 : tp < tpPrev ? -1 : trend;
    const dm = h[i] - l[i];
    cm = t === trend ? cm + dm : dmPrev + dm;
    trend = t;
    dmPrev = dm;
    vf[i] = cm > 0 ? v[i] * Math.abs(2 * (dm / cm) - 1) * t * 100 : 0;
  }
  const ef = I.ema(vf, f);
  const es = I.ema(vf, s);
  const kvo = new Float64Array(n).fill(NaN);
  for (let i = 0; i < n; i++) if (fin(ef[i]) && fin(es[i])) kvo[i] = ef[i] - es[i];
  return { kvo, signal: I.ema(kvo, sig) };
}

export function research3Specs(): IndicationSpec[] {
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

  // 1 quarter-hour order flow: the close-location-weighted volume of the last `n` quarter-hour boundary bars, against
  // the average volume of the last 60 bars. The paper's imbalance predicts four to twelve hours ahead, so this is a
  // continuation entry, fired on a boundary bar when the pooled imbalance clears `thr` either way.
  both("volume", "r-qh-flow", "Quarter-hour order flow", { n: 4, thr: 0.35 }, { n: 8, thr: 0.3 }, (k, p) => {
    const { t, h, l, c, v, n } = k.b;
    const vAvg = I.sma(v, 60);
    const flows: number[] = [];
    return ev(n, (i) => {
      if (!isQuarterHour(t[i]) || !(vAvg[i] > 0)) return 0;
      flows.push((clv(h[i], l[i], c[i]) * v[i]) / vAvg[i]);
      if (flows.length > p.n) flows.shift();
      if (flows.length < p.n) return 0;
      const m = flows.reduce((a, b) => a + b, 0) / p.n;
      return m > p.thr ? 1 : m < -p.thr ? -1 : 0;
    });
  });

  // 2 quarter-hour burst reversal: a boundary bar whose return is `z` standard deviations out AND whose volume is a
  // burst (above `vx` times the 60-bar average) is faded — the paper's reversal at the quarter-hour phase
  both("move", "r-qh-rev", "Quarter-hour burst reversal", { z: 2.5, vx: 1.5 }, { z: 3, vx: 2 }, (k, p) => {
    const { t, o, c, v, n } = k.b;
    const r = new Float64Array(n).fill(NaN);
    for (let i = 0; i < n; i++) if (o[i] > 0) r[i] = c[i] / o[i] - 1;
    const sd = I.stdev(r, 60);
    const vAvg = I.sma(v, 60);
    return ev(n, (i) => {
      if (i < 61 || !isQuarterHour(t[i]) || !(sd[i] > 0) || !(vAvg[i] > 0)) return 0;
      if (v[i] < p.vx * vAvg[i]) return 0;
      return r[i] > p.z * sd[i] ? -1 : r[i] < -p.z * sd[i] ? 1 : 0;
    });
  });

  // 3 Laguerre RSI turn: a cross back up through `lo` from below is long, back down through `hi` short (fade)
  both("rsi", "r-laguerre", "Laguerre RSI turn", { g: 0.5, lo: 0.15, hi: 0.85 }, { g: 0.7, lo: 0.1, hi: 0.9 }, (k, p) => {
    const x = laguerreRsi(k.b.c, p.g);
    return ev(k.b.n, (i) => {
      if (!(fin(x[i]) && fin(x[i - 1]))) return 0;
      if (x[i - 1] < p.lo && x[i] >= p.lo) return 1;
      if (x[i - 1] > p.hi && x[i] <= p.hi) return -1;
      return 0;
    });
  });

  // 4 DeMark TD setup: `n` closes in a row each above (below) the close four bars earlier is an exhausted run, faded
  // on the bar the count completes (9 = the classic setup; 13 in the slower variant)
  both("move", "r-td", "TD setup exhaustion", { n: 9 }, { n: 13 }, (k, p) => {
    const { c, n } = k.b;
    let up = 0,
      dn = 0;
    return ev(n, (i) => {
      if (i < 4) return 0;
      up = c[i] > c[i - 4] ? up + 1 : 0;
      dn = c[i] < c[i - 4] ? dn + 1 : 0;
      if (up === p.n) return -1;
      if (dn === p.n) return 1;
      return 0;
    });
  });

  // 5 choppiness break: the index drops under `lo` within `m` bars of having been over `hi` — a range just turned into
  // a trend; the direction is the move over the window. Fired once, on the bar it first crosses under.
  both("break", "r-chop", "Choppiness break", { p: 14, lo: 38.2, hi: 61.8, m: 20 }, { p: 28, lo: 38.2, hi: 61.8, m: 40 }, (k, q) => {
    const { h, l, c, n } = k.b;
    const ch = choppiness(h, l, c, q.p);
    const hiMax = rmax(
      ch.map((x) => (fin(x) ? x : -Infinity)),
      q.m,
    );
    return ev(n, (i) => {
      if (i < q.p + q.m || !(fin(ch[i]) && fin(ch[i - 1]))) return 0;
      if (!(ch[i] < q.lo && ch[i - 1] >= q.lo && hiMax[i - 1] > q.hi)) return 0;
      return Math.sign(c[i] - c[i - q.p]);
    });
  });

  // 6 chandelier exit flip: the close crosses the trailing chandelier stop of the opposite side — trend flips there
  both("trend", "r-chand", "Chandelier flip", { p: 22, m: 3 }, { p: 44, m: 3.5 }, (k, q) => {
    const { h, l, c, n } = k.b;
    const a = I.atr(h, l, c, q.p);
    const hh = rmax(h, q.p);
    const ll = rmin(l, q.p);
    let dir = 0;
    return ev(n, (i) => {
      if (i < q.p + 1 || !(fin(a[i - 1]) && fin(hh[i - 1]) && fin(ll[i - 1]))) return 0;
      const longStop = hh[i - 1] - q.m * a[i - 1];
      const shortStop = ll[i - 1] + q.m * a[i - 1];
      const d = c[i] > shortStop ? 1 : c[i] < longStop ? -1 : dir;
      const flip = dir !== 0 && d !== dir ? d : 0;
      dir = d;
      return flip;
    });
  });

  // 7 ultimate oscillator turn from an extreme (fade)
  both("osc", "r-ultimate", "Ultimate oscillator turn", { a: 7, b: 14, d: 28, lo: 30, hi: 70 }, { a: 14, b: 28, d: 56, lo: 25, hi: 75 }, (k, p) => {
    const { h, l, c, n } = k.b;
    const u = ultimate(h, l, c, p.a, p.b, p.d);
    return ev(n, (i) => {
      if (!(fin(u[i]) && fin(u[i - 1]))) return 0;
      if (u[i - 1] < p.lo && u[i] >= p.lo) return 1;
      if (u[i - 1] > p.hi && u[i] <= p.hi) return -1;
      return 0;
    });
  });

  // 8 Klinger volume oscillator: a signal-line cross on the side of zero the cross points away from (volume force
  // turning with the trend, not against it)
  both("volume", "r-klinger", "Klinger cross", { f: 34, s: 55, sig: 13 }, { f: 55, s: 89, sig: 21 }, (k, p) => {
    const { h, l, c, v, n } = k.b;
    const { kvo, signal } = klinger(h, l, c, v, p.f, p.s, p.sig);
    return ev(n, (i) => {
      if (!(fin(kvo[i]) && fin(kvo[i - 1]) && fin(signal[i]) && fin(signal[i - 1]))) return 0;
      if (kvo[i - 1] <= signal[i - 1] && kvo[i] > signal[i] && kvo[i] > 0) return 1;
      if (kvo[i - 1] >= signal[i - 1] && kvo[i] < signal[i] && kvo[i] < 0) return -1;
      return 0;
    });
  });

  return out;
}
