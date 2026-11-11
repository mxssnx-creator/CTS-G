// Per-symbol memo of indicator series so ~40 indications and 9 bots share one computation.
import type { Bars } from "../domain/types.ts";
import * as I from "../math/indicators.ts";
import type { MarketRef, MarketSource } from "./market.ts";
import { StreamMath } from "./stream-math.ts";

type Any = unknown;

export class SeriesCache {
  readonly b: Bars;
  private m = new Map<string, Any>();
  /** Explicitly worker-owned append-only state. Ordinary immutable/replay caches keep the original full math. */
  streamMath?: StreamMath;
  enableStreaming(math = new StreamMath()) {
    this.streamMath = math;
    return this;
  }
  /** the universe's market reference (makeUniverse attaches it; a series built on its own has none) */
  private mk: { src: MarketSource; tf: number; factor: number } | null = null;
  constructor(b: Bars) {
    this.b = b;
  }
  /** Drop every cached series except those whose key starts with one of `keep` (memory bound between groups). */
  clear(keep: readonly string[] = []) {
    if (!keep.length) {
      this.m.clear();
      return;
    }
    for (const k of this.m.keys()) if (!keep.some((p) => k.startsWith(p))) this.m.delete(k);
  }
  /** Drop cached series whose key ends with `suffix` (one-shot combo signals: keeps memory bounded). */
  forgetSuffix(suffix: string) {
    for (const k of this.m.keys()) if (k.endsWith(suffix)) this.m.delete(k);
  }
  memo<T>(key: string, fn: () => T): T {
    const hit = this.m.get(key);
    if (hit !== undefined) return hit as T;
    const v = fn();
    this.m.set(key, v);
    return v;
  }
  ema(p: number) {
    return this.memo(`ema${p}`, () => this.streamMath?.ema(this.b.c, p) ?? I.ema(this.b.c, p));
  }
  sma(p: number) {
    return this.memo(`sma${p}`, () => this.streamMath?.sma(this.b.c, p) ?? I.sma(this.b.c, p));
  }
  rsi(p: number) {
    return this.memo(`rsi${p}`, () => this.streamMath?.rsi(this.b.c, p) ?? I.rsi(this.b.c, p));
  }
  atr(p: number) {
    return this.memo(
      `atr${p}`,
      () => this.streamMath?.atr(this.b, p) ?? I.atr(this.b.h, this.b.l, this.b.c, p),
    );
  }
  /** ATR as an EMA of the true range (Stable-02 ports and ATR exits) */
  atrEma(p: number) {
    return this.memo(
      `atre${p}`,
      () => this.streamMath?.atrEma(this.b, p) ?? I.atrEma(this.b.h, this.b.l, this.b.c, p),
    );
  }
  macd(f = 12, s = 26, g = 9) {
    return this.memo(
      `macd${f}.${s}.${g}`,
      () => this.streamMath?.macd(this.b.c, f, s, g) ?? I.macd(this.b.c, f, s, g),
    );
  }
  bb(p = 20, k = 2) {
    return this.memo(
      `bb${p}.${k}`,
      () => this.streamMath?.bb(this.b.c, p, k) ?? I.bollinger(this.b.c, p, k),
    );
  }
  psar(step = 0.02, max = 0.2) {
    return this.memo(
      `sar${step}.${max}`,
      () => this.streamMath?.psar(this.b, step, max) ?? I.psar(this.b.h, this.b.l, step, max),
    );
  }
  dmi(p = 14) {
    return this.memo(
      `dmi${p}`,
      () => this.streamMath?.dmi(this.b, p) ?? I.dmi(this.b.h, this.b.l, this.b.c, p),
    );
  }
  st(p = 10, m = 3) {
    return this.memo(
      `st${p}.${m}`,
      () =>
        this.streamMath?.supertrend(this.b, p, m) ??
        I.supertrend(this.b.h, this.b.l, this.b.c, p, m),
    );
  }
  don(p: number) {
    return this.memo(
      `don${p}`,
      () => this.streamMath?.don(this.b, p) ?? I.donchianPrior(this.b.h, this.b.l, p),
    );
  }
  vwap(p: number) {
    return this.memo(
      `vwap${p}`,
      () =>
        this.streamMath?.vwap(this.b, p) ??
        I.vwapRolling(this.b.h, this.b.l, this.b.c, this.b.v, p),
    );
  }
  volSma(p: number) {
    return this.memo(
      `vsma${p}`,
      () => this.streamMath?.sma(this.b.v, p, "volume") ?? I.sma(this.b.v, p),
    );
  }
  roc(p: number) {
    return this.memo(`roc${p}`, () => this.streamMath?.roc(this.b.c, p) ?? I.roc(this.b.c, p));
  }
  stoch(p = 14, d = 3) {
    return this.memo(
      `stoch${p}.${d}`,
      () => this.streamMath?.stoch(this.b, p, d) ?? I.stoch(this.b.h, this.b.l, this.b.c, p, d),
    );
  }
  cci(p: number) {
    return this.memo(
      `cci${p}`,
      () => this.streamMath?.cci(this.b, p) ?? I.cci(this.b.h, this.b.l, this.b.c, p),
    );
  }
  willr(p: number) {
    return this.memo(
      `willr${p}`,
      () => this.streamMath?.willr(this.b, p) ?? I.willr(this.b.h, this.b.l, this.b.c, p),
    );
  }
  mfi(p: number) {
    return this.memo(
      `mfi${p}`,
      () => this.streamMath?.mfi(this.b, p) ?? I.mfi(this.b.h, this.b.l, this.b.c, this.b.v, p),
    );
  }
  obv() {
    return this.memo("obv", () => this.streamMath?.obv(this.b) ?? I.obv(this.b.c, this.b.v));
  }
  cmf(p: number) {
    return this.memo(
      `cmf${p}`,
      () => this.streamMath?.cmf(this.b, p) ?? I.cmf(this.b.h, this.b.l, this.b.c, this.b.v, p),
    );
  }
  stochRsi(p: number, s = p) {
    return this.memo(
      `srsi${p}.${s}`,
      () => this.streamMath?.stochRsi(this.b.c, p, s) ?? I.stochRsi(this.b.c, p, s, 3),
    );
  }
  aroon(p: number) {
    return this.memo(
      `aroon${p}`,
      () => this.streamMath?.aroon(this.b, p) ?? I.aroon(this.b.h, this.b.l, p),
    );
  }
  kelt(p: number, m: number) {
    return this.memo(
      `kelt${p}.${m}`,
      () => this.streamMath?.kelt(this.b, p, m) ?? I.keltner(this.b.h, this.b.l, this.b.c, p, m),
    );
  }
  ichi(t: number, k: number, b: number) {
    return this.memo(
      `ichi${t}.${k}.${b}`,
      () => this.streamMath?.ichi(this.b, t, k, b, k) ?? I.ichimoku(this.b.h, this.b.l, t, k, b, k),
    );
  }
  hma(p: number) {
    return this.memo(`hma${p}`, () => this.streamMath?.hma(this.b.c, p) ?? I.hma(this.b.c, p));
  }
  trix(p: number) {
    return this.memo(`trix${p}`, () => this.streamMath?.trix(this.b.c, p) ?? I.trix(this.b.c, p));
  }
  kama(p: number) {
    return this.memo(`kama${p}`, () => this.streamMath?.kama(this.b.c, p) ?? I.kama(this.b.c, p));
  }
  ha() {
    return this.memo(
      "ha",
      () => this.streamMath?.ha(this.b) ?? I.heikinAshi(this.b.o, this.b.h, this.b.l, this.b.c),
    );
  }
  z(p: number) {
    return this.memo(`z${p}`, () => this.streamMath?.z(this.b.c, p) ?? I.zscore(this.b.c, p));
  }
  /**
   * Higher-timeframe view: `factor` × this timeframe, built from COMPLETED higher bars only.
   * `map[i]` = index of the last higher bar that had closed when bar i closed (−1 = none yet).
   * The higher view carries this series' market reference, aggregated the same way (see market.ts).
   */
  htf(factor: number): { k: SeriesCache; map: Int32Array } {
    return this.memo(`htf${factor}`, () => {
      const { hb, map } = this.streamMath?.higher(this.b, factor) ?? htfBars(this.b, factor);
      const k = new SeriesCache(hb);
      if (this.mk) k.setMarket(this.mk.src, this.mk.tf, this.mk.factor * factor);
      if (this.streamMath) k.enableStreaming(this.streamMath.child(`htf:${factor}`));
      return { k, map };
    });
  }
  /**
   * Attach the universe's market reference (market.ts): `tf` is the timeframe of the universe series it is built
   * from, `factor` this series' aggregation of that timeframe (1 = a universe series, a higher view: its factor).
   */
  setMarket(src: MarketSource | null, tf: number = this.b.tfMin, factor = 1) {
    if (this.mk?.src === src && this.mk.tf === tf && this.mk.factor === factor) return;
    this.mk = src ? { src, tf, factor } : null;
    // Cached relation states and higher views may already depend on the previous market source.
    this.m.clear();
  }
  /**
   * The equal-weight market of this series' timeframe, aligned to its bars by open time: per-bar log return,
   * cumulative log index and activity (see market.ts). null when no market reference was attached (a series
   * computed on its own): the relation indications are then neutral.
   */
  market(): MarketRef | null {
    const mk = this.mk;
    if (!mk) return null;
    return this.memo("mkt", () => mk.src.ref(this.b, mk.tf, mk.factor));
  }
  reference(sym: string): MarketRef | null {
    const mk = this.mk;
    if (!mk) return null;
    return this.memo(`peer:${sym}`, () => mk.src.reference(this.b, sym, mk.tf, mk.factor));
  }
  period(ms: number) {
    const b = this.b;
    return this.memo(
      `per${ms}`,
      () => this.streamMath?.period(b, ms) ?? I.periodLevels(b.t, b.h, b.l, b.c, b.o, b.v, ms),
    );
  }
  rangeSma(p: number) {
    return this.memo(`rsma${p}`, () => {
      if (this.streamMath) {
        const r = this.streamMath.vector(
          "ranges",
          this.b.n,
          () => 0,
          (i, out) => {
            out[i] = this.b.h[i] - this.b.l[i];
          },
        );
        return this.streamMath.sma(r, p, "ranges");
      }
      const r = new Float64Array(this.b.n);
      for (let i = 0; i < this.b.n; i++) r[i] = this.b.h[i] - this.b.l[i];
      return I.sma(r, p);
    });
  }
}

/**
 * Higher-timeframe bars: `factor` × the series' timeframe, COMPLETED higher bars only (a bucket is published with
 * the bar that closes it). A bucket needs every aligned, contiguous base bar; gaps and partial starting buckets
 * are dropped; non-increasing source timestamps are rejected. `map[i]` = index of the last higher bar that had
 * closed when bar i closed (−1 = none).
 */
export function htfBars(b: Bars, factor: number): { hb: Bars; map: Int32Array } {
  const tfMs = b.tfMin * 60_000;
  const span = tfMs * factor;
  const T: number[] = [],
    O: number[] = [],
    Hh: number[] = [],
    L: number[] = [],
    C: number[] = [],
    V: number[] = [];
  const map = new Int32Array(b.n).fill(-1);
  let cur = NaN;
  let bo = 0,
    bh = 0,
    bl = 0,
    bc = 0,
    bv = 0,
    bt = 0;
  let count = 0;
  let complete = false;
  let lastT = NaN;
  for (let i = 0; i < b.n; i++) {
    if (i > 0 && !(b.t[i] > lastT))
      throw new RangeError("source bars must have strictly increasing timestamps");
    const bucket = Math.floor(b.t[i] / span);
    if (bucket !== cur) {
      cur = bucket;
      bt = bucket * span;
      bo = b.o[i];
      bh = b.h[i];
      bl = b.l[i];
      bc = b.c[i];
      bv = b.v[i];
      count = 1;
      complete = b.t[i] === bt;
    } else {
      if (b.h[i] > bh) bh = b.h[i];
      if (b.l[i] < bl) bl = b.l[i];
      bc = b.c[i];
      bv += b.v[i];
      count++;
      complete = complete && b.t[i] === lastT + tfMs;
    }
    lastT = b.t[i];
    // the bucket closes with this bar: publish the completed higher bar
    if (complete && count === factor && b.t[i] + tfMs === bt + span) {
      T.push(bt);
      O.push(bo);
      Hh.push(bh);
      L.push(bl);
      C.push(bc);
      V.push(bv);
    }
    map[i] = T.length - 1;
  }
  const hb: Bars = {
    sym: b.sym,
    tfMin: b.tfMin * factor,
    n: T.length,
    t: Float64Array.from(T),
    o: Float64Array.from(O),
    h: Float64Array.from(Hh),
    l: Float64Array.from(L),
    c: Float64Array.from(C),
    v: Float64Array.from(V),
  };
  return { hb, map };
}
