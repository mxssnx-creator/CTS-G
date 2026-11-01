// Research signals, second batch ("r-…"): rarer OHLCV indicators and cycle filters (Ehlers roofing filter, Fisher,
// Connors RSI, Schaff trend cycle, Vortex, TSI, Elder ray + force index, Awesome oscillator, linear-regression
// channel, Williams fractals, Camarilla pivots) and price-action / market-structure rules (failed breakout, Wyckoff
// spring, value-area re-entry, opening-range and previous-day breaks, break of structure, change of character, fair
// value gap retest, inside-bar break, engulfing, pin bar, morning / evening star, exhaustion streak, vote of weak
// signals). The literature finds no single OHLCV signal that clears a 0.2 % round trip on its own, so these are
// candidates for the causal ranking, the PF acceptance and the entry filters — not proven edges. Every state is
// non-zero exactly on the bars the signal fires and uses bars 0..i only (no confirmation "from the future": swing
// points are used only once their right-hand bars have closed). Short and slower "-m" range each.
import type { SeriesCache } from "./cache.ts";
import type { IndicationSpec } from "./registry.ts";
import type { IndicationKind } from "../domain/types.ts";
import * as I from "../math/indicators.ts";

type F64 = Float64Array;
const fin = (x: number) => Number.isFinite(x);
const H = 3_600_000;
const DAY = 24 * H;

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

/** state of a crossing: +1 on the bar a crosses above b, −1 below (both finite) */
function crossState(a: F64, b: F64): Int8Array {
  const out = new Int8Array(a.length);
  for (let i = 1; i < a.length; i++) {
    if (!(fin(a[i]) && fin(b[i]) && fin(a[i - 1]) && fin(b[i - 1]))) continue;
    if (a[i - 1] <= b[i - 1] && a[i] > b[i]) out[i] = 1;
    else if (a[i - 1] >= b[i - 1] && a[i] < b[i]) out[i] = -1;
  }
  return out;
}

/** volume z-score over 20 bars (same as research.ts) */
function vz20(k: SeriesCache): F64 {
  return k.memo("rvz20", () => {
    const v = k.b.v;
    const m = I.sma(v, 20);
    const s = I.stdev(v, 20);
    const out = new Float64Array(v.length).fill(NaN);
    for (let i = 0; i < v.length; i++) if (s[i] > 0) out[i] = (v[i] - m[i]) / s[i];
    return out;
  });
}

/** Kaufman efficiency ratio over p bars */
function er(k: SeriesCache, p: number): F64 {
  return k.memo(`rer${p}`, () => {
    const c = k.b.c;
    const out = new Float64Array(c.length).fill(NaN);
    let path = 0;
    for (let i = 1; i < c.length; i++) {
      path += Math.abs(c[i] - c[i - 1]);
      if (i > p) path -= Math.abs(c[i - p] - c[i - p - 1]);
      if (i >= p && path > 0) out[i] = Math.abs(c[i] - c[i - p]) / path;
    }
    return out;
  });
}

/** Ehlers roofing filter: 48-bar high-pass then a 10-bar SuperSmoother */
function roofing(k: SeriesCache, hp: number, ss: number): F64 {
  return k.memo(`rroof${hp}.${ss}`, () => {
    const c = k.b.c;
    const n = c.length;
    const a1 =
      (Math.cos((0.707 * 2 * Math.PI) / hp) + Math.sin((0.707 * 2 * Math.PI) / hp) - 1) /
      Math.cos((0.707 * 2 * Math.PI) / hp);
    const hpv = new Float64Array(n);
    for (let i = 2; i < n; i++)
      hpv[i] =
        (1 - a1 / 2) * (1 - a1 / 2) * (c[i] - 2 * c[i - 1] + c[i - 2]) +
        2 * (1 - a1) * hpv[i - 1] -
        (1 - a1) * (1 - a1) * hpv[i - 2];
    const a = Math.exp((-1.414 * Math.PI) / ss);
    const b = 2 * a * Math.cos((1.414 * Math.PI) / ss);
    const c2 = b;
    const c3 = -a * a;
    const c1 = 1 - c2 - c3;
    const f = new Float64Array(n).fill(NaN);
    for (let i = 2; i < n; i++)
      f[i] =
        c1 * ((hpv[i] + hpv[i - 1]) / 2) +
        c2 * (fin(f[i - 1]) ? f[i - 1] : 0) +
        c3 * (fin(f[i - 2]) ? f[i - 2] : 0);
    return f;
  });
}

/** rolling max / min of x over p bars ending at i (inclusive) */
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

/** confirmed swing pivots: a pivot at bar t is known from bar t + w on. `hi[i]` = index of the newest pivot high known at i */
function pivots(
  k: SeriesCache,
  w: number,
): { hi: Int32Array; lo: Int32Array; hi2: Int32Array; lo2: Int32Array } {
  return k.memo(`rpiv${w}`, () => {
    const { h, l, n } = k.b;
    const hi = new Int32Array(n).fill(-1);
    const lo = new Int32Array(n).fill(-1);
    const hi2 = new Int32Array(n).fill(-1);
    const lo2 = new Int32Array(n).fill(-1);
    let ph = -1;
    let ph2 = -1;
    let pl = -1;
    let pl2 = -1;
    for (let i = 0; i < n; i++) {
      const t = i - w; // candidate pivot bar, its right-hand bars i-w+1..i have closed
      if (t >= w) {
        let isH = true;
        let isL = true;
        for (let j = t - w; j <= t + w; j++) {
          if (j === t) continue;
          if (h[j] >= h[t]) isH = false;
          if (l[j] <= l[t]) isL = false;
        }
        if (isH) {
          ph2 = ph;
          ph = t;
        }
        if (isL) {
          pl2 = pl;
          pl = t;
        }
      }
      hi[i] = ph;
      hi2[i] = ph2;
      lo[i] = pl;
      lo2[i] = pl2;
    }
    return { hi, lo, hi2, lo2 };
  });
}

export function research2Specs(): IndicationSpec[] {
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

  // 1 Ehlers roofing filter zero-cross, only when the filter is large against its own history and price trends
  both(
    "trend",
    "r-roofing",
    "Roofing filter cross",
    { hp: 48, ss: 10, min: 0.5 },
    { hp: 96, ss: 20, min: 0.7 },
    (k, p) => {
      const f = roofing(k, p.hp, p.ss);
      const sd = I.stdev(
        f.map((x) => (fin(x) ? x : 0)),
        100,
      );
      const e = er(k, 14);
      return ev(k.b.n, (i) => {
        if (i < 120 || !(fin(f[i]) && fin(f[i - 1]) && fin(sd[i]) && fin(e[i])) || sd[i] <= 0)
          return 0;
        if (Math.abs(f[i]) < p.min * sd[i] || e[i] < 0.15) return 0;
        if (f[i - 1] <= 0 && f[i] > 0 && f[i] > f[i - 1]) return 1;
        if (f[i - 1] >= 0 && f[i] < 0 && f[i] < f[i - 1]) return -1;
        return 0;
      });
    },
  );

  // 2 Fisher transform turn from an extreme (fade)
  both("osc", "r-fisher", "Fisher turn", { n: 10, ext: 1.5 }, { n: 20, ext: 1.8 }, (k, p) => {
    const { h, l, n } = k.b;
    const hh = rmax(h, p.n);
    const ll = rmin(l, p.n);
    const fish = new Float64Array(n).fill(NaN);
    let v = 0;
    let f1 = 0;
    for (let i = p.n; i < n; i++) {
      const rng = hh[i] - ll[i];
      if (!(rng > 0)) continue;
      const x = 2 * (((h[i] + l[i]) / 2 - ll[i]) / rng - 0.5);
      v = Math.max(-0.999, Math.min(0.999, 0.33 * x + 0.67 * v));
      f1 = 0.5 * Math.log((1 + v) / (1 - v)) + 0.5 * f1;
      fish[i] = f1;
    }
    const trig = new Float64Array(n).fill(NaN);
    for (let i = 1; i < n; i++) trig[i] = fish[i - 1];
    const cs = crossState(fish, trig);
    return ev(n, (i) => {
      if (cs[i] > 0 && fin(fish[i]) && fish[i] < -p.ext + 0.6) return 1;
      if (cs[i] < 0 && fin(fish[i]) && fish[i] > p.ext - 0.6) return -1;
      return 0;
    });
  });

  // 3 Connors RSI: RSI(3), RSI of the up / down streak, percent rank of the 1-bar return — fade with the slow trend
  both(
    "rsi",
    "r-connors",
    "Connors RSI fade",
    { lo: 10, ema: 200 },
    { lo: 15, ema: 100 },
    (k, p) => {
      const { c, n } = k.b;
      const r3 = k.rsi(3);
      const streak = new Float64Array(n);
      for (let i = 1; i < n; i++)
        streak[i] =
          c[i] > c[i - 1]
            ? Math.max(1, streak[i - 1] + 1)
            : c[i] < c[i - 1]
              ? Math.min(-1, streak[i - 1] - 1)
              : 0;
      const rs = I.rsi(streak, 2);
      const roc = new Float64Array(n);
      for (let i = 1; i < n; i++) roc[i] = c[i - 1] > 0 ? c[i] / c[i - 1] - 1 : 0;
      const e = k.ema(p.ema);
      return ev(n, (i) => {
        if (i < 110 || !(fin(r3[i]) && fin(rs[i]) && fin(e[i]))) return 0;
        let below = 0;
        for (let j = i - 100; j < i; j++) if (roc[j] < roc[i]) below++;
        const crsi = (r3[i] + rs[i] + below) / 3;
        if (crsi < p.lo && c[i] > e[i]) return 1;
        if (crsi > 100 - p.lo && c[i] < e[i]) return -1;
        return 0;
      });
    },
  );

  // 4 Schaff trend cycle crossing 25 / 75 (early trend entry)
  both(
    "macd",
    "r-stc",
    "Schaff trend cycle",
    { f: 23, s: 50, cyc: 10 },
    { f: 34, s: 75, cyc: 14 },
    (k, p) => {
      const { c, n } = k.b;
      const macd = new Float64Array(n);
      const ef = I.ema(c, p.f);
      const es = I.ema(c, p.s);
      for (let i = 0; i < n; i++) macd[i] = ef[i] - es[i];
      const stoch = (x: F64, cyc: number) => {
        const mx = rmax(x, cyc);
        const mn = rmin(x, cyc);
        const o = new Float64Array(n).fill(NaN);
        for (let i = 0; i < n; i++)
          if (fin(mx[i]) && mx[i] > mn[i]) o[i] = (100 * (x[i] - mn[i])) / (mx[i] - mn[i]);
        return o;
      };
      const smooth = (x: F64) => {
        const o = new Float64Array(n).fill(NaN);
        let prev = 0;
        for (let i = 0; i < n; i++) {
          if (!fin(x[i])) continue;
          prev = prev + 0.5 * (x[i] - prev);
          o[i] = prev;
        }
        return o;
      };
      const f1 = smooth(stoch(macd, p.cyc));
      const stc = smooth(
        stoch(
          f1.map((x) => (fin(x) ? x : 0)),
          p.cyc,
        ),
      );
      return ev(n, (i) => {
        if (i < p.s + 20 || !(fin(stc[i]) && fin(stc[i - 1]))) return 0;
        if (stc[i - 1] < 25 && stc[i] >= 25) return 1;
        if (stc[i - 1] > 75 && stc[i] <= 75) return -1;
        return 0;
      });
    },
  );

  // 5 Vortex indicator crossover with a spread
  both("trend", "r-vortex", "Vortex cross", { p: 14, gap: 0.1 }, { p: 21, gap: 0.15 }, (k, p) => {
    const { h, l, n } = k.b;
    const tr = I.trueRange(h, l, k.b.c);
    const vp = new Float64Array(n);
    const vm = new Float64Array(n);
    for (let i = 1; i < n; i++) {
      vp[i] = Math.abs(h[i] - l[i - 1]);
      vm[i] = Math.abs(l[i] - h[i - 1]);
    }
    const sp = I.sma(vp, p.p);
    const sm = I.sma(vm, p.p);
    const st = I.sma(tr, p.p);
    const up = new Float64Array(n).fill(NaN);
    const dn = new Float64Array(n).fill(NaN);
    for (let i = 0; i < n; i++)
      if (st[i] > 0) {
        up[i] = sp[i] / st[i];
        dn[i] = sm[i] / st[i];
      }
    const cs = crossState(up, dn);
    return ev(n, (i) => (cs[i] !== 0 && Math.abs(up[i] - dn[i]) >= p.gap * 0.3 ? cs[i] : 0));
  });

  // 6 True strength index crossing its signal on the right side of zero
  both("macd", "r-tsi", "TSI cross", { a: 13, b: 7, sig: 5 }, { a: 25, b: 13, sig: 7 }, (k, p) => {
    const { c, n } = k.b;
    const m = new Float64Array(n);
    const am = new Float64Array(n);
    for (let i = 1; i < n; i++) {
      m[i] = c[i] - c[i - 1];
      am[i] = Math.abs(m[i]);
    }
    const num = I.ema(I.ema(m, p.a), p.b);
    const den = I.ema(I.ema(am, p.a), p.b);
    const tsi = new Float64Array(n).fill(NaN);
    for (let i = 0; i < n; i++) if (den[i] > 0) tsi[i] = (100 * num[i]) / den[i];
    const sig = I.ema(
      tsi.map((x) => (fin(x) ? x : 0)),
      p.sig,
    );
    const cs = crossState(tsi, sig);
    return ev(n, (i) => {
      if (i < p.a * 3 || cs[i] === 0) return 0;
      return cs[i] > 0 && tsi[i] > 0 ? 1 : cs[i] < 0 && tsi[i] < 0 ? -1 : 0;
    });
  });

  // 7 Elder ray + force index pullback in a trend
  both("trend", "r-elder", "Elder ray pullback", { ema: 13, fi: 2 }, { ema: 26, fi: 5 }, (k, p) => {
    const { h, l, c, v, n } = k.b;
    const e = k.ema(p.ema);
    const force = new Float64Array(n);
    for (let i = 1; i < n; i++) force[i] = (c[i] - c[i - 1]) * v[i];
    const fi = I.ema(force, p.fi);
    return ev(n, (i) => {
      if (i < p.ema * 3 || !(fin(e[i]) && fin(e[i - 1]))) return 0;
      const bull = h[i] - e[i];
      const bear = l[i] - e[i];
      const bull1 = h[i - 1] - e[i - 1];
      const bear1 = l[i - 1] - e[i - 1];
      if (e[i] > e[i - 1] && bear < 0 && bear > bear1 && fi[i] < 0) return 1;
      if (e[i] < e[i - 1] && bull > 0 && bull < bull1 && fi[i] > 0) return -1;
      return 0;
    });
  });

  // 8 Awesome oscillator saucer (continuation)
  both("osc", "r-awesome", "AO saucer", { f: 5, s: 34 }, { f: 8, s: 55 }, (k, p) => {
    const { h, l, n } = k.b;
    const hl2 = new Float64Array(n);
    for (let i = 0; i < n; i++) hl2[i] = (h[i] + l[i]) / 2;
    const ao = new Float64Array(n).fill(NaN);
    const sf = I.sma(hl2, p.f);
    const ss = I.sma(hl2, p.s);
    for (let i = 0; i < n; i++) ao[i] = sf[i] - ss[i];
    return ev(n, (i) => {
      if (i < p.s + 3 || !(fin(ao[i]) && fin(ao[i - 2]))) return 0;
      if (ao[i] > 0 && ao[i - 2] > ao[i - 1] && ao[i - 1] < ao[i] && ao[i - 2] > 0) return 1;
      if (ao[i] < 0 && ao[i - 2] < ao[i - 1] && ao[i - 1] > ao[i] && ao[i - 2] < 0) return -1;
      return 0;
    });
  });

  // 9 linear-regression channel: trend entry on a pullback to the line, fade at ±2σ when the slope is flat
  both("channel", "r-linreg", "Regression channel", { n: 50, t: 2 }, { n: 100, t: 2.5 }, (k, p) => {
    const { c, n } = k.b;
    return ev(n, (i) => {
      const w = p.n;
      if (i < w + 5) return 0;
      let sx = 0;
      let sy = 0;
      let sxy = 0;
      let sxx = 0;
      for (let j = 0; j < w; j++) {
        const y = c[i - w + 1 + j];
        sx += j;
        sy += y;
        sxy += j * y;
        sxx += j * j;
      }
      const den = w * sxx - sx * sx;
      const b = (w * sxy - sx * sy) / den;
      const a = (sy - b * sx) / w;
      let rss = 0;
      for (let j = 0; j < w; j++) {
        const r = c[i - w + 1 + j] - (a + b * j);
        rss += r * r;
      }
      const s = Math.sqrt(rss / (w - 2));
      if (!(s > 0)) return 0;
      const fitted = a + b * (w - 1);
      const z = (c[i] - fitted) / s;
      const tstat = b / (s / Math.sqrt(sxx - (sx * sx) / w));
      if (Math.abs(tstat) > p.t && Math.abs(z) < 1)
        return tstat > 0 ? (z < -0.3 ? 1 : 0) : z > 0.3 ? -1 : 0;
      if (Math.abs(z) > 2 && Math.abs(tstat) < 1) return z > 0 ? -1 : 1;
      return 0;
    });
  });

  // 10 Williams fractal breakout with a trend gate
  both("break", "r-fractal", "Fractal breakout", { w: 2, ema: 50 }, { w: 3, ema: 100 }, (k, p) => {
    const { c, n } = k.b;
    const pv = pivots(k, p.w);
    const e = k.ema(p.ema);
    const er14 = er(k, 14);
    return ev(n, (i) => {
      if (pv.hi[i] < 0 || pv.lo[i] < 0 || !(fin(e[i]) && fin(er14[i])) || er14[i] < 0.25) return 0;
      const ph = k.b.h[pv.hi[i]];
      const pl = k.b.l[pv.lo[i]];
      if (c[i] > ph && c[i - 1] <= ph && c[i] > e[i]) return 1;
      if (c[i] < pl && c[i - 1] >= pl && c[i] < e[i]) return -1;
      return 0;
    });
  });

  // 11 Camarilla pivots from the previous UTC day: fade a rejection at R3 / S3, follow a close through R4 / S4
  both("channel", "r-camarilla", "Camarilla pivots", { rej: 0.5 }, { rej: 0.7 }, (k, p) => {
    const { t, h, l, c, o, n } = k.b;
    return ev(n, (i) => {
      const day = Math.floor(t[i] / DAY);
      // previous day's H / L / C (bars are in order; walk back to the previous day)
      let j = i;
      while (j >= 0 && Math.floor(t[j] / DAY) === day) j--;
      if (j < 0) return 0;
      const pd = Math.floor(t[j] / DAY);
      let ph = -Infinity;
      let pl = Infinity;
      const pc = c[j];
      for (let m = j; m >= 0 && Math.floor(t[m] / DAY) === pd; m--) {
        if (h[m] > ph) ph = h[m];
        if (l[m] < pl) pl = l[m];
      }
      const rng = ph - pl;
      if (!(rng > 0)) return 0;
      const r3 = pc + (1.1 * rng) / 4;
      const r4 = pc + (1.1 * rng) / 2;
      const s3 = pc - (1.1 * rng) / 4;
      const s4 = pc - (1.1 * rng) / 2;
      const body = Math.abs(c[i] - o[i]);
      const barRng = h[i] - l[i];
      if (barRng <= 0) return 0;
      if (h[i] > r3 && c[i] < r3 && (h[i] - Math.max(o[i], c[i])) / barRng > p.rej) return -1;
      if (l[i] < s3 && c[i] > s3 && (Math.min(o[i], c[i]) - l[i]) / barRng > p.rej) return 1;
      if (c[i] > r4 && c[i - 1] <= r4 && body > 0.5 * barRng) return 1;
      if (c[i] < s4 && c[i - 1] >= s4 && body > 0.5 * barRng) return -1;
      return 0;
    });
  });

  // 12 failed breakout (fakeout): a close beyond the 20-bar extreme within the last 3 bars, back inside now
  both(
    "move",
    "r-fakeout",
    "Failed breakout",
    { p: 20, back: 0.1 },
    { p: 40, back: 0.15 },
    (k, p) => {
      const { c, n } = k.b;
      const d = I.donchianPrior(k.b.h, k.b.l, p.p);
      const a = k.atr(14);
      return ev(n, (i) => {
        if (i < p.p + 4 || !fin(a[i])) return 0;
        for (let j = i - 3; j < i; j++) {
          if (!fin(d.hi[j])) continue;
          if (c[j] > d.hi[j] && c[i] < d.hi[j] - p.back * a[i]) return -1;
          if (c[j] < d.lo[j] && c[i] > d.lo[j] + p.back * a[i]) return 1;
        }
        return 0;
      });
    },
  );

  // 13 Wyckoff spring / upthrust: a pierce of a tight 40-bar range that reclaims within 2 bars
  both(
    "move",
    "r-spring",
    "Wyckoff spring",
    { p: 40, width: 4, pierce: 0.5 },
    { p: 60, width: 5, pierce: 0.7 },
    (k, p) => {
      const { l, h, c, n } = k.b;
      const d = I.donchianPrior(h, l, p.p);
      const a = k.atr(14);
      return ev(n, (i) => {
        if (i < p.p + 3 || !fin(a[i])) return 0;
        for (let j = i - 2; j <= i; j++) {
          if (!(fin(d.hi[j]) && fin(d.lo[j])) || d.hi[j] - d.lo[j] > p.width * a[i]) continue;
          if (l[j] < d.lo[j] && d.lo[j] - l[j] <= p.pierce * a[i] && c[i] > d.lo[j] && c[i] > c[j])
            return 1;
          if (h[j] > d.hi[j] && h[j] - d.hi[j] <= p.pierce * a[i] && c[i] < d.hi[j] && c[i] < c[j])
            return -1;
        }
        return 0;
      });
    },
  );

  // 14 value-area re-entry: a rolling 96-bar volume profile (bar volume spread over its range); after a close outside
  // the value area, two closes back inside fade toward the point of control
  both(
    "volume",
    "r-valuearea",
    "Value-area re-entry",
    { look: 96, va: 0.7 },
    { look: 192, va: 0.7 },
    (k, p) => {
      const { h, l, c, v, n } = k.b;
      const a = k.atr(14);
      return ev(n, (i) => {
        if (i < p.look + 3 || !fin(a[i]) || !(a[i] > 0)) return 0;
        const bin = 0.15 * a[i];
        let lo = Infinity;
        let hi = -Infinity;
        for (let j = i - p.look; j < i - 1; j++) {
          if (l[j] < lo) lo = l[j];
          if (h[j] > hi) hi = h[j];
        }
        const nb = Math.min(400, Math.ceil((hi - lo) / bin));
        if (nb < 5) return 0;
        const vol = new Float64Array(nb);
        for (let j = i - p.look; j < i - 1; j++) {
          const b0 = Math.max(0, Math.floor((l[j] - lo) / bin));
          const b1 = Math.min(nb - 1, Math.floor((h[j] - lo) / bin));
          const share = v[j] / (b1 - b0 + 1);
          for (let b = b0; b <= b1; b++) vol[b] += share;
        }
        let poc = 0;
        let total = 0;
        for (let b = 0; b < nb; b++) {
          total += vol[b];
          if (vol[b] > vol[poc]) poc = b;
        }
        let lb = poc;
        let ub = poc;
        let acc = vol[poc];
        while (acc < p.va * total && (lb > 0 || ub < nb - 1)) {
          const dn = lb > 0 ? vol[lb - 1] : -1;
          const up = ub < nb - 1 ? vol[ub + 1] : -1;
          if (up >= dn) acc += vol[++ub];
          else acc += vol[--lb];
        }
        const val = lo + lb * bin;
        const vah = lo + (ub + 1) * bin;
        if (c[i - 2] < val && c[i - 1] >= val && c[i] >= val && c[i] < vah) return 1;
        if (c[i - 2] > vah && c[i - 1] <= vah && c[i] <= vah && c[i] > val) return -1;
        return 0;
      });
    },
  );

  // 15 opening-range breakout at 00:00 and 13:30 UTC: the first 4 bars' range, a close beyond it within 8 hours
  both(
    "break",
    "r-orb",
    "Opening-range breakout",
    { bars: 4, minW: 0.5 },
    { bars: 8, minW: 0.7 },
    (k, p) => {
      const { t, h, l, c, n } = k.b;
      const a = k.atr(14);
      const tfMs = (k.b.tfMin || 15) * 60_000;
      let anchorT = -1;
      let rh = 0;
      let rl = 0;
      let fired = false;
      return ev(n, (i) => {
        const tod = t[i] % DAY;
        const isAnchor = tod === 0 || tod === 13.5 * H;
        if (isAnchor) {
          anchorT = t[i];
          rh = h[i];
          rl = l[i];
          fired = false;
          return 0;
        }
        if (anchorT < 0 || fired || !fin(a[i])) return 0;
        const since = t[i] - anchorT;
        if (since < p.bars * tfMs) {
          if (h[i] > rh) rh = h[i];
          if (l[i] < rl) rl = l[i];
          return 0;
        }
        if (since > 8 * H) return 0;
        const w = rh - rl;
        if (w < p.minW * a[i] || w > 3 * a[i]) return 0;
        if (c[i] > rh + 0.1 * a[i]) {
          fired = true;
          return 1;
        }
        if (c[i] < rl - 0.1 * a[i]) {
          fired = true;
          return -1;
        }
        return 0;
      });
    },
  );

  // 16 previous-day high / low break and retest
  both(
    "break",
    "r-pdhl",
    "Prev-day break and retest",
    { brk: 0.15, ret: 0.05 },
    { brk: 0.3, ret: 0.1 },
    (k, p) => {
      const { t, h, l, c, n } = k.b;
      const a = k.atr(14);
      let day = -1;
      let pdh = NaN;
      let pdl = NaN;
      let curH = -Infinity;
      let curL = Infinity;
      let brokeUp = -99;
      let brokeDn = -99;
      return ev(n, (i) => {
        const d = Math.floor(t[i] / DAY);
        if (d !== day) {
          if (day >= 0) {
            pdh = curH;
            pdl = curL;
          }
          day = d;
          curH = -Infinity;
          curL = Infinity;
          brokeUp = -99;
          brokeDn = -99;
        }
        if (h[i] > curH) curH = h[i];
        if (l[i] < curL) curL = l[i];
        if (!(fin(pdh) && fin(a[i]))) return 0;
        if (c[i] > pdh + p.brk * a[i] && brokeUp < 0) brokeUp = i;
        if (c[i] < pdl - p.brk * a[i] && brokeDn < 0) brokeDn = i;
        if (
          brokeUp >= 0 &&
          i > brokeUp &&
          i - brokeUp <= 8 &&
          l[i] <= pdh + p.ret * a[i] &&
          c[i] > pdh
        ) {
          brokeUp = -99;
          return 1;
        }
        if (
          brokeDn >= 0 &&
          i > brokeDn &&
          i - brokeDn <= 8 &&
          h[i] >= pdl - p.ret * a[i] &&
          c[i] < pdl
        ) {
          brokeDn = -99;
          return -1;
        }
        return 0;
      });
    },
  );

  // 17 break of structure in an established swing trend (continuation)
  both("break", "r-bos", "Break of structure", { w: 3, buf: 0.3 }, { w: 5, buf: 0.4 }, (k, p) => {
    const { h, l, c, n } = k.b;
    const pv = pivots(k, p.w);
    const a = k.atr(14);
    return ev(n, (i) => {
      const { hi, hi2, lo, lo2 } = { hi: pv.hi[i], hi2: pv.hi2[i], lo: pv.lo[i], lo2: pv.lo2[i] };
      if (hi < 0 || hi2 < 0 || lo < 0 || lo2 < 0 || !fin(a[i])) return 0;
      const up = h[hi] > h[hi2] && l[lo] > l[lo2];
      const dn = h[hi] < h[hi2] && l[lo] < l[lo2];
      if (up && c[i] > h[hi] + p.buf * a[i] && c[i - 1] <= h[hi] + p.buf * a[i]) return 1;
      if (dn && c[i] < l[lo] - p.buf * a[i] && c[i - 1] >= l[lo] - p.buf * a[i]) return -1;
      return 0;
    });
  });

  // 18 change of character: lower highs / lower lows broken above the last lower high (reversal)
  both(
    "break",
    "r-choch",
    "Change of character",
    { w: 3, buf: 0.2 },
    { w: 5, buf: 0.3 },
    (k, p) => {
      const { h, l, c, n } = k.b;
      const pv = pivots(k, p.w);
      const a = k.atr(14);
      return ev(n, (i) => {
        const hi = pv.hi[i];
        const hi2 = pv.hi2[i];
        const lo = pv.lo[i];
        const lo2 = pv.lo2[i];
        if (hi < 0 || hi2 < 0 || lo < 0 || lo2 < 0 || !fin(a[i])) return 0;
        const down = h[hi] < h[hi2] && l[lo] < l[lo2];
        const upT = h[hi] > h[hi2] && l[lo] > l[lo2];
        if (down && c[i] > h[hi] + p.buf * a[i] && c[i - 1] <= h[hi] + p.buf * a[i]) return 1;
        if (upT && c[i] < l[lo] - p.buf * a[i] && c[i - 1] >= l[lo] - p.buf * a[i]) return -1;
        return 0;
      });
    },
  );

  // 19 fair value gap displacement retest: a 3-bar gap after a displacement bar, price returns to the gap and holds
  both(
    "move",
    "r-fvg",
    "Fair value gap retest",
    { disp: 1.5, gap: 0.3, ttl: 12 },
    { disp: 2, gap: 0.5, ttl: 20 },
    (k, p) => {
      const { o, h, l, c, n } = k.b;
      const a = k.atr(14);
      return ev(n, (i) => {
        if (!fin(a[i]) || i < 30) return 0;
        for (let s = i - 1; s >= Math.max(2, i - p.ttl); s--) {
          // gap formed by bars s-2, s-1, s: bar s-1 is the displacement bar
          const atr = a[s];
          if (!fin(atr)) continue;
          const body = c[s - 1] - o[s - 1];
          if (body >= p.disp * atr && l[s] > h[s - 2] && l[s] - h[s - 2] >= p.gap * atr) {
            const top = l[s];
            const bot = h[s - 2];
            if (l[i] <= (top + bot) / 2 && c[i] > bot && c[i] > o[i]) return 1;
          }
          if (-body >= p.disp * atr && h[s] < l[s - 2] && l[s - 2] - h[s] >= p.gap * atr) {
            const top = l[s - 2];
            const bot = h[s];
            if (h[i] >= (top + bot) / 2 && c[i] < top && c[i] < o[i]) return -1;
          }
        }
        return 0;
      });
    },
  );

  // 20 inside-bar breakout with the slow trend
  both(
    "break",
    "r-inside",
    "Inside-bar breakout",
    { mother: 1, ema: 50 },
    { mother: 1.3, ema: 100 },
    (k, p) => {
      const { h, l, c, n } = k.b;
      const a = k.atr(14);
      const e = k.ema(p.ema);
      return ev(n, (i) => {
        if (i < p.ema + 3 || !(fin(a[i]) && fin(e[i]) && fin(e[i - 3]))) return 0;
        // bar i-1 is inside bar i-2 (the mother); bar i closes beyond the mother's range
        const hm = h[i - 2];
        const lm = l[i - 2];
        if (hm - lm < p.mother * a[i] || !(h[i - 1] <= hm && l[i - 1] >= lm)) return 0;
        if (c[i] > hm && e[i] > e[i - 3]) return 1;
        if (c[i] < lm && e[i] < e[i - 3]) return -1;
        return 0;
      });
    },
  );

  // 21 engulfing at a swing extreme on a volume spike
  both(
    "move",
    "r-engulf",
    "Engulfing at extreme",
    { mult: 1.5, atr: 0.8, look: 10 },
    { mult: 2, atr: 1, look: 20 },
    (k, p) => {
      const { o, h, l, c, n } = k.b;
      const a = k.atr(14);
      const z = vz20(k);
      const lo = rmin(l, p.look);
      const hi = rmax(h, p.look);
      return ev(n, (i) => {
        if (i < p.look + 2 || !(fin(a[i]) && fin(z[i])) || z[i] < 1.5) return 0;
        const b0 = Math.abs(c[i] - o[i]);
        const b1 = Math.abs(c[i - 1] - o[i - 1]);
        if (b0 < p.atr * a[i] || b0 < p.mult * b1) return 0;
        if (
          c[i - 1] < o[i - 1] &&
          c[i] > o[i] &&
          o[i] <= c[i - 1] &&
          c[i] >= o[i - 1] &&
          l[i] <= lo[i - 1]
        )
          return 1;
        if (
          c[i - 1] > o[i - 1] &&
          c[i] < o[i] &&
          o[i] >= c[i - 1] &&
          c[i] <= o[i - 1] &&
          h[i] >= hi[i - 1]
        )
          return -1;
        return 0;
      });
    },
  );

  // 22 pin bar / hammer at a 20-bar extreme
  both(
    "move",
    "r-pin",
    "Pin bar at extreme",
    { wick: 2, rng: 1.2 },
    { wick: 2.5, rng: 1.5 },
    (k, p) => {
      const { o, h, l, c, n } = k.b;
      const a = k.atr(14);
      const lo = rmin(l, 20);
      const hi = rmax(h, 20);
      return ev(n, (i) => {
        if (i < 22 || !fin(a[i])) return 0;
        const rng = h[i] - l[i];
        const body = Math.abs(c[i] - o[i]);
        if (rng < p.rng * a[i] || rng <= 0) return 0;
        const lw = Math.min(o[i], c[i]) - l[i];
        const uw = h[i] - Math.max(o[i], c[i]);
        if (
          lw >= p.wick * Math.max(body, 0.05 * rng) &&
          lw >= 0.6 * rng &&
          (c[i] - l[i]) / rng > 0.7 &&
          l[i] <= lo[i - 1]
        )
          return 1;
        if (
          uw >= p.wick * Math.max(body, 0.05 * rng) &&
          uw >= 0.6 * rng &&
          (h[i] - c[i]) / rng > 0.7 &&
          h[i] >= hi[i - 1]
        )
          return -1;
        return 0;
      });
    },
  );

  // 23 morning / evening star
  both(
    "move",
    "r-star",
    "Morning / evening star",
    { small: 0.3, big: 0.8 },
    { small: 0.25, big: 1 },
    (k, p) => {
      const { o, h, l, c, n } = k.b;
      const a = k.atr(14);
      return ev(n, (i) => {
        if (i < 20 || !fin(a[i])) return 0;
        const b2 = c[i - 2] - o[i - 2];
        const b1 = Math.abs(c[i - 1] - o[i - 1]);
        if (b1 > p.small * a[i]) return 0;
        if (
          b2 < -p.big * a[i] &&
          l[i - 1] <= l[i - 2] &&
          c[i] > o[i] &&
          c[i] > (o[i - 2] + c[i - 2]) / 2
        )
          return 1;
        if (
          b2 > p.big * a[i] &&
          h[i - 1] >= h[i - 2] &&
          c[i] < o[i] &&
          c[i] < (o[i - 2] + c[i - 2]) / 2
        )
          return -1;
        return 0;
      });
    },
  );

  // 24 exhaustion streak: N same-direction closes with a large total move and a volume spike, fade the first reversal
  both("move", "r-streak", "Exhaustion streak", { n: 5, mul: 2.5 }, { n: 6, mul: 3 }, (k, p) => {
    const { o, c, n } = k.b;
    const a = k.atr(14);
    const z = vz20(k);
    return ev(n, (i) => {
      if (i < p.n + 2 || !(fin(a[i - 1]) && fin(z[i - 1]))) return 0;
      let dn = true;
      let up = true;
      for (let j = i - p.n; j < i; j++) {
        if (!(c[j] < c[j - 1])) dn = false;
        if (!(c[j] > c[j - 1])) up = false;
      }
      const move = Math.abs(c[i - 1] - c[i - 1 - p.n]);
      if (move < p.mul * a[i - 1] || (z[i - 1] < 1.5 && z[i - 2] < 1.5)) return 0;
      if (dn && c[i] > o[i]) return 1;
      if (up && c[i] < o[i]) return -1;
      return 0;
    });
  });

  return out;
}

export const RESEARCH2_SOURCES: ReadonlyArray<{ name: string; label: string }> = [
  { name: "r-roofing", label: "Roofing filter cross" },
  { name: "r-fisher", label: "Fisher turn" },
  { name: "r-connors", label: "Connors RSI fade" },
  { name: "r-stc", label: "Schaff trend cycle" },
  { name: "r-vortex", label: "Vortex cross" },
  { name: "r-tsi", label: "TSI cross" },
  { name: "r-elder", label: "Elder ray pullback" },
  { name: "r-awesome", label: "AO saucer" },
  { name: "r-linreg", label: "Regression channel" },
  { name: "r-fractal", label: "Fractal breakout" },
  { name: "r-camarilla", label: "Camarilla pivots" },
  { name: "r-fakeout", label: "Failed breakout" },
  { name: "r-spring", label: "Wyckoff spring" },
  { name: "r-valuearea", label: "Value-area re-entry" },
  { name: "r-orb", label: "Opening-range breakout" },
  { name: "r-pdhl", label: "Prev-day break and retest" },
  { name: "r-bos", label: "Break of structure" },
  { name: "r-choch", label: "Change of character" },
  { name: "r-fvg", label: "Fair value gap retest" },
  { name: "r-inside", label: "Inside-bar breakout" },
  { name: "r-engulf", label: "Engulfing at extreme" },
  { name: "r-pin", label: "Pin bar at extreme" },
  { name: "r-star", label: "Morning / evening star" },
  { name: "r-streak", label: "Exhaustion streak" },
];
