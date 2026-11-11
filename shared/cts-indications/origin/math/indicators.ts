// Indicator math on Float64Array columns. Every output[i] depends only on inputs[0..i].
// Warm-up values are NaN.

export type F64 = Float64Array;

export function sma(x: F64, p: number): F64 {
  const out = new Float64Array(x.length).fill(NaN);
  let s = 0;
  for (let i = 0; i < x.length; i++) {
    s += x[i];
    if (i >= p) s -= x[i - p];
    if (i >= p - 1) out[i] = s / p;
  }
  return out;
}

export function ema(x: F64, p: number): F64 {
  const out = new Float64Array(x.length).fill(NaN);
  const k = 2 / (p + 1);
  // the seed (SMA of the first p values) starts at the first finite value: an input with a warm-up prefix of NaN
  // (e.g. another average, as in a double-smoothed TSI) would otherwise stay NaN for good
  let start = 0;
  while (start < x.length && !Number.isFinite(x[start])) start++;
  let prev = NaN;
  let s = 0;
  for (let i = start; i < x.length; i++) {
    const j = i - start;
    if (j < p - 1) {
      s += x[i];
      continue;
    }
    if (j === p - 1) {
      prev = (s + x[i]) / p;
    } else {
      prev = x[i] * k + prev * (1 - k);
    }
    out[i] = prev;
  }
  return out;
}

/** Wilder smoothing (RMA). */
function rma(x: F64, p: number, start = 0): F64 {
  const out = new Float64Array(x.length).fill(NaN);
  let s = 0;
  let cnt = 0;
  let prev = NaN;
  for (let i = start; i < x.length; i++) {
    if (cnt < p) {
      s += x[i];
      cnt++;
      if (cnt === p) {
        prev = s / p;
        out[i] = prev;
      }
      continue;
    }
    prev = (prev * (p - 1) + x[i]) / p;
    out[i] = prev;
  }
  return out;
}

export function rsi(c: F64, p: number): F64 {
  const n = c.length;
  const up = new Float64Array(n);
  const dn = new Float64Array(n);
  for (let i = 1; i < n; i++) {
    const d = c[i] - c[i - 1];
    up[i] = d > 0 ? d : 0;
    dn[i] = d < 0 ? -d : 0;
  }
  const au = rma(up, p, 1);
  const ad = rma(dn, p, 1);
  const out = new Float64Array(n).fill(NaN);
  for (let i = 0; i < n; i++) {
    if (Number.isNaN(au[i])) continue;
    // no movement at all is neutral (50), not an extreme: a flat start of an illiquid series read as RSI 100
    out[i] = ad[i] === 0 ? (au[i] === 0 ? 50 : 100) : 100 - 100 / (1 + au[i] / ad[i]);
  }
  return out;
}

export function trueRange(h: F64, l: F64, c: F64): F64 {
  const n = c.length;
  const tr = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const hl = h[i] - l[i];
    if (i === 0) {
      tr[i] = hl;
      continue;
    }
    tr[i] = Math.max(hl, Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1]));
  }
  return tr;
}

export function atr(h: F64, l: F64, c: F64, p: number): F64 {
  return rma(trueRange(h, l, c), p);
}

/** ATR as an EMA of the true range (the Stable-02 desk's ATR; `atr` above is Wilder's). */
export function atrEma(h: F64, l: F64, c: F64, p: number): F64 {
  return ema(trueRange(h, l, c), p);
}

export function macd(c: F64, fast = 12, slow = 26, sig = 9): { line: F64; signal: F64; hist: F64 } {
  const ef = ema(c, fast);
  const es = ema(c, slow);
  const n = c.length;
  const line = new Float64Array(n).fill(NaN);
  for (let i = 0; i < n; i++) line[i] = ef[i] - es[i];
  const first = line.findIndex((v) => !Number.isNaN(v));
  const signal = new Float64Array(n).fill(NaN);
  if (first >= 0) {
    const sub = ema(line.subarray(first), sig);
    signal.set(sub, first);
  }
  const hist = new Float64Array(n).fill(NaN);
  for (let i = 0; i < n; i++) hist[i] = line[i] - signal[i];
  return { line, signal, hist };
}

export function stdev(x: F64, p: number): F64 {
  const out = new Float64Array(x.length).fill(NaN);
  let s = 0;
  let s2 = 0;
  for (let i = 0; i < x.length; i++) {
    s += x[i];
    s2 += x[i] * x[i];
    if (i >= p) {
      s -= x[i - p];
      s2 -= x[i - p] * x[i - p];
    }
    if (i >= p - 1) {
      const m = s / p;
      out[i] = Math.sqrt(Math.max(0, s2 / p - m * m));
    }
  }
  return out;
}

export function bollinger(c: F64, p = 20, k = 2): { mid: F64; up: F64; lo: F64; width: F64 } {
  const mid = sma(c, p);
  const sd = stdev(c, p);
  const n = c.length;
  const up = new Float64Array(n);
  const lo = new Float64Array(n);
  const width = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    up[i] = mid[i] + k * sd[i];
    lo[i] = mid[i] - k * sd[i];
    width[i] = (up[i] - lo[i]) / mid[i];
  }
  return { mid, up, lo, width };
}

/** Parabolic SAR. Returns sar level and direction (+1 long / -1 short). */
export function psar(h: F64, l: F64, step = 0.02, max = 0.2): { sar: F64; dir: Int8Array } {
  const n = h.length;
  const sar = new Float64Array(n).fill(NaN);
  const dir = new Int8Array(n);
  if (n < 2) return { sar, dir };
  let up = h[1] >= h[0];
  let ep = up ? h[1] : l[1];
  let s = up ? l[0] : h[0];
  let af = step;
  for (let i = 1; i < n; i++) {
    s = s + af * (ep - s);
    if (up) {
      s = Math.min(s, l[i - 1], i > 1 ? l[i - 2] : l[i - 1]);
      if (l[i] < s) {
        up = false;
        s = ep;
        ep = l[i];
        af = step;
      } else if (h[i] > ep) {
        ep = h[i];
        af = Math.min(max, af + step);
      }
    } else {
      s = Math.max(s, h[i - 1], i > 1 ? h[i - 2] : h[i - 1]);
      if (h[i] > s) {
        up = true;
        s = ep;
        ep = h[i];
        af = step;
      } else if (l[i] < ep) {
        ep = l[i];
        af = Math.min(max, af + step);
      }
    }
    sar[i] = s;
    dir[i] = up ? 1 : -1;
  }
  return { sar, dir };
}

export function dmi(h: F64, l: F64, c: F64, p = 14): { adx: F64; pdi: F64; mdi: F64 } {
  const n = c.length;
  const pdm = new Float64Array(n);
  const mdm = new Float64Array(n);
  for (let i = 1; i < n; i++) {
    const u = h[i] - h[i - 1];
    const d = l[i - 1] - l[i];
    pdm[i] = u > d && u > 0 ? u : 0;
    mdm[i] = d > u && d > 0 ? d : 0;
  }
  const tr = trueRange(h, l, c);
  const str = rma(tr, p, 1);
  const sp = rma(pdm, p, 1);
  const sm = rma(mdm, p, 1);
  const pdi = new Float64Array(n).fill(NaN);
  const mdi = new Float64Array(n).fill(NaN);
  const dx = new Float64Array(n).fill(NaN);
  let firstDx = -1;
  for (let i = 0; i < n; i++) {
    if (Number.isNaN(str[i]) || str[i] === 0) continue;
    pdi[i] = (100 * sp[i]) / str[i];
    mdi[i] = (100 * sm[i]) / str[i];
    const s = pdi[i] + mdi[i];
    dx[i] = s === 0 ? 0 : (100 * Math.abs(pdi[i] - mdi[i])) / s;
    if (firstDx < 0) firstDx = i;
  }
  const adx = firstDx < 0 ? new Float64Array(n).fill(NaN) : rma(dx, p, firstDx);
  return { adx, pdi, mdi };
}

/** Rolling VWAP over the last p bars (typical price). */
export function vwapRolling(h: F64, l: F64, c: F64, v: F64, p: number): F64 {
  const n = c.length;
  const out = new Float64Array(n).fill(NaN);
  let pv = 0;
  let vv = 0;
  for (let i = 0; i < n; i++) {
    const tp = (h[i] + l[i] + c[i]) / 3;
    const vol = v[i] > 0 ? v[i] : 1e-9;
    pv += tp * vol;
    vv += vol;
    if (i >= p) {
      const tq = (h[i - p] + l[i - p] + c[i - p]) / 3;
      const vq = v[i - p] > 0 ? v[i - p] : 1e-9;
      pv -= tq * vq;
      vv -= vq;
    }
    if (i >= p - 1) out[i] = pv / vv;
  }
  return out;
}

export function supertrend(
  h: F64,
  l: F64,
  c: F64,
  p = 10,
  mult = 3,
): { line: F64; dir: Int8Array } {
  const n = c.length;
  const a = atr(h, l, c, p);
  const line = new Float64Array(n).fill(NaN);
  const dir = new Int8Array(n);
  let fu = NaN;
  let fl = NaN;
  let d: 1 | -1 = 1;
  for (let i = 0; i < n; i++) {
    if (Number.isNaN(a[i])) continue;
    const mid = (h[i] + l[i]) / 2;
    const bu = mid + mult * a[i];
    const bl = mid - mult * a[i];
    const pc = i > 0 ? c[i - 1] : c[i];
    fu = Number.isNaN(fu) || bu < fu || pc > fu ? bu : fu;
    fl = Number.isNaN(fl) || bl > fl || pc < fl ? bl : fl;
    if (d === 1 && c[i] < fl) d = -1;
    else if (d === -1 && c[i] > fu) d = 1;
    line[i] = d === 1 ? fl : fu;
    dir[i] = d;
  }
  return { line, dir };
}

/** Donchian channel of the p bars BEFORE bar i (excludes the current bar). */
export function donchianPrior(h: F64, l: F64, p: number): { hi: F64; lo: F64 } {
  const n = h.length;
  const hi = new Float64Array(n).fill(NaN);
  const lo = new Float64Array(n).fill(NaN);
  // monotonic deques for O(n)
  const dh: number[] = [];
  const dl: number[] = [];
  for (let i = 0; i < n; i++) {
    if (i >= p) {
      hi[i] = h[dh[0]];
      lo[i] = l[dl[0]];
    }
    while (dh.length && h[dh[dh.length - 1]] <= h[i]) dh.pop();
    dh.push(i);
    if (dh[0] <= i - p) dh.shift();
    while (dl.length && l[dl[dl.length - 1]] >= l[i]) dl.pop();
    dl.push(i);
    if (dl[0] <= i - p) dl.shift();
  }
  return { hi, lo };
}

export function roc(c: F64, p: number): F64 {
  const out = new Float64Array(c.length).fill(NaN);
  for (let i = p; i < c.length; i++) out[i] = c[i] / c[i - p] - 1;
  return out;
}

export function stoch(h: F64, l: F64, c: F64, p = 14, d = 3): { k: F64; d: F64 } {
  const n = c.length;
  const k = new Float64Array(n).fill(NaN);
  for (let i = p - 1; i < n; i++) {
    let hh = -Infinity;
    let ll = Infinity;
    for (let j = i - p + 1; j <= i; j++) {
      if (h[j] > hh) hh = h[j];
      if (l[j] < ll) ll = l[j];
    }
    k[i] = hh === ll ? 50 : ((c[i] - ll) / (hh - ll)) * 100;
  }
  const first = p - 1;
  const dd = new Float64Array(n).fill(NaN);
  if (n > first) dd.set(sma(k.subarray(first), d), first);
  return { k, d: dd };
}

/** Per-bar anchored-period levels: open/VWAP/high/low/close of the current and the previous period (e.g. hour). */
export function periodLevels(
  t: F64,
  h: F64,
  l: F64,
  c: F64,
  o: F64,
  v: F64,
  periodMs: number,
): {
  curOpen: F64;
  curVwap: F64;
  curRange: F64;
  prevVwap: F64;
  prevHigh: F64;
  prevLow: F64;
  prevPivot: F64;
  prevRange: F64;
} {
  const n = c.length;
  const mk = () => new Float64Array(n).fill(NaN);
  const curOpen = mk();
  const curVwap = mk();
  const curRange = mk();
  const prevVwap = mk();
  const prevHigh = mk();
  const prevLow = mk();
  const prevPivot = mk();
  const prevRange = mk();
  let pid = -1;
  let po = NaN;
  let pv = 0;
  let vv = 0;
  let ph = -Infinity;
  let pl = Infinity;
  let pc = NaN;
  let lastV = NaN;
  let lastH = NaN;
  let lastL = NaN;
  let lastC = NaN;
  for (let i = 0; i < n; i++) {
    const id = Math.floor(t[i] / periodMs);
    if (id !== pid) {
      if (pid >= 0) {
        lastV = vv > 0 ? pv / vv : NaN;
        lastH = ph;
        lastL = pl;
        lastC = pc;
      }
      pid = id;
      po = o[i];
      pv = 0;
      vv = 0;
      ph = -Infinity;
      pl = Infinity;
    }
    const tp = (h[i] + l[i] + c[i]) / 3;
    const vol = v[i] > 0 ? v[i] : 1e-9;
    pv += tp * vol;
    vv += vol;
    if (h[i] > ph) ph = h[i];
    if (l[i] < pl) pl = l[i];
    pc = c[i];
    curOpen[i] = po;
    curVwap[i] = pv / vv;
    curRange[i] = ph - pl;
    prevVwap[i] = lastV;
    prevHigh[i] = lastH;
    prevLow[i] = lastL;
    prevPivot[i] = (lastH + lastL + lastC) / 3;
    prevRange[i] = lastH - lastL;
  }
  return { curOpen, curVwap, curRange, prevVwap, prevHigh, prevLow, prevPivot, prevRange };
}

// ── common oscillators, volume, channels, Ichimoku, smoothers (all causal) ─────────────

export function wma(x: F64, p: number): F64 {
  const out = new Float64Array(x.length).fill(NaN);
  const den = (p * (p + 1)) / 2;
  let run = 0;
  for (let i = 0; i < x.length; i++) {
    if (!Number.isFinite(x[i])) {
      run = 0;
      continue;
    }
    run++;
    if (run < p) continue;
    let s = 0;
    for (let j = 0; j < p; j++) s += x[i - j] * (p - j);
    out[i] = s / den;
  }
  return out;
}

/** Hull moving average. */
export function hma(x: F64, p: number): F64 {
  const a = wma(x, Math.max(1, Math.round(p / 2)));
  const b = wma(x, p);
  const d = new Float64Array(x.length);
  for (let i = 0; i < x.length; i++) d[i] = 2 * a[i] - b[i];
  return wma(d, Math.max(1, Math.round(Math.sqrt(p))));
}

/** Commodity Channel Index. */
export function cci(h: F64, l: F64, c: F64, p = 20): F64 {
  const n = c.length;
  const tp = new Float64Array(n);
  for (let i = 0; i < n; i++) tp[i] = (h[i] + l[i] + c[i]) / 3;
  const m = sma(tp, p);
  const out = new Float64Array(n).fill(NaN);
  for (let i = p - 1; i < n; i++) {
    let md = 0;
    for (let j = i - p + 1; j <= i; j++) md += Math.abs(tp[j] - m[i]);
    md /= p;
    out[i] = md > 0 ? (tp[i] - m[i]) / (0.015 * md) : 0;
  }
  return out;
}

/** Williams %R (−100 … 0). */
export function willr(h: F64, l: F64, c: F64, p = 14): F64 {
  const s = stoch(h, l, c, p, 1).k;
  const out = new Float64Array(c.length);
  for (let i = 0; i < c.length; i++) out[i] = s[i] - 100;
  return out;
}

/** Money Flow Index. */
export function mfi(h: F64, l: F64, c: F64, v: F64, p = 14): F64 {
  const n = c.length;
  const out = new Float64Array(n).fill(NaN);
  const tp = new Float64Array(n);
  for (let i = 0; i < n; i++) tp[i] = (h[i] + l[i] + c[i]) / 3;
  for (let i = p; i < n; i++) {
    let pos = 0;
    let neg = 0;
    for (let j = i - p + 1; j <= i; j++) {
      const f = tp[j] * v[j];
      if (tp[j] > tp[j - 1]) pos += f;
      else if (tp[j] < tp[j - 1]) neg += f;
    }
    out[i] = neg === 0 ? 100 : 100 - 100 / (1 + pos / neg);
  }
  return out;
}

export function obv(c: F64, v: F64): F64 {
  const out = new Float64Array(c.length);
  for (let i = 1; i < c.length; i++)
    out[i] = out[i - 1] + (c[i] > c[i - 1] ? v[i] : c[i] < c[i - 1] ? -v[i] : 0);
  return out;
}

/** Chaikin Money Flow. */
export function cmf(h: F64, l: F64, c: F64, v: F64, p = 20): F64 {
  const n = c.length;
  const mfv = new Float64Array(n);
  for (let i = 0; i < n; i++)
    mfv[i] = h[i] > l[i] ? ((c[i] - l[i] - (h[i] - c[i])) / (h[i] - l[i])) * v[i] : 0;
  const a = sma(mfv, p);
  const b = sma(v, p);
  const out = new Float64Array(n).fill(NaN);
  for (let i = 0; i < n; i++) if (b[i] > 0) out[i] = a[i] / b[i];
  return out;
}

/** Stochastic RSI %K (0…100). */
export function stochRsi(c: F64, rp = 14, sp = 14, smooth = 3): F64 {
  const r = rsi(c, rp);
  const n = c.length;
  const raw = new Float64Array(n).fill(NaN);
  for (let i = 0; i < n; i++) {
    if (i < sp - 1) continue;
    let hh = -Infinity;
    let ll = Infinity;
    let okAll = true;
    for (let j = i - sp + 1; j <= i; j++) {
      if (!Number.isFinite(r[j])) {
        okAll = false;
        break;
      }
      if (r[j] > hh) hh = r[j];
      if (r[j] < ll) ll = r[j];
    }
    if (okAll) raw[i] = hh === ll ? 50 : ((r[i] - ll) / (hh - ll)) * 100;
  }
  const first = raw.findIndex((x) => Number.isFinite(x));
  const out = new Float64Array(n).fill(NaN);
  if (first >= 0) out.set(sma(raw.subarray(first), smooth), first);
  return out;
}

/** Aroon up / down (0…100). */
export function aroon(h: F64, l: F64, p = 25): { up: F64; dn: F64 } {
  const n = h.length;
  const up = new Float64Array(n).fill(NaN);
  const dn = new Float64Array(n).fill(NaN);
  for (let i = p; i < n; i++) {
    let hi = i,
      lo = i;
    for (let j = i - p; j <= i; j++) {
      if (h[j] >= h[hi]) hi = j;
      if (l[j] <= l[lo]) lo = j;
    }
    up[i] = ((p - (i - hi)) / p) * 100;
    dn[i] = ((p - (i - lo)) / p) * 100;
  }
  return { up, dn };
}

/** Keltner channel: EMA(p) ± m × ATR(p). */
export function keltner(h: F64, l: F64, c: F64, p = 20, m = 2): { mid: F64; up: F64; lo: F64 } {
  const mid = ema(c, p);
  const a = atr(h, l, c, p);
  const up = new Float64Array(c.length);
  const lo = new Float64Array(c.length);
  for (let i = 0; i < c.length; i++) {
    up[i] = mid[i] + m * a[i];
    lo[i] = mid[i] - m * a[i];
  }
  return { mid, up, lo };
}

function midRange(h: F64, l: F64, p: number): F64 {
  const n = h.length;
  const out = new Float64Array(n).fill(NaN);
  for (let i = p - 1; i < n; i++) {
    let hh = -Infinity;
    let ll = Infinity;
    for (let j = i - p + 1; j <= i; j++) {
      if (h[j] > hh) hh = h[j];
      if (l[j] < ll) ll = l[j];
    }
    out[i] = (hh + ll) / 2;
  }
  return out;
}

/** Ichimoku; span A/B are the values plotted at bar i (computed `shift` bars earlier), so they are causal. */
export function ichimoku(
  h: F64,
  l: F64,
  t = 9,
  k = 26,
  b = 52,
  shift = 26,
): { tenkan: F64; kijun: F64; spanA: F64; spanB: F64 } {
  const n = h.length;
  const tenkan = midRange(h, l, t);
  const kijun = midRange(h, l, k);
  const sb = midRange(h, l, b);
  const spanA = new Float64Array(n).fill(NaN);
  const spanB = new Float64Array(n).fill(NaN);
  for (let i = shift; i < n; i++) {
    spanA[i] = (tenkan[i - shift] + kijun[i - shift]) / 2;
    spanB[i] = sb[i - shift];
  }
  return { tenkan, kijun, spanA, spanB };
}

/** TRIX: 1-bar rate of change of a triple EMA, in %. */
export function trix(c: F64, p = 15): F64 {
  const e1 = ema(c, p);
  const f = e1.findIndex((x) => Number.isFinite(x));
  const e2 = new Float64Array(c.length).fill(NaN);
  if (f >= 0) e2.set(ema(e1.subarray(f), p), f);
  const f2 = e2.findIndex((x) => Number.isFinite(x));
  const e3 = new Float64Array(c.length).fill(NaN);
  if (f2 >= 0) e3.set(ema(e2.subarray(f2), p), f2);
  const out = new Float64Array(c.length).fill(NaN);
  for (let i = 1; i < c.length; i++)
    if (Number.isFinite(e3[i - 1])) out[i] = (e3[i] / e3[i - 1] - 1) * 100;
  return out;
}

/** Kaufman adaptive moving average. */
export function kama(c: F64, p = 10, fast = 2, slow = 30): F64 {
  const n = c.length;
  const out = new Float64Array(n).fill(NaN);
  const fs = 2 / (fast + 1);
  const ss = 2 / (slow + 1);
  let prev = NaN;
  for (let i = p; i < n; i++) {
    const change = Math.abs(c[i] - c[i - p]);
    let vol = 0;
    for (let j = i - p + 1; j <= i; j++) vol += Math.abs(c[j] - c[j - 1]);
    const er = vol > 0 ? change / vol : 0;
    const sc = (er * (fs - ss) + ss) ** 2;
    prev = Number.isFinite(prev) ? prev + sc * (c[i] - prev) : c[i];
    out[i] = prev;
  }
  return out;
}

/** Heikin-Ashi open / close. */
export function heikinAshi(o: F64, h: F64, l: F64, c: F64): { o: F64; c: F64 } {
  const n = c.length;
  const ho = new Float64Array(n);
  const hc = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    hc[i] = (o[i] + h[i] + l[i] + c[i]) / 4;
    ho[i] = i === 0 ? (o[0] + c[0]) / 2 : (ho[i - 1] + hc[i - 1]) / 2;
  }
  return { o: ho, c: hc };
}

/** Rolling z-score of x. */
export function zscore(x: F64, p = 20): F64 {
  const m = sma(x, p);
  const s = stdev(x, p);
  const out = new Float64Array(x.length).fill(NaN);
  for (let i = 0; i < x.length; i++) if (s[i] > 0) out[i] = (x[i] - m[i]) / s[i];
  return out;
}
