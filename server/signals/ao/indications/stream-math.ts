import type { Bars } from "../domain/types.ts";

type Vec = Float64Array | Int8Array | Int32Array;
type RecordState = { n: number; cap: number; cols: Record<string, Vec>; state: unknown };

/** Private append-only numeric state. Full calculations remain the oracle; no truncated warmup or approximation.
 * Published prefixes are immutable, capacities grow geometrically and each economic generation owns its state. */
export class StreamMath {
  private records = new Map<string, RecordState>();
  private children = new Map<string, StreamMath>();
  child(key: string): StreamMath {
    let child = this.children.get(key);
    if (!child) this.children.set(key, (child = new StreamMath()));
    return child;
  }
  frame<R extends Record<string, Vec>, S>(
    key: string,
    n: number,
    names: readonly string[],
    initial: () => S,
    ints: readonly string[] = [],
    wideInts: readonly string[] = [],
  ) {
    let record = this.records.get(key);
    if (record && n < record.n) throw new Error(`stream math history shortened: ${key}`);
    if (!record) {
      record = { n: 0, cap: 0, cols: {}, state: initial() };
      this.records.set(key, record);
    }
    if (record.cap < n || !record.cols[names[0]]) {
      const cap = Math.max(n, Math.ceil(n * 1.25) + 64);
      for (const name of names) {
        const col = wideInts.includes(name)
          ? new Int32Array(cap)
          : ints.includes(name)
            ? new Int8Array(cap)
            : new Float64Array(cap).fill(NaN);
        if (record.cols[name]) col.set(record.cols[name]);
        record.cols[name] = col;
      }
      record.cap = cap;
    }
    return {
      from: record.n,
      state: record.state as S,
      cols: Object.fromEntries(names.map((name) => [name, record!.cols[name].subarray(0, n)])) as R,
      commit: (state: S) => {
        record!.state = state;
        record!.n = n;
      },
    };
  }
  run<R extends Record<string, Vec>, S>(
    key: string,
    n: number,
    names: readonly string[],
    initial: () => S,
    step: (i: number, cols: R, state: S) => void,
    ints: readonly string[] = [],
  ): R {
    let record = this.records.get(key);
    if (record && n < record.n) throw new Error(`stream math history shortened: ${key}`);
    if (!record) {
      record = { n: 0, cap: 0, cols: {}, state: initial() };
      this.records.set(key, record);
    }
    if (record.cap < n || !record.cols[names[0]]) {
      const cap = Math.max(n, Math.ceil(n * 1.25) + 64);
      for (const name of names) {
        const column = ints.includes(name) ? new Int8Array(cap) : new Float64Array(cap).fill(NaN);
        if (record.cols[name]) column.set(record.cols[name]);
        record.cols[name] = column;
      }
      record.cap = cap;
    }
    for (let i = record.n; i < n; i++) step(i, record.cols as R, record.state as S);
    record.n = n;
    return Object.fromEntries(names.map((name) => [name, record!.cols[name].subarray(0, n)])) as R;
  }
  vector<S>(
    key: string,
    n: number,
    initial: () => S,
    step: (i: number, out: Float64Array, state: S) => void,
  ) {
    return this.run<{ value: Float64Array }, S>(key, n, ["value"], initial, (i, cols, state) =>
      step(i, cols.value, state),
    ).value;
  }
  /** Same completed-bucket aggregation and index mapping as htfBars, retaining an unfinished bucket. */
  higher(b: Bars, factor: number): { hb: Bars; map: Int32Array } {
    const tfMs = b.tfMin * 60_000,
      span = tfMs * factor;
    const names = ["t", "o", "h", "l", "c", "v"] as const;
    const frame = this.frame<
      { map: Int32Array },
      {
        cur: number;
        bo: number;
        bh: number;
        bl: number;
        bc: number;
        bv: number;
        bt: number;
        count: number;
        complete: boolean;
        lastT: number;
        published: number;
      }
    >(
      `higher:${factor}`,
      b.n,
      ["map"],
      () => ({
        cur: NaN,
        bo: 0,
        bh: 0,
        bl: 0,
        bc: 0,
        bv: 0,
        bt: 0,
        count: 0,
        complete: false,
        lastT: NaN,
        published: 0,
      }),
      [],
      ["map"],
    );
    // OHLCV capacity follows higher-bar count, not base history length; only the index map spans base bars.
    const barsFrame = this.frame<Record<(typeof names)[number], Float64Array>, number>(
      `higher-bars:${factor}`,
      Math.ceil(b.n / factor),
      names,
      () => 0,
    );
    const x = barsFrame.cols,
      s = frame.state;
    for (let i = frame.from; i < b.n; i++) {
      if (i > 0 && !(b.t[i] > s.lastT))
        throw new RangeError("source bars must have strictly increasing timestamps");
      const bucket = Math.floor(b.t[i] / span);
      if (bucket !== s.cur) {
        s.cur = bucket;
        s.bt = bucket * span;
        s.bo = b.o[i];
        s.bh = b.h[i];
        s.bl = b.l[i];
        s.bc = b.c[i];
        s.bv = b.v[i];
        s.count = 1;
        s.complete = b.t[i] === s.bt;
      } else {
        if (b.h[i] > s.bh) s.bh = b.h[i];
        if (b.l[i] < s.bl) s.bl = b.l[i];
        s.bc = b.c[i];
        s.bv += b.v[i];
        s.count++;
        s.complete = s.complete && b.t[i] === s.lastT + tfMs;
      }
      s.lastT = b.t[i];
      if (s.complete && s.count === factor && b.t[i] + tfMs === s.bt + span) {
        const j = s.published++;
        x.t[j] = s.bt;
        x.o[j] = s.bo;
        x.h[j] = s.bh;
        x.l[j] = s.bl;
        x.c[j] = s.bc;
        x.v[j] = s.bv;
      }
      frame.cols.map[i] = s.published - 1;
    }
    frame.commit(s);
    barsFrame.commit(0);
    return {
      hb: {
        sym: b.sym,
        tfMin: b.tfMin * factor,
        n: s.published,
        t: x.t.subarray(0, s.published),
        o: x.o.subarray(0, s.published),
        h: x.h.subarray(0, s.published),
        l: x.l.subarray(0, s.published),
        c: x.c.subarray(0, s.published),
        v: x.v.subarray(0, s.published),
      },
      map: frame.cols.map,
    };
  }
  ema(x: Float64Array, p: number, key = "close") {
    const alpha = 2 / (p + 1);
    return this.vector(
      `ema:${key}:${p}`,
      x.length,
      () => ({ start: -1, sum: 0, prev: NaN }),
      (i, out, s) => {
        if (s.start < 0) {
          if (!Number.isFinite(x[i])) return;
          s.start = i;
        }
        const j = i - s.start;
        if (j < p - 1) {
          s.sum += x[i];
          return;
        }
        s.prev = j === p - 1 ? (s.sum + x[i]) / p : x[i] * alpha + s.prev * (1 - alpha);
        out[i] = s.prev;
      },
    );
  }
  rma(x: Float64Array, p: number, start: number, key: string) {
    return this.vector(
      `rma:${key}:${p}:${start}`,
      x.length,
      () => ({ sum: 0, count: 0, prev: NaN }),
      (i, out, s) => {
        if (i < start) return;
        if (s.count < p) {
          s.sum += x[i];
          if (++s.count === p) out[i] = s.prev = s.sum / p;
          return;
        }
        out[i] = s.prev = (s.prev * (p - 1) + x[i]) / p;
      },
    );
  }
  sma(x: Float64Array, p: number, key = "close", start = 0) {
    return this.vector(
      `sma:${key}:${p}:${start}`,
      x.length,
      () => ({ sum: 0 }),
      (i, out, s) => {
        if (i < start) return;
        s.sum += x[i];
        if (i - start >= p) s.sum -= x[i - p];
        if (i - start >= p - 1) out[i] = s.sum / p;
      },
    );
  }
  stdev(x: Float64Array, p: number, key = "close") {
    return this.vector(
      `sd:${key}:${p}`,
      x.length,
      () => ({ sum: 0, squares: 0 }),
      (i, out, s) => {
        s.sum += x[i];
        s.squares += x[i] * x[i];
        if (i >= p) {
          s.sum -= x[i - p];
          s.squares -= x[i - p] * x[i - p];
        }
        if (i >= p - 1) {
          const mean = s.sum / p;
          out[i] = Math.sqrt(Math.max(0, s.squares / p - mean * mean));
        }
      },
    );
  }
  z(x: Float64Array, p: number, key = "close") {
    const mean = this.sma(x, p, key),
      sd = this.stdev(x, p, key);
    return this.vector(
      `z:${key}:${p}`,
      x.length,
      () => 0,
      (i, out) => {
        if (sd[i] > 0) out[i] = (x[i] - mean[i]) / sd[i];
      },
    );
  }
  tr(b: Bars) {
    return this.vector(
      "true-range",
      b.n,
      () => 0,
      (i, out) => {
        const range = b.h[i] - b.l[i];
        out[i] = i
          ? Math.max(range, Math.abs(b.h[i] - b.c[i - 1]), Math.abs(b.l[i] - b.c[i - 1]))
          : range;
      },
    );
  }
  atr(b: Bars, p: number) {
    return this.rma(this.tr(b), p, 0, "atr");
  }
  atrEma(b: Bars, p: number) {
    return this.ema(this.tr(b), p, "atr");
  }
  rsi(c: Float64Array, p: number) {
    const delta = this.run<{ up: Float64Array; down: Float64Array }, number>(
      "rsi-deltas",
      c.length,
      ["up", "down"],
      () => 0,
      (i, out) => {
        const d = i ? c[i] - c[i - 1] : 0;
        out.up[i] = d > 0 ? d : 0;
        out.down[i] = d < 0 ? -d : 0;
      },
    );
    const up = this.rma(delta.up, p, 1, "rsi-up"),
      down = this.rma(delta.down, p, 1, "rsi-down");
    return this.vector(
      `rsi:${p}`,
      c.length,
      () => 0,
      (i, out) => {
        if (!Number.isNaN(up[i]))
          out[i] = down[i] === 0 ? (up[i] === 0 ? 50 : 100) : 100 - 100 / (1 + up[i] / down[i]);
      },
    );
  }
  macd(c: Float64Array, fast: number, slow: number, period: number) {
    const ef = this.ema(c, fast),
      es = this.ema(c, slow),
      key = `${fast}:${slow}:${period}`;
    const line = this.vector(
      `macd-line:${key}`,
      c.length,
      () => 0,
      (i, out) => {
        out[i] = ef[i] - es[i];
      },
    );
    const signal = this.ema(line, period, `macd-line:${key}`);
    const hist = this.vector(
      `macd-hist:${key}`,
      c.length,
      () => 0,
      (i, out) => {
        out[i] = line[i] - signal[i];
      },
    );
    return { line, signal, hist };
  }
  bb(c: Float64Array, p: number, k: number) {
    const mid = this.sma(c, p),
      sd = this.stdev(c, p);
    const cols = this.run<{ up: Float64Array; lo: Float64Array; width: Float64Array }, number>(
      `bb:${p}:${k}`,
      c.length,
      ["up", "lo", "width"],
      () => 0,
      (i, out) => {
        out.up[i] = mid[i] + k * sd[i];
        out.lo[i] = mid[i] - k * sd[i];
        out.width[i] = (out.up[i] - out.lo[i]) / mid[i];
      },
    );
    return { mid, ...cols };
  }
  dmi(b: Bars, p: number) {
    const dm = this.run<{ plus: Float64Array; minus: Float64Array }, number>(
      "dmi-deltas",
      b.n,
      ["plus", "minus"],
      () => 0,
      (i, out) => {
        const up = i ? b.h[i] - b.h[i - 1] : 0,
          down = i ? b.l[i - 1] - b.l[i] : 0;
        out.plus[i] = up > down && up > 0 ? up : 0;
        out.minus[i] = down > up && down > 0 ? down : 0;
      },
    );
    const tr = this.rma(this.tr(b), p, 1, "dmi-tr"),
      plus = this.rma(dm.plus, p, 1, "dmi-plus"),
      minus = this.rma(dm.minus, p, 1, "dmi-minus");
    const key = `dmi:${p}`;
    const cols = this.run<
      { pdi: Float64Array; mdi: Float64Array; dx: Float64Array },
      { first: number }
    >(
      key,
      b.n,
      ["pdi", "mdi", "dx"],
      () => ({ first: -1 }),
      (i, out, state) => {
        if (Number.isNaN(tr[i]) || tr[i] === 0) return;
        out.pdi[i] = (100 * plus[i]) / tr[i];
        out.mdi[i] = (100 * minus[i]) / tr[i];
        const sum = out.pdi[i] + out.mdi[i];
        out.dx[i] = sum === 0 ? 0 : (100 * Math.abs(out.pdi[i] - out.mdi[i])) / sum;
        if (state.first < 0) state.first = i;
      },
    );
    const first = (this.records.get(key)!.state as { first: number }).first;
    const adx =
      first < 0 ? new Float64Array(b.n).fill(NaN) : this.rma(cols.dx, p, first, `adx:${p}`);
    return { adx, pdi: cols.pdi, mdi: cols.mdi };
  }
  don(b: Bars, p: number) {
    return this.run<{ hi: Float64Array; lo: Float64Array }, { hi: number[]; lo: number[] }>(
      `don:${p}`,
      b.n,
      ["hi", "lo"],
      () => ({ hi: [], lo: [] }),
      (i, out, s) => {
        if (i >= p) {
          out.hi[i] = b.h[s.hi[0]];
          out.lo[i] = b.l[s.lo[0]];
        }
        while (s.hi.length && b.h[s.hi.at(-1)!] <= b.h[i]) s.hi.pop();
        s.hi.push(i);
        if (s.hi[0] <= i - p) s.hi.shift();
        while (s.lo.length && b.l[s.lo.at(-1)!] >= b.l[i]) s.lo.pop();
        s.lo.push(i);
        if (s.lo[0] <= i - p) s.lo.shift();
      },
    );
  }
  vwap(b: Bars, p: number) {
    return this.vector(
      `vwap:${p}`,
      b.n,
      () => ({ pv: 0, vv: 0 }),
      (i, out, s) => {
        const vol = b.v[i] > 0 ? b.v[i] : 1e-9;
        s.pv += ((b.h[i] + b.l[i] + b.c[i]) / 3) * vol;
        s.vv += vol;
        if (i >= p) {
          const old = i - p,
            oldVol = b.v[old] > 0 ? b.v[old] : 1e-9;
          s.pv -= ((b.h[old] + b.l[old] + b.c[old]) / 3) * oldVol;
          s.vv -= oldVol;
        }
        if (i >= p - 1) out[i] = s.pv / s.vv;
      },
    );
  }
  stoch(b: Bars, p: number, d: number) {
    const key = `stoch:${p}:${d}`;
    const k = this.vector(
      key,
      b.n,
      () => 0,
      (i, out) => {
        if (i < p - 1) return;
        let hi = -Infinity,
          lo = Infinity;
        for (let j = i - p + 1; j <= i; j++) {
          if (b.h[j] > hi) hi = b.h[j];
          if (b.l[j] < lo) lo = b.l[j];
        }
        out[i] = hi === lo ? 50 : ((b.c[i] - lo) / (hi - lo)) * 100;
      },
    );
    return { k, d: this.sma(k, d, key, p - 1) };
  }
  psar(b: Bars, step: number, max: number) {
    if (b.n < 2) return { sar: new Float64Array(b.n).fill(NaN), dir: new Int8Array(b.n) };
    return this.run<
      { sar: Float64Array; dir: Int8Array },
      { up: boolean; ep: number; sar: number; af: number }
    >(
      `sar:${step}:${max}`,
      b.n,
      ["sar", "dir"],
      () => ({
        up: b.h[1] >= b.h[0],
        ep: b.h[1] >= b.h[0] ? b.h[1] : b.l[1],
        sar: b.h[1] >= b.h[0] ? b.l[0] : b.h[0],
        af: step,
      }),
      (i, out, s) => {
        if (!i) return;
        s.sar += s.af * (s.ep - s.sar);
        if (s.up) {
          s.sar = Math.min(s.sar, b.l[i - 1], i > 1 ? b.l[i - 2] : b.l[i - 1]);
          if (b.l[i] < s.sar) {
            s.up = false;
            s.sar = s.ep;
            s.ep = b.l[i];
            s.af = step;
          } else if (b.h[i] > s.ep) {
            s.ep = b.h[i];
            s.af = Math.min(max, s.af + step);
          }
        } else {
          s.sar = Math.max(s.sar, b.h[i - 1], i > 1 ? b.h[i - 2] : b.h[i - 1]);
          if (b.h[i] > s.sar) {
            s.up = true;
            s.sar = s.ep;
            s.ep = b.h[i];
            s.af = step;
          } else if (b.l[i] < s.ep) {
            s.ep = b.l[i];
            s.af = Math.min(max, s.af + step);
          }
        }
        out.sar[i] = s.sar;
        out.dir[i] = s.up ? 1 : -1;
      },
      ["dir"],
    );
  }
  supertrend(b: Bars, p: number, mult: number) {
    const atr = this.atr(b, p);
    return this.run<
      { line: Float64Array; dir: Int8Array },
      { up: number; low: number; dir: number }
    >(
      `supertrend:${p}:${mult}`,
      b.n,
      ["line", "dir"],
      () => ({ up: NaN, low: NaN, dir: 1 }),
      (i, out, s) => {
        if (Number.isNaN(atr[i])) return;
        const mid = (b.h[i] + b.l[i]) / 2,
          up = mid + mult * atr[i],
          low = mid - mult * atr[i];
        const previous = i ? b.c[i - 1] : b.c[i];
        s.up = Number.isNaN(s.up) || up < s.up || previous > s.up ? up : s.up;
        s.low = Number.isNaN(s.low) || low > s.low || previous < s.low ? low : s.low;
        if (s.dir === 1 && b.c[i] < s.low) s.dir = -1;
        else if (s.dir === -1 && b.c[i] > s.up) s.dir = 1;
        out.line[i] = s.dir === 1 ? s.low : s.up;
        out.dir[i] = s.dir;
      },
      ["dir"],
    );
  }
  obv(b: Bars) {
    return this.vector(
      "obv",
      b.n,
      () => 0,
      (i, out) => {
        out[i] = i
          ? out[i - 1] + (b.c[i] > b.c[i - 1] ? b.v[i] : b.c[i] < b.c[i - 1] ? -b.v[i] : 0)
          : 0;
      },
    );
  }
  roc(c: Float64Array, p: number) {
    return this.vector(
      `roc:${p}`,
      c.length,
      () => 0,
      (i, out) => {
        if (i >= p) out[i] = c[i] / c[i - p] - 1;
      },
    );
  }
  typical(b: Bars) {
    return this.vector(
      "typical",
      b.n,
      () => 0,
      (i, out) => {
        out[i] = (b.h[i] + b.l[i] + b.c[i]) / 3;
      },
    );
  }
  cci(b: Bars, p: number) {
    const tp = this.typical(b),
      mean = this.sma(tp, p, "typical");
    return this.vector(
      `cci:${p}`,
      b.n,
      () => 0,
      (i, out) => {
        if (i < p - 1) return;
        let md = 0;
        for (let j = i - p + 1; j <= i; j++) md += Math.abs(tp[j] - mean[i]);
        md /= p;
        out[i] = md > 0 ? (tp[i] - mean[i]) / (0.015 * md) : 0;
      },
    );
  }
  wma(x: Float64Array, p: number, key = "close") {
    const den = (p * (p + 1)) / 2;
    return this.vector(
      `wma:${key}:${p}`,
      x.length,
      () => ({ run: 0 }),
      (i, out, s) => {
        if (!Number.isFinite(x[i])) {
          s.run = 0;
          return;
        }
        if (++s.run < p) return;
        let sum = 0;
        for (let j = 0; j < p; j++) sum += x[i - j] * (p - j);
        out[i] = sum / den;
      },
    );
  }
  hma(c: Float64Array, p: number) {
    const fast = this.wma(c, Math.max(1, Math.round(p / 2))),
      slow = this.wma(c, p);
    const delta = this.vector(
      `hma-delta:${p}`,
      c.length,
      () => 0,
      (i, out) => {
        out[i] = 2 * fast[i] - slow[i];
      },
    );
    return this.wma(delta, Math.max(1, Math.round(Math.sqrt(p))), `hma:${p}`);
  }
  kelt(b: Bars, p: number, mult: number) {
    const mid = this.ema(b.c, p),
      atr = this.atr(b, p);
    const cols = this.run<{ up: Float64Array; lo: Float64Array }, number>(
      `kelt:${p}:${mult}`,
      b.n,
      ["up", "lo"],
      () => 0,
      (i, out) => {
        out.up[i] = mid[i] + mult * atr[i];
        out.lo[i] = mid[i] - mult * atr[i];
      },
    );
    return { mid, ...cols };
  }
  midRange(b: Bars, p: number) {
    return this.vector(
      `midrange:${p}`,
      b.n,
      () => 0,
      (i, out) => {
        if (i < p - 1) return;
        let hi = -Infinity,
          lo = Infinity;
        for (let j = i - p + 1; j <= i; j++) {
          if (b.h[j] > hi) hi = b.h[j];
          if (b.l[j] < lo) lo = b.l[j];
        }
        out[i] = (hi + lo) / 2;
      },
    );
  }
  ichi(b: Bars, t: number, k: number, period: number, shift: number) {
    const tenkan = this.midRange(b, t),
      kijun = this.midRange(b, k),
      sb = this.midRange(b, period);
    const spans = this.run<{ spanA: Float64Array; spanB: Float64Array }, number>(
      `ichi:${t}:${k}:${period}:${shift}`,
      b.n,
      ["spanA", "spanB"],
      () => 0,
      (i, out) => {
        if (i >= shift) {
          out.spanA[i] = (tenkan[i - shift] + kijun[i - shift]) / 2;
          out.spanB[i] = sb[i - shift];
        }
      },
    );
    return { tenkan, kijun, ...spans };
  }
  trix(c: Float64Array, p: number) {
    const one = this.ema(c, p),
      two = this.ema(one, p, `trix2:${p}`),
      three = this.ema(two, p, `trix3:${p}`);
    return this.vector(
      `trix:${p}`,
      c.length,
      () => 0,
      (i, out) => {
        if (i > 0 && Number.isFinite(three[i - 1])) out[i] = (three[i] / three[i - 1] - 1) * 100;
      },
    );
  }
  kama(c: Float64Array, p: number) {
    const fast = 2 / 3,
      slow = 2 / 31;
    return this.vector(
      `kama:${p}`,
      c.length,
      () => ({ prev: NaN }),
      (i, out, s) => {
        if (i < p) return;
        const change = Math.abs(c[i] - c[i - p]);
        let vol = 0;
        for (let j = i - p + 1; j <= i; j++) vol += Math.abs(c[j] - c[j - 1]);
        const er = vol > 0 ? change / vol : 0,
          sc = (er * (fast - slow) + slow) ** 2;
        out[i] = s.prev = Number.isFinite(s.prev) ? s.prev + sc * (c[i] - s.prev) : c[i];
      },
    );
  }
  ha(b: Bars) {
    return this.run<{ o: Float64Array; c: Float64Array }, number>(
      "heikin-ashi",
      b.n,
      ["o", "c"],
      () => 0,
      (i, out) => {
        out.c[i] = (b.o[i] + b.h[i] + b.l[i] + b.c[i]) / 4;
        out.o[i] = i ? (out.o[i - 1] + out.c[i - 1]) / 2 : (b.o[0] + b.c[0]) / 2;
      },
    );
  }
  willr(b: Bars, p: number) {
    const k = this.stoch(b, p, 1).k;
    return this.vector(
      `willr:${p}`,
      b.n,
      () => 0,
      (i, out) => {
        out[i] = k[i] - 100;
      },
    );
  }
  mfi(b: Bars, p: number) {
    const tp = this.typical(b);
    return this.vector(
      `mfi:${p}`,
      b.n,
      () => 0,
      (i, out) => {
        if (i < p) return;
        let pos = 0,
          neg = 0;
        for (let j = i - p + 1; j <= i; j++) {
          const flow = tp[j] * b.v[j];
          if (tp[j] > tp[j - 1]) pos += flow;
          else if (tp[j] < tp[j - 1]) neg += flow;
        }
        out[i] = neg === 0 ? 100 : 100 - 100 / (1 + pos / neg);
      },
    );
  }
  cmf(b: Bars, p: number) {
    const flow = this.vector(
      "cmf-flow",
      b.n,
      () => 0,
      (i, out) => {
        out[i] =
          b.h[i] > b.l[i]
            ? ((b.c[i] - b.l[i] - (b.h[i] - b.c[i])) / (b.h[i] - b.l[i])) * b.v[i]
            : 0;
      },
    );
    const mean = this.sma(flow, p, "cmf-flow"),
      volume = this.sma(b.v, p, "volume");
    return this.vector(
      `cmf:${p}`,
      b.n,
      () => 0,
      (i, out) => {
        if (volume[i] > 0) out[i] = mean[i] / volume[i];
      },
    );
  }
  stochRsi(c: Float64Array, rp: number, sp: number) {
    const rsi = this.rsi(c, rp),
      key = `stoch-rsi:${rp}:${sp}`;
    const raw = this.vector(
      key,
      c.length,
      () => 0,
      (i, out) => {
        if (i < sp - 1) return;
        let hi = -Infinity,
          lo = Infinity;
        for (let j = i - sp + 1; j <= i; j++) {
          if (!Number.isFinite(rsi[j])) return;
          if (rsi[j] > hi) hi = rsi[j];
          if (rsi[j] < lo) lo = rsi[j];
        }
        out[i] = hi === lo ? 50 : ((rsi[i] - lo) / (hi - lo)) * 100;
      },
    );
    const first = raw.findIndex(Number.isFinite);
    return first < 0 ? new Float64Array(c.length).fill(NaN) : this.sma(raw, 3, key, first);
  }
  aroon(b: Bars, p: number) {
    return this.run<{ up: Float64Array; dn: Float64Array }, number>(
      `aroon:${p}`,
      b.n,
      ["up", "dn"],
      () => 0,
      (i, out) => {
        if (i < p) return;
        let hi = i,
          lo = i;
        for (let j = i - p; j <= i; j++) {
          if (b.h[j] >= b.h[hi]) hi = j;
          if (b.l[j] <= b.l[lo]) lo = j;
        }
        out.up[i] = ((p - (i - hi)) / p) * 100;
        out.dn[i] = ((p - (i - lo)) / p) * 100;
      },
    );
  }
  period(b: Bars, ms: number) {
    const names = [
      "curOpen",
      "curVwap",
      "curRange",
      "prevVwap",
      "prevHigh",
      "prevLow",
      "prevPivot",
      "prevRange",
    ] as const;
    return this.run<
      Record<(typeof names)[number], Float64Array>,
      {
        pid: number;
        open: number;
        pv: number;
        vv: number;
        high: number;
        low: number;
        close: number;
        lastV: number;
        lastH: number;
        lastL: number;
        lastC: number;
      }
    >(
      `period:${ms}`,
      b.n,
      names,
      () => ({
        pid: -1,
        open: NaN,
        pv: 0,
        vv: 0,
        high: -Infinity,
        low: Infinity,
        close: NaN,
        lastV: NaN,
        lastH: NaN,
        lastL: NaN,
        lastC: NaN,
      }),
      (i, out, s) => {
        const id = Math.floor(b.t[i] / ms);
        if (id !== s.pid) {
          if (s.pid >= 0) {
            s.lastV = s.vv > 0 ? s.pv / s.vv : NaN;
            s.lastH = s.high;
            s.lastL = s.low;
            s.lastC = s.close;
          }
          s.pid = id;
          s.open = b.o[i];
          s.pv = 0;
          s.vv = 0;
          s.high = -Infinity;
          s.low = Infinity;
        }
        const volume = b.v[i] > 0 ? b.v[i] : 1e-9;
        s.pv += ((b.h[i] + b.l[i] + b.c[i]) / 3) * volume;
        s.vv += volume;
        if (b.h[i] > s.high) s.high = b.h[i];
        if (b.l[i] < s.low) s.low = b.l[i];
        s.close = b.c[i];
        out.curOpen[i] = s.open;
        out.curVwap[i] = s.pv / s.vv;
        out.curRange[i] = s.high - s.low;
        out.prevVwap[i] = s.lastV;
        out.prevHigh[i] = s.lastH;
        out.prevLow[i] = s.lastL;
        out.prevPivot[i] = (s.lastH + s.lastL + s.lastC) / 3;
        out.prevRange[i] = s.lastH - s.lastL;
      },
    );
  }
}
