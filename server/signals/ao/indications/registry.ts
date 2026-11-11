// Indication registry: 10 kinds × independent configs. Each returns a per-bar directional STATE
// (+1 bullish, -1 bearish, 0 neutral) using only bars 0..i. Bots use it as an agree-filter; the
// "follow" bot enters on state onsets.
import type { IndicationDef, IndicationKind } from "../domain/types.ts";
import type { SeriesCache } from "./cache.ts";
import * as I from "../math/indicators.ts";
import { SIGNAL_SOURCES, signalId } from "../signal-config.ts";
import { stable02Specs } from "./stable02.ts";
import { researchSpecs } from "./research.ts";
import { research2Specs } from "./research2.ts";
import { research3Specs } from "./research3.ts";
import { research4Specs } from "./research4.ts";
import { research5Specs } from "./research5.ts";
import { microSpecs } from "./micro.ts";
import { inverseSpecs } from "./inverse.ts";
import { INVERSE_TIMEFRAMES, isInverseInd } from "./inverse-config.ts";
import { microFamilySpecs, inverseMicroSpecs } from "./micro-family.ts";
import { rangeFamilySpecs } from "./range-families.ts";
import { tacticSpecs } from "./tactic.ts";
import {
  isSignalCoordinationInd,
  signalCoordinationSourceLabel,
  signalCoordinationState,
} from "../signal-coordination.ts";
import { relateSpecs } from "./relate.ts";
import { breakSpecs } from "./break.ts";
import { isLowRangeInd, LOW_RANGE_TIMEFRAMES, lowRangeSpecs } from "./low-range.ts";

export { SIGNAL_SOURCES, signalId };

export interface IndicationSpec extends IndicationDef {
  fn: (k: SeriesCache) => Int8Array;
}

const sgn = (x: number) => (x > 0 ? 1 : x < 0 ? -1 : 0);
const ok = (...xs: number[]) => xs.every((x) => Number.isFinite(x));

// These callbacks are synchronous. The scope is restored even on failure or nested source evaluation.
let streamScope: { math: NonNullable<SeriesCache["streamMath"]>; id: string } | null = null;
function state(n: number, f: (i: number) => number, key?: string): Int8Array {
  if (streamScope && key)
    return streamScope.math.run<{ value: Int8Array }, number>(
      `registry:${streamScope.id}:${key}`,
      n,
      ["value"],
      () => 0,
      (i, out) => {
        const v = f(i);
        out.value[i] = Number.isFinite(v) ? sgn(v) : 0;
      },
      ["value"],
    ).value;
  const out = new Int8Array(n);
  for (let i = 0; i < n; i++) {
    const v = f(i);
    out[i] = Number.isFinite(v) ? (sgn(v) as -1 | 0 | 1) : 0;
  }
  return out;
}

/**
 * Bollinger squeeze break: the band width of bar i-1 is within `tight` of its tightest over the previous `look`
 * bars, and bar i closes outside the band. Causal (bar i reads bars ≤ i) and side-symmetric: a close above the
 * upper band is +1, below the lower band −1, held 8 bars.
 */
function squeezeBreak(
  k: {
    bb: (p: number, d: number) => { up: Float64Array; lo: Float64Array; width: Float64Array };
    b: { c: Float64Array; n: number };
  },
  look: number,
  tight: number,
): Int8Array {
  const { up, lo, width } = k.bb(20, 2);
  const c = k.b.c;
  const n = k.b.n;
  const ev = new Int8Array(n);
  for (let i = look; i < n; i++) {
    let minW = Infinity;
    for (let j = i - look; j < i; j++) if (width[j] < minW) minW = width[j];
    if (width[i - 1] <= minW * tight) ev[i] = c[i] > up[i] ? 1 : c[i] < lo[i] ? -1 : 0;
  }
  return hold(ev, 8, "hold:63");
}

/** Extend event bars into a state that persists `keep` bars (latest event wins). */
function hold(ev: Int8Array, keep: number, key?: string): Int8Array {
  if (streamScope && key)
    return streamScope.math.run<{ value: Int8Array }, { cur: number; left: number }>(
      `registry:${streamScope.id}:${key}:${keep}`,
      ev.length,
      ["value"],
      () => ({ cur: 0, left: 0 }),
      (i, out, s) => {
        if (ev[i] !== 0) {
          s.cur = ev[i];
          s.left = keep;
        }
        if (s.left > 0) {
          out.value[i] = s.cur;
          s.left--;
        }
      },
      ["value"],
    ).value;
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

function spec(
  kind: IndicationKind,
  id: string,
  label: string,
  params: Record<string, number>,
  fn: (k: SeriesCache) => Int8Array,
): IndicationSpec {
  // Signal aliases deliberately share their exact source function and numeric continuation.
  if (id.startsWith("sig-")) return { id, kind, label, params, fn };
  return {
    id,
    kind,
    label,
    params,
    fn: (k) => {
      const previous = streamScope;
      streamScope = k.streamMath ? { math: k.streamMath, id } : null;
      try {
        return fn(k);
      } finally {
        streamScope = previous;
      }
    },
  };
}

const BASE_INDICATIONS: readonly IndicationSpec[] = [
  // ── trend ───────────────────────────────────────────────
  spec("trend", "trend-ema", "EMA 9/21 trend", { fast: 9, slow: 21 }, (k) => {
    const f = k.ema(9),
      s = k.ema(21),
      c = k.b.c;
    return state(
      k.b.n,
      (i) =>
        ok(f[i], s[i])
          ? f[i] > s[i] && c[i] > s[i]
            ? 1
            : f[i] < s[i] && c[i] < s[i]
              ? -1
              : 0
          : NaN,
      "state:100",
    );
  }),
  spec("trend", "trend-adx", "ADX 25 + DI", { p: 14, min: 25 }, (k) => {
    const { adx, pdi, mdi } = k.dmi(14);
    return state(k.b.n, (i) => (ok(adx[i]) && adx[i] >= 25 ? pdi[i] - mdi[i] : 0), "state:106");
  }),
  spec("trend", "trend-st", "Supertrend 10×3", { p: 10, m: 3 }, (k) => k.st(10, 3).dir),
  spec("trend", "trend-ribbon", "Ribbon 9/21/55", { a: 9, b: 21, c: 55 }, (k) => {
    const a = k.ema(9),
      b = k.ema(21),
      c = k.ema(55);
    return state(
      k.b.n,
      (i) =>
        ok(a[i], b[i], c[i])
          ? a[i] > b[i] && b[i] > c[i]
            ? 1
            : a[i] < b[i] && b[i] < c[i]
              ? -1
              : 0
          : NaN,
      "state:113",
    );
  }),
  // ── break ───────────────────────────────────────────────
  spec("break", "break-don20", "Donchian 20 break", { p: 20, keep: 6 }, (k) => {
    const { hi, lo } = k.don(20),
      c = k.b.c;
    return hold(
      state(k.b.n, (i) => (c[i] > hi[i] ? 1 : c[i] < lo[i] ? -1 : 0), "state:128"),
      6,
      "hold:127",
    );
  }),
  spec("break", "break-vol", "Break + volume 1.6×", { p: 20, vol: 1.6, keep: 6 }, (k) => {
    const { hi, lo } = k.don(20),
      c = k.b.c,
      v = k.b.v,
      vs = k.volSma(20);
    return hold(
      state(
        k.b.n,
        (i) => (v[i] > 1.6 * vs[i] ? (c[i] > hi[i] ? 1 : c[i] < lo[i] ? -1 : 0) : 0),
        "state:138",
      ),
      6,
      "hold:137",
    );
  }),
  spec("break", "break-atr", "ATR 1.15× expansion", { p: 14, mul: 1.15, keep: 4 }, (k) => {
    const a = k.atr(14),
      c = k.b.c;
    return hold(
      state(
        k.b.n,
        (i) =>
          i > 0 && ok(a[i - 1]) && Math.abs(c[i] - c[i - 1]) > 1.15 * a[i - 1]
            ? c[i] - c[i - 1]
            : 0,
        "state:146",
      ),
      4,
      "hold:145",
    );
  }),
  spec("break", "break-squeeze", "BB squeeze break", { p: 20, look: 60, keep: 8 }, (k) =>
    squeezeBreak(k, 60, 1.15),
  ),
  // break-squeeze had the best traded PF of the top Base passers (14.13 on 110 orders), so the look-back and the
  // tightness are sampled around it: a shorter / longer quiet stretch, a tighter / looser squeeze
  spec(
    "break",
    "break-squeeze-30",
    "BB squeeze break, 30 bars",
    { p: 20, look: 30, keep: 8 },
    (k) => squeezeBreak(k, 30, 1.15),
  ),
  spec(
    "break",
    "break-squeeze-120",
    "BB squeeze break, 120 bars",
    { p: 20, look: 120, keep: 8 },
    (k) => squeezeBreak(k, 120, 1.15),
  ),
  spec("break", "break-squeeze-t10", "BB squeeze break, tight", { p: 20, look: 60, keep: 8 }, (k) =>
    squeezeBreak(k, 60, 1.1),
  ),
  spec("break", "break-squeeze-t25", "BB squeeze break, loose", { p: 20, look: 60, keep: 8 }, (k) =>
    squeezeBreak(k, 60, 1.25),
  ),
  spec("break", "break-retest", "Break retest hold", { p: 20, keep: 5 }, (k) => {
    const { hi, lo } = k.don(20),
      { c, l, h } = k.b,
      n = k.b.n;
    const ev = new Int8Array(n);
    let lastUp = -99,
      lastDn = -99,
      lvlU = 0,
      lvlD = 0;
    for (let i = 1; i < n; i++) {
      if (c[i] > hi[i]) {
        lastUp = i;
        lvlU = hi[i];
      }
      if (c[i] < lo[i]) {
        lastDn = i;
        lvlD = lo[i];
      }
      if (i - lastUp > 1 && i - lastUp <= 8 && l[i] <= lvlU * 1.001 && c[i] > lvlU) ev[i] = 1;
      if (i - lastDn > 1 && i - lastDn <= 8 && h[i] >= lvlD * 0.999 && c[i] < lvlD) ev[i] = -1;
    }
    return hold(ev, 5, "hold:180");
  }),
  spec("break", "break-fail", "Failed break fade", { p: 20, keep: 5 }, (k) => {
    const { hi, lo } = k.don(20),
      { c, h, l } = k.b;
    return hold(
      state(
        k.b.n,
        (i) => (h[i] > hi[i] && c[i] < hi[i] ? -1 : l[i] < lo[i] && c[i] > lo[i] ? 1 : 0),
        "state:186",
      ),
      5,
      "hold:185",
    );
  }),
  // ── active ──────────────────────────────────────────────
  spec("active", "act-burst", "Range burst 1.8×", { p: 20, mul: 1.8, keep: 4 }, (k) => {
    const rs = k.rangeSma(20),
      { o, c, h, l } = k.b;
    return hold(
      state(k.b.n, (i) => (i > 0 && h[i] - l[i] > 1.8 * rs[i - 1] ? c[i] - o[i] : 0), "state:197"),
      4,
      "hold:196",
    );
  }),
  spec("active", "act-shift", "Range shift 20/60", { a: 20, b: 60 }, (k) => {
    const a = k.rangeSma(20),
      b = k.rangeSma(60),
      r = k.roc(10);
    return state(k.b.n, (i) => (ok(a[i], b[i], r[i]) && a[i] > 1.2 * b[i] ? r[i] : 0), "state:205");
  }),
  spec("active", "act-hf", "HF momentum 3", { n: 3 }, (k) => {
    const a = k.atr(14),
      c = k.b.c;
    return state(
      k.b.n,
      (i) => {
        if (i < 3 || !ok(a[i])) return NaN;
        const d = c[i] - c[i - 3];
        return Math.abs(d) > 0.9 * a[i] ? d : 0;
      },
      "state:210",
    );
  }),
  spec("active", "act-chop", "Chop fade", { p: 14 }, (k) => {
    const { adx } = k.dmi(14),
      r = k.rsi(7);
    return state(
      k.b.n,
      (i) => (ok(adx[i], r[i]) && adx[i] < 18 ? (r[i] < 30 ? 1 : r[i] > 70 ? -1 : 0) : 0),
      "state:219",
    );
  }),
  // ── direction ───────────────────────────────────────────
  spec("direction", "dir-emax", "EMA 9/21 cross", { f: 9, s: 21 }, (k) => {
    const f = k.ema(9),
      s = k.ema(21);
    return state(k.b.n, (i) => f[i] - s[i], "state:227");
  }),
  spec("direction", "dir-st", "Supertrend 7×2 flip", { p: 7, m: 2 }, (k) => k.st(7, 2).dir),
  spec("direction", "dir-vwap", "VWAP 60 axis", { p: 60 }, (k) => {
    const w = k.vwap(60),
      c = k.b.c;
    return state(k.b.n, (i) => c[i] - w[i], "state:233");
  }),
  spec("direction", "dir-macd", "MACD hist sign", {}, (k) => {
    const { hist } = k.macd();
    return state(k.b.n, (i) => hist[i], "state:237");
  }),
  spec("direction", "dir-thrust", "3-bar thrust", { n: 3, keep: 3 }, (k) => {
    const { c, o } = k.b;
    return hold(
      state(
        k.b.n,
        (i) => {
          if (i < 2) return 0;
          const u =
            c[i] > o[i] &&
            c[i - 1] > o[i - 1] &&
            c[i - 2] > o[i - 2] &&
            c[i] > c[i - 1] &&
            c[i - 1] > c[i - 2];
          const d =
            c[i] < o[i] &&
            c[i - 1] < o[i - 1] &&
            c[i - 2] < o[i - 2] &&
            c[i] < c[i - 1] &&
            c[i - 1] < c[i - 2];
          return u ? 1 : d ? -1 : 0;
        },
        "state:242",
      ),
      3,
      "hold:241",
    );
  }),
  spec("direction", "dir-reclaim", "EMA21 reclaim", { p: 21, keep: 4 }, (k) => {
    const e = k.ema(21),
      c = k.b.c;
    return hold(
      state(
        k.b.n,
        (i) =>
          i > 0 && ok(e[i - 1])
            ? c[i - 1] < e[i - 1] && c[i] > e[i]
              ? 1
              : c[i - 1] > e[i - 1] && c[i] < e[i]
                ? -1
                : 0
            : 0,
        "state:265",
      ),
      4,
      "hold:264",
    );
  }),
  // ── move ────────────────────────────────────────────────
  spec("move", "move-impulse", "Impulse 6 bars 1.5 ATR", { n: 6, mul: 1.5, keep: 4 }, (k) => {
    const a = k.atr(14),
      c = k.b.c;
    return hold(
      state(
        k.b.n,
        (i) => (i >= 6 && ok(a[i]) && Math.abs(c[i] - c[i - 6]) > 1.5 * a[i] ? c[i] - c[i - 6] : 0),
        "state:282",
      ),
      4,
      "hold:281",
    );
  }),
  spec("move", "move-swing", "Swing 8 mid", { n: 8 }, (k) => {
    const { hi, lo } = k.don(8),
      c = k.b.c;
    return state(
      k.b.n,
      (i) =>
        ok(hi[i], lo[i])
          ? c[i] > hi[i] - (hi[i] - lo[i]) * 0.25
            ? 1
            : c[i] < lo[i] + (hi[i] - lo[i]) * 0.25
              ? -1
              : 0
          : NaN,
      "state:291",
    );
  }),
  spec("move", "move-cont", "Continuation ROC12", { n: 12 }, (k) => {
    const r12 = k.roc(12),
      r3 = k.roc(3);
    return state(
      k.b.n,
      (i) =>
        ok(r12[i], r3[i])
          ? r12[i] > 0.004 && r3[i] < 0
            ? 1
            : r12[i] < -0.004 && r3[i] > 0
              ? -1
              : 0
          : NaN,
      "state:304",
    );
  }),
  // ── rsi ─────────────────────────────────────────────────
  spec("rsi", "rsi-extreme", "RSI14 30/70 revert", { p: 14, lo: 30, hi: 70 }, (k) => {
    const r = k.rsi(14);
    return state(k.b.n, (i) => (r[i] < 30 ? 1 : r[i] > 70 ? -1 : 0), "state:317");
  }),
  spec("rsi", "rsi-mid", "RSI14 55/45 momentum", { p: 14 }, (k) => {
    const r = k.rsi(14);
    return state(k.b.n, (i) => (r[i] > 55 ? 1 : r[i] < 45 ? -1 : 0), "state:321");
  }),
  spec("rsi", "rsi-fast", "RSI7 20/80 revert", { p: 7 }, (k) => {
    const r = k.rsi(7);
    return state(k.b.n, (i) => (r[i] < 20 ? 1 : r[i] > 80 ? -1 : 0), "state:325");
  }),
  spec("rsi", "rsi-div", "RSI divergence 10", { p: 14, look: 10, keep: 4 }, (k) => {
    const r = k.rsi(14),
      c = k.b.c;
    return hold(
      state(
        k.b.n,
        (i) => {
          if (i < 10 || !ok(r[i], r[i - 10])) return 0;
          if (c[i] < c[i - 10] && r[i] > r[i - 10] + 4 && r[i] < 45) return 1;
          if (c[i] > c[i - 10] && r[i] < r[i - 10] - 4 && r[i] > 55) return -1;
          return 0;
        },
        "state:331",
      ),
      4,
      "hold:330",
    );
  }),
  // ── bollinger ───────────────────────────────────────────
  spec("bollinger", "bb-bounce", "BB bounce", { p: 20, k: 2 }, (k) => {
    const { up, lo } = k.bb(20, 2),
      c = k.b.c;
    return state(k.b.n, (i) => (c[i] < lo[i] ? 1 : c[i] > up[i] ? -1 : 0), "state:344");
  }),
  spec("bollinger", "bb-walk", "BB band walk", { p: 20, k: 2 }, (k) => {
    const { up, lo } = k.bb(20, 2),
      c = k.b.c;
    return state(
      k.b.n,
      (i) =>
        i > 0 && c[i] > up[i] && c[i - 1] > up[i - 1]
          ? 1
          : i > 0 && c[i] < lo[i] && c[i - 1] < lo[i - 1]
            ? -1
            : 0,
      "state:349",
    );
  }),
  spec("bollinger", "bb-mid", "BB mid reclaim", { p: 20, keep: 4 }, (k) => {
    const { mid } = k.bb(20, 2),
      c = k.b.c;
    return hold(
      state(
        k.b.n,
        (i) =>
          i > 0 && ok(mid[i - 1])
            ? c[i - 1] < mid[i - 1] && c[i] > mid[i]
              ? 1
              : c[i - 1] > mid[i - 1] && c[i] < mid[i]
                ? -1
                : 0
            : 0,
        "state:361",
      ),
      4,
      "hold:360",
    );
  }),
  spec("bollinger", "bb-wick", "BB wick tag", { p: 20, keep: 3 }, (k) => {
    const { up, lo } = k.bb(20, 2),
      { c, h, l } = k.b;
    return hold(
      state(
        k.b.n,
        (i) => (l[i] < lo[i] && c[i] > lo[i] ? 1 : h[i] > up[i] && c[i] < up[i] ? -1 : 0),
        "state:377",
      ),
      3,
      "hold:376",
    );
  }),
  // ── sar ─────────────────────────────────────────────────
  spec("sar", "sar-std", "PSAR 0.02", { step: 0.02 }, (k) => k.psar(0.02, 0.2).dir),
  spec("sar", "sar-fast", "PSAR 0.04 fast", { step: 0.04 }, (k) => k.psar(0.04, 0.3).dir),
  spec("sar", "sar-flip", "PSAR flip event", { step: 0.02, keep: 6 }, (k) => {
    const d = k.psar(0.02, 0.2).dir;
    return hold(
      state(k.b.n, (i) => (i > 0 && d[i] !== d[i - 1] ? d[i] : 0), "state:389"),
      6,
      "hold:388",
    );
  }),
  // ── macd ────────────────────────────────────────────────
  spec("macd", "macd-cross", "MACD signal cross", { keep: 6 }, (k) => {
    const { line, signal } = k.macd();
    return hold(
      state(
        k.b.n,
        (i) =>
          i > 0 && ok(signal[i - 1])
            ? line[i - 1] < signal[i - 1] && line[i] > signal[i]
              ? 1
              : line[i - 1] > signal[i - 1] && line[i] < signal[i]
                ? -1
                : 0
            : 0,
        "state:397",
      ),
      6,
      "hold:396",
    );
  }),
  spec("macd", "macd-hist", "MACD hist rising", {}, (k) => {
    const { hist } = k.macd();
    return state(
      k.b.n,
      (i) =>
        i > 1 && ok(hist[i - 2])
          ? hist[i] > hist[i - 1] && hist[i - 1] > hist[i - 2]
            ? 1
            : hist[i] < hist[i - 1] && hist[i - 1] < hist[i - 2]
              ? -1
              : 0
          : NaN,
      "state:411",
    );
  }),
  spec("macd", "macd-zero", "MACD zero line", {}, (k) => {
    const { line } = k.macd();
    return state(k.b.n, (i) => line[i], "state:423");
  }),
  // ── ema ─────────────────────────────────────────────────
  spec("ema", "ema-21-55", "EMA 21/55", { f: 21, s: 55 }, (k) => {
    const f = k.ema(21),
      s = k.ema(55);
    return state(k.b.n, (i) => f[i] - s[i], "state:429");
  }),
  spec("ema", "ema-pullback", "EMA21 pullback", { p: 21 }, (k) => {
    const e21 = k.ema(21),
      e55 = k.ema(55),
      { c, l, h } = k.b;
    return state(
      k.b.n,
      (i) => {
        if (!ok(e21[i], e55[i])) return NaN;
        if (e21[i] > e55[i] && l[i] <= e21[i] && c[i] > e21[i]) return 1;
        if (e21[i] < e55[i] && h[i] >= e21[i] && c[i] < e21[i]) return -1;
        return 0;
      },
      "state:435",
    );
  }),
  spec("ema", "ema-slope", "EMA50 slope", { p: 50, n: 5 }, (k) => {
    const e = k.ema(50);
    return state(
      k.b.n,
      (i) => (i >= 5 && ok(e[i - 5]) ? (e[i] - e[i - 5]) / e[i] - 0 : NaN),
      "state:444",
    );
  }),
  spec("ema", "ema-stoch", "EMA trend + stoch dip", { p: 21 }, (k) => {
    const e = k.ema(21),
      e2 = k.ema(55),
      { k: st } = k.stoch(14, 3);
    return state(
      k.b.n,
      (i) =>
        ok(e[i], e2[i], st[i])
          ? e[i] > e2[i] && st[i] < 25
            ? 1
            : e[i] < e2[i] && st[i] > 75
              ? -1
              : 0
          : NaN,
      "state:450",
    );
  }),
];

// ── parameter families: every kind extended with independent variants ─────────────────────────────
const VARIANTS: IndicationSpec[] = [];
const add = (x: IndicationSpec) => VARIANTS.push(x);

for (const [f, sl] of [
  [5, 20],
  [12, 26],
  [20, 50],
  [50, 200],
] as const)
  add(
    spec("trend", `trend-ema-${f}-${sl}`, `EMA ${f}/${sl} trend`, { fast: f, slow: sl }, (k) => {
      const a = k.ema(f),
        b = k.ema(sl),
        c = k.b.c;
      return state(
        k.b.n,
        (i) =>
          ok(a[i], b[i])
            ? a[i] > b[i] && c[i] > b[i]
              ? 1
              : a[i] < b[i] && c[i] < b[i]
                ? -1
                : 0
            : NaN,
        "state:477",
      );
    }),
  );
for (const min of [20, 30])
  add(
    spec("trend", `trend-adx-${min}`, `ADX ${min} + DI`, { p: 14, min }, (k) => {
      const { adx, pdi, mdi } = k.dmi(14);
      return state(k.b.n, (i) => (ok(adx[i]) && adx[i] >= min ? pdi[i] - mdi[i] : 0), "state:492");
    }),
  );
// trend-st-14-4 passed 4 of its 70 Base combos and traded at PF 1.38, so the pair grid is sampled more finely
for (const [p, m] of [
  [7, 2],
  [14, 2],
  [14, 4],
  [21, 3],
  [21, 5],
  [28, 6],
  [35, 7],
] as const)
  add(spec("trend", `trend-st-${p}-${m}`, `Supertrend ${p}×${m}`, { p, m }, (k) => k.st(p, m).dir));

for (const p of [10, 40, 55])
  add(
    spec("break", `break-don${p}`, `Donchian ${p} break`, { p, keep: 6 }, (k) => {
      const { hi, lo } = k.don(p),
        c = k.b.c;
      return hold(
        state(k.b.n, (i) => (c[i] > hi[i] ? 1 : c[i] < lo[i] ? -1 : 0), "state:513"),
        6,
        "hold:512",
      );
    }),
  );
for (const mul of [1.3, 2.0])
  add(
    spec(
      "break",
      `break-vol-${mul}`,
      `Break + volume ${mul}×`,
      { p: 20, vol: mul, keep: 6 },
      (k) => {
        const { hi, lo } = k.don(20),
          c = k.b.c,
          v = k.b.v,
          vs = k.volSma(20);
        return hold(
          state(
            k.b.n,
            (i) => (v[i] > mul * vs[i] ? (c[i] > hi[i] ? 1 : c[i] < lo[i] ? -1 : 0) : 0),
            "state:531",
          ),
          6,
          "hold:530",
        );
      },
    ),
  );
for (const mul of [0.9, 1.5, 2.0])
  add(
    spec("break", `break-atr-${mul}`, `ATR ${mul}× expansion`, { p: 14, mul, keep: 4 }, (k) => {
      const a = k.atr(14),
        c = k.b.c;
      return hold(
        state(
          k.b.n,
          (i) =>
            i > 0 && ok(a[i - 1]) && Math.abs(c[i] - c[i - 1]) > mul * a[i - 1]
              ? c[i] - c[i - 1]
              : 0,
          "state:545",
        ),
        4,
        "hold:544",
      );
    }),
  );

for (const mul of [1.5, 2.5])
  add(
    spec("active", `act-burst-${mul}`, `Range burst ${mul}×`, { p: 20, mul, keep: 4 }, (k) => {
      const rs = k.rangeSma(20),
        { o, c, h, l } = k.b;
      return hold(
        state(
          k.b.n,
          (i) => (i > 0 && h[i] - l[i] > mul * rs[i - 1] ? c[i] - o[i] : 0),
          "state:559",
        ),
        4,
        "hold:558",
      );
    }),
  );
for (const n of [5, 8])
  add(
    spec("active", `act-hf-${n}`, `HF momentum ${n}`, { n }, (k) => {
      const a = k.atr(14),
        c = k.b.c;
      return state(
        k.b.n,
        (i) => {
          if (i < n || !ok(a[i])) return NaN;
          const d = c[i] - c[i - n];
          return Math.abs(d) > 0.9 * a[i] * Math.sqrt(n / 3) ? d : 0;
        },
        "state:569",
      );
    }),
  );

for (const [f, sl] of [
  [5, 13],
  [12, 26],
  [20, 50],
] as const)
  add(
    spec("direction", `dir-emax-${f}-${sl}`, `EMA ${f}/${sl} cross`, { f, s: sl }, (k) => {
      const a = k.ema(f),
        b = k.ema(sl);
      return state(k.b.n, (i) => a[i] - b[i], "state:586");
    }),
  );
for (const p of [30, 120, 240])
  add(
    spec("direction", `dir-vwap-${p}`, `VWAP ${p} axis`, { p }, (k) => {
      const w = k.vwap(p),
        c = k.b.c;
      return state(k.b.n, (i) => c[i] - w[i], "state:594");
    }),
  );
add(
  spec("direction", "dir-thrust-4", "4-bar thrust", { n: 4, keep: 3 }, (k) => {
    const { c, o } = k.b;
    return hold(
      state(k.b.n, (i) => {
        if (i < 3) return 0;
        let u = true,
          d = true;
        for (let j = i - 3; j <= i; j++) {
          if (!(c[j] > o[j] && (j === i - 3 || c[j] > c[j - 1]))) u = false;
          if (!(c[j] < o[j] && (j === i - 3 || c[j] < c[j - 1]))) d = false;
        }
        return u ? 1 : d ? -1 : 0;
      }),
      3,
      "hold:600",
    );
  }),
);
add(
  spec("direction", "dir-reclaim-50", "EMA50 reclaim", { p: 50, keep: 4 }, (k) => {
    const e = k.ema(50),
      c = k.b.c;
    return hold(
      state(
        k.b.n,
        (i) =>
          i > 0 && ok(e[i - 1])
            ? c[i - 1] < e[i - 1] && c[i] > e[i]
              ? 1
              : c[i - 1] > e[i - 1] && c[i] < e[i]
                ? -1
                : 0
            : 0,
        "state:620",
      ),
      4,
      "hold:619",
    );
  }),
);

for (const [n, mul] of [
  [4, 1.2],
  [10, 2.0],
  [20, 2.5],
] as const)
  add(
    spec(
      "move",
      `move-impulse-${n}-${mul}`,
      `Impulse ${n} bars ${mul} ATR`,
      { n, mul, keep: 4 },
      (k) => {
        const a = k.atr(14),
          c = k.b.c;
        return hold(
          state(
            k.b.n,
            (i) =>
              i >= n && ok(a[i]) && Math.abs(c[i] - c[i - n]) > mul * a[i] ? c[i] - c[i - n] : 0,
            "state:649",
          ),
          4,
          "hold:648",
        );
      },
    ),
  );
for (const n of [16, 32])
  add(
    spec("move", `move-swing-${n}`, `Swing ${n} mid`, { n }, (k) => {
      const { hi, lo } = k.don(n),
        c = k.b.c;
      return state(
        k.b.n,
        (i) =>
          ok(hi[i], lo[i])
            ? c[i] > hi[i] - (hi[i] - lo[i]) * 0.25
              ? 1
              : c[i] < lo[i] + (hi[i] - lo[i]) * 0.25
                ? -1
                : 0
            : NaN,
        "state:662",
      );
    }),
  );

for (const [p, lo, hi] of [
  [14, 25, 75],
  [14, 20, 80],
  [21, 30, 70],
  [7, 15, 85],
] as const)
  add(
    spec("rsi", `rsi-${p}-${lo}-${hi}`, `RSI${p} ${lo}/${hi} revert`, { p, lo, hi }, (k) => {
      const r = k.rsi(p);
      return state(k.b.n, (i) => (r[i] < lo ? 1 : r[i] > hi ? -1 : 0), "state:683");
    }),
  );
// RSI-extreme MOMENTUM: bullish while RSI is above 100-lvl, bearish below lvl. With the "follow" bot this enters
// with the move on the bar RSI crosses into the extreme — the most robust family in the 1-year two-half research
// (docs/family-1h.md, docs/adjust-1h.md).
for (const [p, lvl] of [
  [10, 25],
  [14, 15],
  [14, 20],
  [14, 25],
  [21, 15],
  [21, 20],
  [21, 25],
] as const)
  add(
    spec("rsi", `rsi-mom-${p}-${lvl}`, `RSI${p} ${lvl}/${100 - lvl} momentum`, { p, lvl }, (k) => {
      const r = k.rsi(p);
      return state(k.b.n, (i) => (r[i] > 100 - lvl ? 1 : r[i] < lvl ? -1 : 0), "state:701");
    }),
  );
for (const [up, dn] of [
  [60, 40],
  [52, 48],
] as const)
  add(
    spec("rsi", `rsi-mid-${up}-${dn}`, `RSI14 ${up}/${dn} momentum`, { p: 14, up, dn }, (k) => {
      const r = k.rsi(14);
      return state(k.b.n, (i) => (r[i] > up ? 1 : r[i] < dn ? -1 : 0), "state:711");
    }),
  );

for (const [p, kk] of [
  [20, 2.5],
  [20, 3],
  [50, 2],
] as const)
  add(
    spec("bollinger", `bb-bounce-${p}-${kk}`, `BB ${p}/${kk} bounce`, { p, k: kk }, (k) => {
      const { up, lo } = k.bb(p, kk),
        c = k.b.c;
      return state(k.b.n, (i) => (c[i] < lo[i] ? 1 : c[i] > up[i] ? -1 : 0), "state:724");
    }),
  );
add(
  spec("bollinger", "bb-walk-50", "BB 50 band walk", { p: 50, k: 2 }, (k) => {
    const { up, lo } = k.bb(50, 2),
      c = k.b.c;
    return state(
      k.b.n,
      (i) =>
        i > 0 && c[i] > up[i] && c[i - 1] > up[i - 1]
          ? 1
          : i > 0 && c[i] < lo[i] && c[i - 1] < lo[i - 1]
            ? -1
            : 0,
      "state:731",
    );
  }),
);

for (const step of [0.01, 0.03])
  add(spec("sar", `sar-${step}`, `PSAR ${step}`, { step }, (k) => k.psar(step, 0.2).dir));

for (const [f, sl, g] of [
  [5, 35, 5],
  [8, 21, 5],
  [19, 39, 9],
] as const) {
  add(
    spec(
      "macd",
      `macd-cross-${f}-${sl}-${g}`,
      `MACD ${f}/${sl}/${g} cross`,
      { f, s: sl, g, keep: 6 },
      (k) => {
        const { line, signal } = k.macd(f, sl, g);
        return hold(
          state(
            k.b.n,
            (i) =>
              i > 0 && ok(signal[i - 1])
                ? line[i - 1] < signal[i - 1] && line[i] > signal[i]
                  ? 1
                  : line[i - 1] > signal[i - 1] && line[i] < signal[i]
                    ? -1
                    : 0
                : 0,
            "state:758",
          ),
          6,
          "hold:757",
        );
      },
    ),
  );
  add(
    spec(
      "macd",
      `macd-hist-${f}-${sl}-${g}`,
      `MACD ${f}/${sl}/${g} hist sign`,
      { f, s: sl, g },
      (k) => {
        const { hist } = k.macd(f, sl, g);
        return state(k.b.n, (i) => hist[i], "state:780");
      },
    ),
  );
}

for (const [f, sl] of [
  [9, 21],
  [50, 100],
] as const)
  add(
    spec("ema", `ema-${f}-${sl}`, `EMA ${f}/${sl}`, { f, s: sl }, (k) => {
      const a = k.ema(f),
        b = k.ema(sl);
      return state(k.b.n, (i) => a[i] - b[i], "state:794");
    }),
  );
// the ema kind passes Base best of all kinds (3.7 %) and ema-slope-20 is third of all indications (5 of 70)
for (const p of [10, 20, 34, 100, 200])
  add(
    spec("ema", `ema-slope-${p}`, `EMA${p} slope`, { p, n: 5 }, (k) => {
      const e = k.ema(p);
      return state(k.b.n, (i) => (i >= 5 && ok(e[i - 5]) ? e[i] - e[i - 5] : NaN), "state:802");
    }),
  );
// the same slope over a shorter and a longer look-back: the turn is read sooner or later on the same average
for (const n of [3, 10])
  add(
    spec("ema", `ema-slope-20-${n}`, `EMA20 slope over ${n}`, { p: 20, n }, (k) => {
      const e = k.ema(20);
      return state(k.b.n, (i) => (i >= n && ok(e[i - n]) ? e[i] - e[i - n] : NaN), "state:810");
    }),
  );
add(
  spec("ema", "ema-pullback-50", "EMA50 pullback", { p: 50 }, (k) => {
    const e = k.ema(50),
      e2 = k.ema(100),
      { c, l, h } = k.b;
    return state(
      k.b.n,
      (i) => {
        if (!ok(e[i], e2[i])) return NaN;
        if (e[i] > e2[i] && l[i] <= e[i] && c[i] > e[i]) return 1;
        if (e[i] < e2[i] && h[i] >= e[i] && c[i] < e[i]) return -1;
        return 0;
      },
      "state:818",
    );
  }),
);

// ── common indicators (one by one, fine ranges) ─────────────
// oscillators: "+1" = oversold (the revert reading); the follow / revert bots take either direction
for (const p of [14, 20, 40])
  for (const lvl of [100, 200])
    add(
      spec("osc", `cci-${p}-${lvl}`, `CCI${p} ±${lvl}`, { p, lvl }, (k) => {
        const x = k.cci(p);
        return state(k.b.n, (i) => (x[i] < -lvl ? 1 : x[i] > lvl ? -1 : 0), "state:834");
      }),
    );
// periods and levels: willr-14-90 had the best Base pass rate of all 417 indications (8 of 70 combos, 530 orders)
// and willr-28-90 the fourteenth, so the family is sampled finely (measured, docs/positive-coordinations.md)
for (const p of [7, 14, 21, 28, 50])
  for (const lvl of [80, 90, 95])
    add(
      spec("osc", `willr-${p}-${lvl}`, `Williams %R${p} ${lvl}`, { p, lvl }, (k) => {
        const x = k.willr(p);
        return state(k.b.n, (i) => (x[i] < -lvl ? 1 : x[i] > lvl - 100 ? -1 : 0), "state:844");
      }),
    );
for (const lvl of [10, 20])
  add(
    spec("osc", `srsi-14-${lvl}`, `Stoch RSI 14 ${lvl}/${100 - lvl}`, { p: 14, lvl }, (k) => {
      const x = k.stochRsi(14);
      return state(k.b.n, (i) => (x[i] < lvl ? 1 : x[i] > 100 - lvl ? -1 : 0), "state:851");
    }),
  );
for (const p of [20, 50])
  for (const t of [2, 2.5])
    add(
      spec("osc", `z-${p}-${t}`, `Z-score ${p} ±${t}`, { p, t }, (k) => {
        const x = k.z(p);
        return state(k.b.n, (i) => (x[i] < -t ? 1 : x[i] > t ? -1 : 0), "state:859");
      }),
    );
// volume
for (const lvl of [20, 10])
  add(
    spec("volume", `mfi-14-${lvl}`, `MFI14 ${lvl}/${100 - lvl}`, { p: 14, lvl }, (k) => {
      const x = k.mfi(14);
      return state(k.b.n, (i) => (x[i] < lvl ? 1 : x[i] > 100 - lvl ? -1 : 0), "state:867");
    }),
  );
for (const p of [20, 50])
  add(
    spec("volume", `obv-${p}`, `OBV vs EMA${p}`, { p }, (k) => {
      const o = k.obv();
      const e = k.memo(`obvema${p}`, () => I.ema(o, p));
      return state(k.b.n, (i) => (ok(e[i]) ? o[i] - e[i] : NaN), "state:875");
    }),
  );
for (const t of [0.05, 0.1])
  add(
    spec("volume", `cmf-20-${t}`, `CMF20 ±${t}`, { p: 20, t }, (k) => {
      const x = k.cmf(20);
      return state(k.b.n, (i) => (x[i] > t ? 1 : x[i] < -t ? -1 : 0), "state:882");
    }),
  );
// channels
for (const m of [1.5, 2, 2.5])
  add(
    spec("channel", `kelt-20-${m}`, `Keltner 20×${m} break`, { p: 20, m, keep: 4 }, (k) => {
      const { up, lo } = k.kelt(20, m),
        c = k.b.c;
      return hold(
        state(k.b.n, (i) => (c[i] > up[i] ? 1 : c[i] < lo[i] ? -1 : 0), "state:892"),
        4,
        "hold:891",
      );
    }),
  );
for (const p of [20, 30])
  add(
    spec("channel", `squeeze-${p}`, `Squeeze ${p} release`, { p, keep: 6 }, (k) => {
      const bb = k.bb(p, 2),
        kc = k.kelt(p, 1.5),
        c = k.b.c;
      return hold(
        state(
          k.b.n,
          (i) => {
            if (i < 1 || !ok(bb.up[i], kc.up[i], bb.up[i - 1], kc.up[i - 1])) return 0;
            const was = bb.up[i - 1] < kc.up[i - 1] && bb.lo[i - 1] > kc.lo[i - 1];
            const now = bb.up[i] < kc.up[i] && bb.lo[i] > kc.lo[i];
            return was && !now ? c[i] - kc.mid[i] : 0;
          },
          "state:904",
        ),
        6,
        "hold:903",
      );
    }),
  );
for (const p of [14, 25])
  add(
    spec("channel", `aroon-${p}`, `Aroon ${p}`, { p }, (k) => {
      const { up, dn } = k.aroon(p);
      return state(
        k.b.n,
        (i) => (up[i] > 70 && dn[i] < 30 ? 1 : dn[i] > 70 && up[i] < 30 ? -1 : 0),
        "state:918",
      );
    }),
  );
// Ichimoku (classic 9/26/52 and the crypto 20/60/120)
for (const [t, kk, b] of [
  [9, 26, 52],
  [20, 60, 120],
] as const) {
  add(
    spec("ichimoku", `ichi-tk-${t}`, `Ichimoku ${t}/${kk} TK`, { t, k: kk, b }, (k) => {
      const x = k.ichi(t, kk, b);
      return state(
        k.b.n,
        (i) => (ok(x.tenkan[i], x.kijun[i]) ? x.tenkan[i] - x.kijun[i] : NaN),
        "state:931",
      );
    }),
  );
  add(
    spec("ichimoku", `ichi-cloud-${t}`, `Ichimoku ${t}/${kk} cloud`, { t, k: kk, b }, (k) => {
      const x = k.ichi(t, kk, b),
        c = k.b.c;
      return state(
        k.b.n,
        (i) => {
          if (!ok(x.spanA[i], x.spanB[i])) return NaN;
          const top = Math.max(x.spanA[i], x.spanB[i]),
            bot = Math.min(x.spanA[i], x.spanB[i]);
          return c[i] > top && x.tenkan[i] > x.kijun[i]
            ? 1
            : c[i] < bot && x.tenkan[i] < x.kijun[i]
              ? -1
              : 0;
        },
        "state:938",
      );
    }),
  );
}
// smoothers
for (const p of [16, 32, 55])
  add(
    spec("smooth", `hma-${p}`, `HMA ${p} slope`, { p }, (k) => {
      const x = k.hma(p);
      return state(
        k.b.n,
        (i) => (i > 0 && ok(x[i], x[i - 1]) ? x[i] - x[i - 1] : NaN),
        "state:956",
      );
    }),
  );
for (const p of [9, 15])
  add(
    spec("smooth", `trix-${p}`, `TRIX ${p}`, { p }, (k) => {
      const x = k.trix(p);
      return state(k.b.n, (i) => x[i], "state:963");
    }),
  );
for (const p of [10, 20])
  add(
    spec("smooth", `kama-${p}`, `KAMA ${p}`, { p }, (k) => {
      const x = k.kama(p),
        c = k.b.c;
      return state(
        k.b.n,
        (i) =>
          i > 0 && ok(x[i], x[i - 1])
            ? c[i] > x[i] && x[i] > x[i - 1]
              ? 1
              : c[i] < x[i] && x[i] < x[i - 1]
                ? -1
                : 0
            : NaN,
        "state:971",
      );
    }),
  );
for (const n of [1, 3])
  add(
    spec("smooth", `ha-${n}`, `Heikin-Ashi ${n} bar${n > 1 ? "s" : ""}`, { n }, (k) => {
      const x = k.ha();
      return state(k.b.n, (i) => {
        if (i < n - 1) return NaN;
        let up = 0,
          dn = 0;
        for (let j = i - n + 1; j <= i; j++) {
          if (x.c[j] > x.o[j]) up++;
          else if (x.c[j] < x.o[j]) dn++;
        }
        return up === n ? 1 : dn === n ? -1 : 0;
      });
    }),
  );

// ── Stable-02 ports ("s2-…"): the old desk's entry signals (short + slow range) and indications ──
for (const x of stable02Specs()) add(x);
// ── research signals ("r-…"): short-term crypto entries from the literature ──
for (const x of researchSpecs()) add(x);
for (const x of research2Specs()) add(x);
// third batch: quarter-hour order flow and burst reversal (Kim & Hansen 2026), Laguerre RSI, TD setup, choppiness
// break, chandelier flip, ultimate oscillator, Klinger
for (const x of research3Specs()) add(x);
for (const x of research4Specs()) add(x);
for (const x of research5Specs()) add(x);
// ── range families (13 Oct): confluence, regime / compression / session and market-relation entries for the Short,
// General and Long ranges (cfl-, htfpb-, rgm-, cmpx-, orb-, rel-) ──
for (const x of rangeFamilySpecs()) add(x);
// Tactic is a first-class indication kind; its techniques use the ordinary independent lanes and config PF gates.
for (const x of tacticSpecs()) add(x);
// Relate uses explicit 1/5/15m combinations and bounded standard/Micro identities.
for (const x of relateSpecs()) add(x);
// Break adds independent Donchian, squeeze and retest cells with trend, activity and drawdown filters.
for (const x of breakSpecs()) add(x);
// ── Micro range ("mc-…"): fast reversal entries for net 0.1–0.4 % targets (grid.micro.ownInds: only they trade Micro) ──
for (const x of microSpecs()) add(x);
for (const x of microFamilySpecs()) add(x);
for (const x of inverseSpecs([...BASE_INDICATIONS, ...VARIANTS])) add(x);
for (const x of inverseMicroSpecs()) add(x);
// Low-Range uses its own Short protection cells and explicit enablement.
for (const x of lowRangeSpecs()) add(x);

// ── combined timeframes ─────────────────────────────────────
// "<id>@x4": the indication on this timeframe, kept only where it agrees on the 4× timeframe (1h → 4h, 15m → 1h),
// from completed higher bars. These are the combos that passed PF ≥ 1.1 in all four halves of two years of 1h data
// (docs/mtf-1h-365d.md, docs/mtf-1h-prev.md); every other indication × combined timeframe is covered by the research.
export const COMBINED_BASE = [
  "bb-walk",
  "break-vol",
  "break-vol-2",
  "break-atr-2",
  "act-burst-2.5",
  "act-chop",
  "cci-14-200",
  "cci-40-200",
  "z-50-2.5",
] as const;
export const INDICATIONS: readonly IndicationSpec[] = [...BASE_INDICATIONS, ...VARIANTS];

export const INDICATION_BY_ID: ReadonlyMap<string, IndicationSpec> = new Map(
  INDICATIONS.map((s) => [s.id, s]),
);

// ── timeframe lanes ─────────────────────────────────────────
// The engine computes every indication on 1m (the base data) and on 5m, 15m and 30m derived from it, each
// independently ("<id>@m<tf>") and combined ("<id>@m<tf>c": kept only where the same indication agrees on every
// higher ENABLED timeframe — CoreSettings.tfs, carried on the series as Bars.lanes; the whole ladder without — from
// completed higher bars). A plain "<id>" (research tools, old presets) runs on whatever series it is given.
export const TF_LADDER = [1, 5, 15, 30] as const;

export interface Lane {
  base: string;
  tf: number | null;
  combined: boolean;
  /** New30m confirmation is a distinct lane; legacy Low-Range/Inverse c lanes retain their original math. */
  confirmation30?: true;
}

export function laneOf(ind: string): Lane {
  // memoized (read-only results): called in every hot loop of the simulation and the live step, it allocated a regex
  // match and an object per call (x02, 8 Oct allocation profile)
  let l = laneMemo.get(ind);
  if (l) return l;
  const m = /^(.*)@m(\d+)(c(?:30)?)?$/.exec(ind);
  l = Object.freeze(
    m
      ? { base: m[1], tf: +m[2], combined: Boolean(m[3]), ...(m[3] === "c30" ? { confirmation30: true as const } : {}) }
      : { base: ind, tf: null, combined: false },
  );
  if (laneMemo.size > 200_000) laneMemo.clear();
  laneMemo.set(ind, l);
  return l;
}
const laneMemo = new Map<string, Lane>();

export const laneInd = (base: string, tf: number, combined = false, confirmation30 = false) =>
  `${base}@m${tf}${combined ? confirmation30 ? "c30" : "c" : ""}`;

/** Factors (× tf) of the higher ladder timeframes a combined lane must agree with. */
export const higherFactors = (tf: number, ladder: readonly number[] = TF_LADDER) =>
  ladder.filter((h) => h > tf && h % tf === 0).map((h) => h / tf);

/** Short lane label for tables: "15m", "5m+" (combined). */
export const laneLabel = (ind: string) => {
  const l = laneOf(ind);
  return l.tf === null ? "" : `${l.tf}m${l.confirmation30 ? "+30m" : l.combined ? "+" : ""}`;
};

export function indicationState(id: string, k: SeriesCache): Int8Array | null {
  const lane = laneOf(id);
  if (lane.tf !== null) {
    if (lane.base === "none") return null;
    if (!lane.combined) return indicationState(lane.base, k);
    if (lane.confirmation30 && k.b.lanes && !k.b.lanes.includes(30)) return new Int8Array(k.b.n);
    // the higher enabled lanes (Bars.lanes): the ones laneInds generated the lane from, not the fixed ladder
    const ladder = isLowRangeInd(lane.base)
      ? (k.b.lanes ?? LOW_RANGE_TIMEFRAMES).filter((tf) =>
          (LOW_RANGE_TIMEFRAMES as readonly number[]).includes(tf) && (lane.confirmation30 || tf !== 30))
      : isInverseInd(lane.base)
      ? (k.b.lanes ?? INVERSE_TIMEFRAMES).filter((tf) =>
          (INVERSE_TIMEFRAMES as readonly number[]).includes(tf) && (lane.confirmation30 || tf !== 30),
        )
      : k.b.lanes;
    const f = higherFactors(k.b.tfMin, ladder);
    return f.length ? mtfState(lane.base, k, f) : indicationState(lane.base, k);
  }
  if (id === "none") return null;
  if (isSignalCoordinationInd(id))
    return k.memo(`ind:${id}`, () => signalCoordinationState(id, k, indicationState));
  const s = INDICATION_BY_ID.get(id);
  if (!s) throw new Error(`unknown indication ${id}`);
  return k.memo(`ind:${id}`, () => s.fn(k));
}

for (const id of COMBINED_BASE) {
  const base = [...BASE_INDICATIONS, ...VARIANTS].find((x) => x.id === id);
  if (!base) throw new Error(`combined base ${id} missing`);
  const c = spec(
    base.kind,
    `${id}@x4`,
    `${base.label} · 4× TF agrees`,
    { ...base.params, htf: 4 },
    (k) => mtfState(id, k, [4])!,
  );
  (INDICATIONS as IndicationSpec[]).push(c);
  (INDICATION_BY_ID as Map<string, IndicationSpec>).set(c.id, c);
}

/**
 * Combined timeframes: the indication's state on this timeframe, kept only where the SAME indication agrees on
 * every higher timeframe `factors` (× this one), computed from completed higher bars only.
 */
export function mtfState(id: string, k: SeriesCache, factors: readonly number[]): Int8Array | null {
  if (!factors.length) return indicationState(id, k);
  return k.memo(`mtf:${factors.join("x")}:${id}`, () => {
    const base = indicationState(id, k);
    if (!base) return null as unknown as Int8Array;
    const frame = k.streamMath?.frame<{ value: Int8Array }, number>(
      `mtf:${factors.join("x")}:${id}`,
      base.length,
      ["value"],
      () => 0,
      ["value"],
    );
    const out = frame?.cols.value ?? new Int8Array(base.length);
    const from = frame?.from ?? 0;
    out.set(base.subarray(from), from);
    for (const f of factors) {
      const { k: hk, map } = k.htf(f);
      const hs = indicationState(id, hk)!;
      for (let i = from; i < out.length; i++)
        if (out[i] !== 0 && (map[i] < 0 || hs[map[i]] !== out[i])) out[i] = 0;
    }
    frame?.commit(0);
    return out;
  });
}

// ── signal sources ──────────────────────────────────────────────────────────
// Proven classic signals, each in a short and a medium parameter range. A signal fires on its source's own
// state onsets ("follow"); it is processed as source × symbol with its own grid of 42 Normal and 35 Trailing
// configs (see src/core/signals.ts), separately from the engine's bot × indication combos. The specs reuse
// the registry's computations under their own ids ("sig-…"), so the lanes, stages and live execution apply
// unchanged while signal sets never collide with engine sets.
// (the lane's base is a prefix of the id: the id's own prefix answers, with no lane parse)
export const isSignalInd = (ind: string) => ind.startsWith("sig-");
/** Source name of a signal indication ("sig-ema-cross-s@m5" → "ema-cross"). */
export const signalSourceOf = (ind: string) => {
  const base = laneOf(ind).base;
  return isSignalCoordinationInd(base)
    ? signalCoordinationSourceLabel(base)
    : base.replace(/^sig-/, "").replace(/-[sm]$/, "");
};

for (const src of SIGNAL_SOURCES) {
  for (const range of ["short", "medium"] as const) {
    const base = INDICATION_BY_ID.get(range === "short" ? src.short : src.medium);
    if (!base)
      throw new Error(
        `signal source ${src.name} ${range}: ${range === "short" ? src.short : src.medium} missing`,
      );
    const c = spec(
      base.kind,
      signalId(src.name, range),
      `${src.label} · ${range}`,
      { ...base.params },
      base.fn,
    );
    (INDICATIONS as IndicationSpec[]).push(c);
    (INDICATION_BY_ID as Map<string, IndicationSpec>).set(c.id, c);
  }
}
