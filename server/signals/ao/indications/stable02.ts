// Stable-02 ports: the entry signals (signalFor) and the indications (INDICATION_CONFIGS / processIndication) of
// the old desk engine (CTS-A branch Stable-02, src/lib/desk/engine.ts), with the same logic and thresholds, as
// registry indications ("s2-…"). Entry signals keep the desk's event semantics: the state is non-zero exactly on
// the bars the desk's signal fired (the "follow" bot enters on its onsets), except Supertrend, whose flips are the
// onsets of its direction state. Each entry comes in the desk's own parameters (short, "s2-<id>") and a slower
// documented alternate (medium, "s2-<id>-m").
//
// Indicator math: where the desk's math equals the registry's the shared SeriesCache series are reused (EMA, SMA,
// RSI, Bollinger with population σ, Stochastic, CCI, MACD — its signal-line seed differs only in warm-up, volume
// SMA, prior-bar Donchian). The desk's ATR is an EMA of the true range (registry: Wilder), its DMI smooths with EMAs
// and its Supertrend runs on that ATR(14) — those are ported as-is below. The desk's VWAP was cumulative over its
// 240-bar series; here it is a rolling VWAP over 240 bars (a cumulative VWAP over months of history would drift
// with the history's start).
import type { SeriesCache } from "./cache.ts";
import type { IndicationSpec } from "./registry.ts";
import type { IndicationKind } from "../domain/types.ts";
import * as I from "../math/indicators.ts";

type F64 = Float64Array;

/** Stable-02 WARMUP: no entry signal before this bar. */
export const S2_WARMUP = 55;
/** Stable-02 series length (BARS): the window of its session VWAP. */
export const S2_VWAP = 240;

const fin = (x: number) => Number.isFinite(x);

// ── desk math (only what differs from the registry's) ─────────────────────────────────────────────────────

/** Stable-02 DMI: true range and ±DM smoothed with EMAs, ADX = EMA of DX (non-finite DX → 0). */
export function s2Dmi(k: SeriesCache, p = 14): { adx: F64; pdi: F64; mdi: F64 } {
  return k.memo(`s2dmi${p}`, () => {
    const { h, l, c, n } = k.b;
    const pdm = new Float64Array(n);
    const mdm = new Float64Array(n);
    for (let i = 1; i < n; i++) {
      const up = h[i] - h[i - 1];
      const dn = l[i - 1] - l[i];
      pdm[i] = up > dn && up > 0 ? up : 0;
      mdm[i] = dn > up && dn > 0 ? dn : 0;
    }
    const str = I.ema(I.trueRange(h, l, c), p);
    const sp = I.ema(pdm, p);
    const sm = I.ema(mdm, p);
    const pdi = new Float64Array(n).fill(NaN);
    const mdi = new Float64Array(n).fill(NaN);
    const dx = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      if (str[i]) {
        pdi[i] = (100 * sp[i]) / str[i];
        mdi[i] = (100 * sm[i]) / str[i];
      }
      const s = pdi[i] + mdi[i];
      const x = s === 0 || !fin(s) ? NaN : (100 * Math.abs(pdi[i] - mdi[i])) / s;
      dx[i] = fin(x) ? x : 0;
    }
    return { adx: I.ema(dx, p), pdi, mdi };
  });
}

/** Stable-02 Supertrend: on the desk's ATR (EMA of the true range, `atrP` bars) × mult; direction ±1 (0 = warm-up). */
export function s2Supertrend(k: SeriesCache, atrP = 14, mult = 3): Int8Array {
  return k.memo(`s2st${atrP}.${mult}`, () => {
    const { h, l, c, n } = k.b;
    const a = k.atrEma(atrP);
    const dir = new Int8Array(n);
    let upper = 0;
    let lower = 0;
    let d = 1;
    let prevOk = false;
    for (let i = 0; i < n; i++) {
      if (!fin(a[i])) {
        prevOk = false;
        continue;
      }
      const hl2 = (h[i] + l[i]) / 2;
      const bu = hl2 + mult * a[i];
      const bl = hl2 - mult * a[i];
      if (i === 0 || !prevOk) {
        upper = bu;
        lower = bl;
        d = c[i] >= hl2 ? 1 : -1;
      } else {
        lower = bl > lower || c[i - 1] < lower ? bl : lower;
        upper = bu < upper || c[i - 1] > upper ? bu : upper;
        if (d === 1 && c[i] < lower) d = -1;
        else if (d === -1 && c[i] > upper) d = 1;
      }
      dir[i] = d;
      prevOk = true;
    }
    return dir;
  });
}

/** Stable-02 range change: (bar range + |close change|) ÷ ATR(14) (0 without ATR). */
export function s2RangeChange(k: SeriesCache): F64 {
  return k.memo("s2rc", () => {
    const { h, l, c, n } = k.b;
    const a = k.atrEma(14);
    const out = new Float64Array(n);
    for (let i = 1; i < n; i++) {
      const ai = a[i] || 0;
      out[i] = ai > 0 ? (h[i] - l[i] + Math.abs(c[i] - c[i - 1])) / ai : 0;
    }
    return out;
  });
}

/** Stable-02 activity: volume pulse (bar and 2-bar vs volume SMA 20) × range change, capped at 4 (NaN in warm-up). */
export function s2Activity(k: SeriesCache): F64 {
  return k.memo("s2act", () => {
    const { v, n } = k.b;
    const rc = s2RangeChange(k);
    const vs = k.volSma(20);
    const out = new Float64Array(n);
    for (let i = 1; i < n; i++) {
      const s = vs[i] || 0;
      const volX = s > 0 ? v[i] / s : 1;
      const fast = i >= 2 ? (v[i] + v[i - 1]) / Math.max(vs[i] * 2, 1e-9) : volX;
      out[i] = Math.min(4, (volX * 0.55 + fast * 0.45) * (0.4 + rc[i] * 0.6));
    }
    return out;
  });
}

const crossUp = (a: F64, b: F64, i: number) =>
  i > 0 &&
  fin(a[i]) &&
  fin(b[i]) &&
  fin(a[i - 1]) &&
  fin(b[i - 1]) &&
  a[i - 1] <= b[i - 1] &&
  a[i] > b[i];
const crossDn = (a: F64, b: F64, i: number) =>
  i > 0 &&
  fin(a[i]) &&
  fin(b[i]) &&
  fin(a[i - 1]) &&
  fin(b[i - 1]) &&
  a[i - 1] >= b[i - 1] &&
  a[i] < b[i];

/** Desk signal loop: f(i) ∈ {−1, 0, 1} from the warm-up bar on. */
function events(n: number, f: (i: number) => number): Int8Array {
  const out = new Int8Array(n);
  for (let i = S2_WARMUP; i < n; i++) out[i] = f(i) as -1 | 0 | 1;
  return out;
}

/** Extend events into a state that persists `keep` bars (latest event wins). */
function hold(ev: Int8Array, keep: number): Int8Array {
  const out = new Int8Array(ev.length);
  let cur = 0;
  let left = 0;
  for (let i = 0; i < ev.length; i++) {
    if (ev[i] !== 0) {
      cur = ev[i];
      left = keep;
    }
    if (left > 0) {
      out[i] = cur;
      left--;
    }
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

// ── entry signals (signalFor) ─────────────────────────────────────────────────────────────────────────────

/** "ema-cross": EMA fast/slow cross gated by ADX > 18 and volume > 1.05 × SMA 20. */
function emaCross(f: number, s: number) {
  return (k: SeriesCache) => {
    const a = k.ema(f),
      b = k.ema(s),
      vs = k.volSma(20),
      { adx } = s2Dmi(k, 14),
      v = k.b.v;
    return events(k.b.n, (i) => {
      const volOk = fin(vs[i]) && v[i] > vs[i] * 1.05;
      const adxOk = fin(adx[i]) && adx[i] > 18;
      if (crossUp(a, b, i) && adxOk && volOk) return 1;
      if (crossDn(a, b, i) && adxOk && volOk) return -1;
      return 0;
    });
  };
}

/** "rsi-revert": RSI < 32 at / below the lower band (long), > 68 at / above the upper band (short). */
function rsiRevert(rp: number, bp: number) {
  return (k: SeriesCache) => {
    const r = k.rsi(rp),
      { up, lo } = k.bb(bp, 2),
      c = k.b.c;
    return events(k.b.n, (i) =>
      fin(r[i]) && fin(lo[i]) && r[i] < 32 && c[i] <= lo[i]
        ? 1
        : fin(r[i]) && fin(up[i]) && r[i] > 68 && c[i] >= up[i]
          ? -1
          : 0,
    );
  };
}

/** "bb-bounce": close back inside the band after a close outside it, Stochastic %K < 35 / > 65. */
function bbBounce(bp: number, sp: number) {
  return (k: SeriesCache) => {
    const { up, lo } = k.bb(bp, 2),
      { k: sk } = k.stoch(sp, 3),
      c = k.b.c;
    return events(k.b.n, (i) =>
      fin(lo[i]) && c[i - 1] < lo[i - 1] && c[i] > lo[i] && sk[i] < 35
        ? 1
        : fin(up[i]) && c[i - 1] > up[i - 1] && c[i] < up[i] && sk[i] > 65
          ? -1
          : 0,
    );
  };
}

/** "vwap-axis": close crosses the VWAP with volume above its SMA 20, on the EMA's side. */
function vwapAxis(wp: number, ep: number) {
  return (k: SeriesCache) => {
    const w = k.vwap(wp),
      e = k.ema(ep),
      vs = k.volSma(20),
      { c, v } = k.b;
    return events(k.b.n, (i) => {
      const volOk = fin(vs[i]) && v[i] > vs[i];
      const ei = fin(e[i]) ? e[i] : c[i];
      if (crossUp(c, w, i) && volOk && c[i] > ei) return 1;
      if (crossDn(c, w, i) && volOk && c[i] < ei) return -1;
      return 0;
    });
  };
}

/** "vol-break": volume > 1.8 × its SMA, close beyond the SMA of closes in the bar's direction. */
function volBreak(p: number) {
  return (k: SeriesCache) => {
    const vs = k.volSma(p),
      m = k.sma(p),
      { o, c, v } = k.b;
    return events(k.b.n, (i) => {
      const volOk = fin(vs[i]) && v[i] > vs[i] * 1.8;
      if (volOk && fin(m[i]) && c[i] > m[i] && c[i] > o[i]) return 1;
      if (volOk && fin(m[i]) && c[i] < m[i] && c[i] < o[i]) return -1;
      return 0;
    });
  };
}

/** "adx-gate": ADX > 22 with +DI / −DI in the direction of the EMA slope (every bar the gate holds). */
function adxGate(dp: number, ep: number) {
  return (k: SeriesCache) => {
    const { adx, pdi, mdi } = s2Dmi(k, dp),
      e = k.ema(ep);
    return events(k.b.n, (i) => {
      if (!(fin(adx[i]) && adx[i] > 22 && fin(pdi[i]) && fin(mdi[i]) && fin(e[i]) && fin(e[i - 1])))
        return 0;
      const slope = e[i] - e[i - 1];
      if (pdi[i] > mdi[i] && slope > 0) return 1;
      if (mdi[i] > pdi[i] && slope < 0) return -1;
      return 0;
    });
  };
}

/** "stoch-swing": %K crosses %D below 28 with CCI < −50 (long), above 72 with CCI > 50 (short). */
function stochSwing(sp: number, dp: number, cp: number) {
  return (k: SeriesCache) => {
    const { k: sk, d } = k.stoch(sp, dp),
      x = k.cci(cp);
    return events(k.b.n, (i) =>
      crossUp(sk, d, i) && sk[i] < 28 && x[i] < -50
        ? 1
        : crossDn(sk, d, i) && sk[i] > 72 && x[i] > 50
          ? -1
          : 0,
    );
  };
}

/** "confluence": three of five independent confirms (EMA stack, MACD, Supertrend, RSI band, volume bar). */
function confluence(
  f: number,
  s: number,
  m: readonly [number, number, number],
  st: readonly [number, number],
  rp: number,
  vp: number,
) {
  return (k: SeriesCache) => {
    const a = k.ema(f),
      b = k.ema(s),
      { line, signal } = k.macd(m[0], m[1], m[2]),
      dir = s2Supertrend(k, st[0], st[1]),
      r = k.rsi(rp),
      vs = k.volSma(vp),
      { o, c, v } = k.b;
    return events(k.b.n, (i) => {
      let long = 0;
      let short = 0;
      if (fin(a[i]) && fin(b[i]) && a[i] > b[i]) long++;
      else if (fin(a[i]) && fin(b[i]) && a[i] < b[i]) short++;
      if (fin(line[i]) && fin(signal[i]) && line[i] > signal[i]) long++;
      else if (fin(line[i]) && fin(signal[i])) short++;
      if (dir[i] === 1) long++;
      else if (dir[i] === -1) short++;
      if (fin(r[i]) && r[i] > 45 && r[i] < 70) long++;
      else if (fin(r[i]) && r[i] < 55 && r[i] > 30) short++;
      if (fin(vs[i]) && v[i] > vs[i]) {
        if (c[i] > o[i]) long++;
        else short++;
      }
      return long >= 3 && long > short ? 1 : short >= 3 && short > long ? -1 : 0;
    });
  };
}

/** "range-break": close beyond the prior `look`-bar high / low on volume > 1.15 × SMA and a bar > 1.1 × ATR. */
function rangeBreak(look: number) {
  return (k: SeriesCache) => {
    const { hi, lo } = k.don(look),
      vs = k.volSma(20),
      a = k.atrEma(14),
      { h, l, c, v } = k.b;
    return events(k.b.n, (i) => {
      if (i < look) return 0;
      const volOk = fin(vs[i]) && v[i] > vs[i] * 1.15;
      const expand = fin(a[i]) && h[i] - l[i] > a[i] * 1.1;
      if (volOk && expand && c[i] > hi[i]) return 1;
      if (volOk && expand && c[i] < lo[i]) return -1;
      return 0;
    });
  };
}

/** "atr-break": close beyond EMA ± band × ATR while the range change is > 1.15. */
function atrBreak(ep: number, band: number) {
  return (k: SeriesCache) => {
    const a = k.atrEma(14),
      e = k.ema(ep),
      rc = s2RangeChange(k),
      c = k.b.c;
    return events(k.b.n, (i) => {
      if (!fin(a[i]) || !fin(e[i])) return 0;
      const b = a[i] * band;
      const expand = rc[i] > 1.15;
      if (expand && c[i] > e[i] + b) return 1;
      if (expand && c[i] < e[i] - b) return -1;
      return 0;
    });
  };
}

/** "active-hf": activity ≥ 1.2 and an EMA cross, or a directional bar with activity > 1.6. */
function activeHf(f: number, s: number) {
  return (k: SeriesCache) => {
    const act = s2Activity(k),
      a = k.ema(f),
      b = k.ema(s),
      { o, c } = k.b;
    return events(k.b.n, (i) => {
      const x = act[i];
      if (x < 1.2) return 0; // as the desk: a warm-up NaN does not stop the EMA cross
      if (crossUp(a, b, i) || (c[i] > o[i] && x > 1.6)) return 1;
      if (crossDn(a, b, i) || (c[i] < o[i] && x > 1.6)) return -1;
      return 0;
    });
  };
}

/** "range-shift": range change and activity ≥ 1.05 with a directional close beyond the band. */
function rangeShift(bp: number) {
  return (k: SeriesCache) => {
    const rc = s2RangeChange(k),
      act = s2Activity(k),
      { up, lo } = k.bb(bp, 2),
      { o, c } = k.b;
    return events(k.b.n, (i) => {
      if (rc[i] < 1.05 || act[i] < 1.05) return 0;
      if (fin(up[i]) && c[i] > up[i] && c[i] > o[i]) return 1;
      if (fin(lo[i]) && c[i] < lo[i] && c[i] < o[i]) return -1;
      return 0;
    });
  };
}

/**
 * "block-stack": ≥ need of the last `look` bars in one direction, volume > 1.05 × SMA, close on the EMA's side. A doji
 * counts on neither side: the desk's c ≥ o counted it up, a long bias on every market and on its mirror.
 */
function blockStack(look: number, need: number, ep: number) {
  return (k: SeriesCache) => {
    const e = k.ema(ep),
      vs = k.volSma(20),
      { o, c, v } = k.b;
    return events(k.b.n, (i) => {
      if (i < look) return 0;
      let up = 0;
      let dn = 0;
      for (let j = i - look + 1; j <= i; j++) {
        if (c[j] > o[j]) up++;
        else if (c[j] < o[j]) dn++;
      }
      const volOk = fin(vs[i]) && v[i] > vs[i] * 1.05;
      const ei = fin(e[i]) ? e[i] : c[i];
      if (volOk && up >= need && c[i] > ei) return 1;
      if (volOk && dn >= need && c[i] < ei) return -1;
      return 0;
    });
  };
}

/** "block-scale": ADX ≥ 16 and volume above its SMA, on the VWAP's side (every bar it holds). */
function blockScale(dp: number, wp: number) {
  return (k: SeriesCache) => {
    const { adx } = s2Dmi(k, dp),
      vs = k.volSma(20),
      w = k.vwap(wp),
      { c, v } = k.b;
    return events(k.b.n, (i) => {
      const adxOk = fin(adx[i]) && adx[i] >= 16;
      const volOk = fin(vs[i]) && v[i] > vs[i];
      if (adxOk && volOk && fin(w[i]) && c[i] > w[i]) return 1;
      if (adxOk && volOk && fin(w[i]) && c[i] < w[i]) return -1;
      return 0;
    });
  };
}

/**
 * Ported entry signals: desk id → [kind, label, short params, short fn, medium params, medium fn]. "normal" (EMA
 * 9/21 cross = registry "ema-9-21") and "macd-mom" (MACD signal cross; the histogram sign always agrees with the
 * cross = registry "macd-cross") already exist and are not ported.
 */
export const S2_ENTRIES: ReadonlyArray<{
  id: string;
  kind: IndicationKind;
  label: string;
  short: [Record<string, number>, (k: SeriesCache) => Int8Array];
  medium: [Record<string, number>, (k: SeriesCache) => Int8Array];
}> = [
  {
    id: "ema-cross",
    kind: "trend",
    label: "EMA cross pulse",
    short: [{ f: 9, s: 21, adx: 18, vol: 1.05 }, emaCross(9, 21)],
    medium: [{ f: 21, s: 55, adx: 18, vol: 1.05 }, emaCross(21, 55)],
  },
  {
    id: "rsi-revert",
    kind: "rsi",
    label: "RSI mean revert",
    short: [{ rsi: 14, lo: 32, hi: 68, bb: 20 }, rsiRevert(14, 20)],
    medium: [{ rsi: 21, lo: 32, hi: 68, bb: 30 }, rsiRevert(21, 30)],
  },
  {
    id: "st-trail",
    kind: "trend",
    label: "Supertrend trail",
    // the desk passed period 10 but ran on its ATR(14)
    short: [{ atr: 14, m: 3 }, (k) => s2Supertrend(k, 14, 3)],
    medium: [{ atr: 21, m: 4 }, (k) => s2Supertrend(k, 21, 4)],
  },
  {
    id: "bb-bounce",
    kind: "bollinger",
    label: "Bollinger bounce",
    short: [{ bb: 20, k: 2, stoch: 14, lo: 35, hi: 65 }, bbBounce(20, 14)],
    medium: [{ bb: 30, k: 2, stoch: 21, lo: 35, hi: 65 }, bbBounce(30, 21)],
  },
  {
    id: "vwap-axis",
    kind: "direction",
    label: "VWAP axis",
    short: [{ vwap: S2_VWAP, ema: 21, vol: 20 }, vwapAxis(S2_VWAP, 21)],
    medium: [{ vwap: 2 * S2_VWAP, ema: 55, vol: 20 }, vwapAxis(2 * S2_VWAP, 55)],
  },
  {
    id: "vol-break",
    kind: "volume",
    label: "Volume breakout",
    short: [{ p: 20, vol: 1.8 }, volBreak(20)],
    medium: [{ p: 50, vol: 1.8 }, volBreak(50)],
  },
  {
    id: "adx-gate",
    kind: "trend",
    label: "ADX trend gate",
    short: [{ dmi: 14, adx: 22, ema: 55 }, adxGate(14, 55)],
    medium: [{ dmi: 21, adx: 22, ema: 100 }, adxGate(21, 100)],
  },
  {
    id: "stoch-swing",
    kind: "osc",
    label: "Stoch swing",
    short: [{ k: 14, d: 3, lo: 28, hi: 72, cci: 20 }, stochSwing(14, 3, 20)],
    medium: [{ k: 21, d: 5, lo: 28, hi: 72, cci: 40 }, stochSwing(21, 5, 40)],
  },
  {
    id: "confluence",
    kind: "trend",
    label: "Confluence 3 of 5",
    short: [
      { f: 9, s: 21, st: 14, rsi: 14, vol: 20 },
      confluence(9, 21, [12, 26, 9], [14, 3], 14, 20),
    ],
    medium: [
      { f: 21, s: 55, st: 21, rsi: 21, vol: 50 },
      confluence(21, 55, [19, 39, 9], [21, 4], 21, 50),
    ],
  },
  {
    id: "range-break",
    kind: "break",
    label: "Range break",
    short: [{ look: 20, vol: 1.15, atr: 1.1 }, rangeBreak(20)],
    medium: [{ look: 40, vol: 1.15, atr: 1.1 }, rangeBreak(40)],
  },
  {
    id: "atr-break",
    kind: "break",
    label: "ATR break",
    short: [{ ema: 21, band: 1.5, rc: 1.15 }, atrBreak(21, 1.5)],
    medium: [{ ema: 55, band: 2, rc: 1.15 }, atrBreak(55, 2)],
  },
  {
    id: "active-hf",
    kind: "active",
    label: "Active high-freq",
    short: [{ act: 1.2, burst: 1.6, f: 9, s: 21 }, activeHf(9, 21)],
    medium: [{ act: 1.2, burst: 1.6, f: 21, s: 55 }, activeHf(21, 55)],
  },
  {
    id: "range-shift",
    kind: "active",
    label: "Range shift",
    short: [{ rc: 1.05, act: 1.05, bb: 20 }, rangeShift(20)],
    medium: [{ rc: 1.05, act: 1.05, bb: 50 }, rangeShift(50)],
  },
  {
    id: "block-stack",
    kind: "move",
    label: "Block stack",
    short: [{ look: 4, need: 3, vol: 1.05, ema: 9 }, blockStack(4, 3, 9)],
    medium: [{ look: 8, need: 6, vol: 1.05, ema: 21 }, blockStack(8, 6, 21)],
  },
  {
    id: "block-scale",
    kind: "direction",
    label: "Block scale",
    short: [{ dmi: 14, adx: 16, vwap: S2_VWAP }, blockScale(14, S2_VWAP)],
    medium: [{ dmi: 21, adx: 16, vwap: 2 * S2_VWAP }, blockScale(21, 2 * S2_VWAP)],
  },
];

/** Registry id of a ported entry in a range ("s2-ema-cross", "s2-ema-cross-m"). */
export const s2EntryId = (id: string, range: "short" | "medium") =>
  `s2-${id}${range === "medium" ? "-m" : ""}`;

// ── indications (INDICATION_CONFIGS × processIndication): the direction of each hit as the state ──────────────

/** kind "break" (break-vol / break-atr): volume > volMult × SMA or range change ≥ 0.7 × atrMult → bar direction. */
function breakExpand(volMult: number, atrMult: number) {
  return (k: SeriesCache) => {
    const vs = k.volSma(20),
      rc = s2RangeChange(k),
      { o, c, v } = k.b;
    const out = new Int8Array(k.b.n);
    for (let i = 0; i < k.b.n; i++) {
      const volOk = fin(vs[i]) && v[i] > vs[i] * volMult;
      const expand = rc[i] >= atrMult * 0.7;
      if (volOk || expand) out[i] = c[i] >= o[i] ? 1 : -1;
    }
    return out;
  };
}

/** kind "active": mean activity of the last `look` bars ≥ 1.05 with volume or ranging → bar direction. */
function activeLook(look: number, volMult: number) {
  return (k: SeriesCache) => {
    const act = s2Activity(k),
      rc = s2RangeChange(k),
      vs = k.volSma(20),
      { o, c, v, n } = k.b;
    const out = new Int8Array(n);
    for (let i = 0; i < n; i++) {
      const from = Math.max(1, i - look + 1);
      let a = 0;
      for (let j = from; j <= i; j++) a += act[j];
      a /= Math.max(1, i - from + 1);
      const volOk = fin(vs[i]) && v[i] > vs[i] * volMult;
      if (a >= 1.05 && (volOk || rc[i] > 0.85)) out[i] = c[i] >= o[i] ? 1 : -1;
    }
    return out;
  };
}

/** kind "direction": the latest sign change of `sgn` within the last `look` bars. */
function dirFlip(look: number, sgn: (k: SeriesCache) => (i: number) => number) {
  return (k: SeriesCache) => {
    const s = sgn(k);
    const n = k.b.n;
    const ev = new Int8Array(n);
    let was = n > 0 ? s(0) : 0;
    for (let i = 1; i < n; i++) {
      const now = s(i);
      if (now !== 0 && now !== was) ev[i] = now as -1 | 1;
      was = now;
    }
    return hold(ev, look);
  };
}
const signOf = (x: number) => (fin(x) ? Math.sign(x) : 0);

/**
 * Ported indications. "trend-ema" (EMA 9 vs 21 = registry "ema-9-21") exists already; "trend-st" is the state of
 * the ported "s2-st-trail" (desk Supertrend direction) — neither is added twice.
 */
export const S2_INDICATIONS: readonly IndicationSpec[] = [
  spec("trend", "s2-trend-adx", "S2 trend ADX 26 (EMA DMI)", { p: 14, adx: 26 }, (k) => {
    const { adx, pdi, mdi } = s2Dmi(k, 14);
    const out = new Int8Array(k.b.n);
    for (let i = 0; i < k.b.n; i++)
      if (fin(adx[i]) && adx[i] >= 26 && fin(pdi[i]) && fin(mdi[i]))
        out[i] = pdi[i] > mdi[i] ? 1 : -1;
    return out;
  }),
  spec(
    "break",
    "s2-break-vol",
    "S2 break volume 1.6×",
    { vol: 1.6, atr: 1.3 },
    breakExpand(1.6, 1.3),
  ),
  spec(
    "break",
    "s2-break-atr",
    "S2 break ATR 1.15×",
    { vol: 1.5, atr: 1.15 },
    breakExpand(1.5, 1.15),
  ),
  spec("break", "s2-break-hi", "S2 break 8-bar range", { look: 8 }, (k) => {
    const { hi, lo } = k.don(8),
      c = k.b.c;
    const out = new Int8Array(k.b.n);
    for (let i = 8; i < k.b.n; i++) out[i] = c[i] > hi[i] ? 1 : c[i] < lo[i] ? -1 : 0;
    return out;
  }),
  spec("active", "s2-act-hf", "S2 active high-freq", { look: 4, vol: 1.15 }, activeLook(4, 1.15)),
  spec(
    "active",
    "s2-act-range",
    "S2 active range shift",
    { look: 6, vol: 1.05 },
    activeLook(6, 1.05),
  ),
  spec("active", "s2-act-burst", "S2 active burst", { look: 3, vol: 1.5 }, activeLook(3, 1.5)),
  spec(
    "direction",
    "s2-dir-cross",
    "S2 dir EMA cross",
    { look: 5 },
    dirFlip(5, (k) => {
      const a = k.ema(9),
        b = k.ema(21);
      return (i) => (fin(a[i]) && fin(b[i]) ? Math.sign(a[i] - b[i]) : 0);
    }),
  ),
  spec(
    "direction",
    "s2-dir-st",
    "S2 dir Supertrend flip",
    { look: 6 },
    dirFlip(6, (k) => {
      const d = s2Supertrend(k, 14, 3);
      return (i) => d[i];
    }),
  ),
  spec(
    "direction",
    "s2-dir-axis",
    "S2 dir axis VWAP",
    { look: 5, vwap: S2_VWAP },
    dirFlip(5, (k) => {
      const w = k.vwap(S2_VWAP),
        c = k.b.c;
      return (i) => (fin(w[i]) ? Math.sign(c[i] - w[i]) : 0);
    }),
  ),
  spec(
    "direction",
    "s2-dir-macd",
    "S2 dir MACD flip",
    { look: 5 },
    dirFlip(5, (k) => {
      const { hist } = k.macd();
      return (i) => signOf(hist[i]);
    }),
  ),
];

/** Every Stable-02 registry indication: the entries in both ranges, then the indications. */
export function stable02Specs(): IndicationSpec[] {
  const out: IndicationSpec[] = [];
  for (const e of S2_ENTRIES)
    for (const range of ["short", "medium"] as const) {
      const [params, fn] = e[range];
      out.push(
        spec(
          e.kind,
          s2EntryId(e.id, range),
          `S2 ${e.label}${range === "medium" ? " (slow)" : ""}`,
          params,
          fn,
        ),
      );
    }
  return [...out, ...S2_INDICATIONS];
}
