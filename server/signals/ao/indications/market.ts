// Market reference of a universe: per timeframe, an equal-weight market index and a market activity series built
// from every series of that timeframe, so an indication can read what the symbol does RELATIVE to the market (a
// symbol lagging a market move, a residual stretch, an idiosyncratic volume spike) — information a single-symbol
// indication cannot see.
//
//   ret[g]  market log return of grid bar g: the mean, over the series with a bar at that open time and the
//           immediately preceding contiguous bar, of log(c / c_prev) (NaN when no series has both)
//   idx[g]  cumulative market log index: Σ ret up to g (0 at the grid's first bar, a NaN return adds nothing)
//   act[g]  market activity: the mean, over the series with a bar at g, of v / its own rolling 60-bar SMA of v
//           (the SMA includes the bar; NaN until some series has 60 contiguous bars)
//
// The grid is the union of the open times of the timeframe's series, so every series aligns to it by time; a
// series without gaps is a contiguous run of the grid and its aligned arrays are views (no copy). Strictly causal:
// grid bar g reads only bars with open time ≤ t[g] (bars that close with it at the latest), so a universe cut at
// any time gives the same values up to the cut. A series alone in its timeframe is its own market (the relation
// indications are then neutral).
//
// Built lazily, once per timeframe (and per higher-view factor, from completed higher bars) per universe: 4 Float64
// arrays of the grid's length — for 50 symbols × 28 800 1m bars ≈ 0.9 MB per timeframe, while every series reads
// views into it. The worker threads build their universe from the same full bars: identical values everywhere.
import type { Bars } from "../domain/types.ts";
import { htfBars } from "./cache.ts";
import { StreamMath } from "./stream-math.ts";

/** Rolling window (bars) of each series' own volume SMA in the market activity. */
export const MARKET_ACT_P = 60;

export interface MarketRef {
  /** market log return of each bar (NaN: no constituent had a previous bar, or the bar is not on the grid) */
  ret: Float64Array;
  /** cumulative market log index */
  idx: Float64Array;
  /** market activity: mean of v / SMA60(v) over the constituents */
  act: Float64Array;
  /** number of series the market is built from (1: the series alone — relations are neutral) */
  syms: number;
}

interface Grid {
  t: Float64Array;
  ret: Float64Array;
  idx: Float64Array;
  act: Float64Array;
  syms: number;
}

/** Append-only market grids for a private realtime generation. Old constituent bars may not arrive retroactively. */
export class IncrementalMarkets {
  private aggregators = new Map<string, StreamMath>();
  private alignments = new Map<string, { n: number; first: number; start: number; run: boolean }>();
  /** The grid and target prefixes are immutable within this generation; verify only new matching times. */
  align(key: string, grid: Grid, b: Bars): MarketRef {
    const id = `${key}|${b.sym}|${b.tfMin}`;
    let old = this.alignments.get(id);
    if (!old || b.n < old.n || b.t[0] !== old.first)
      old = { n: 0, first: b.t[0], start: b.n ? lowerBound(grid.t, b.t[0]) : 0, run: true };
    let run = old.run && old.start + b.n <= grid.t.length;
    for (let i = old.n; run && i < b.n; i++) if (grid.t[old.start + i] !== b.t[i]) run = false;
    this.alignments.set(id, { ...old, n: b.n, run });
    return run
      ? {
          ret: grid.ret.subarray(old.start, old.start + b.n),
          idx: grid.idx.subarray(old.start, old.start + b.n),
          act: grid.act.subarray(old.start, old.start + b.n),
          syms: grid.syms,
        }
      : alignMarket(grid, b);
  }

  higher(b: Bars, factor: number) {
    const key = `${b.sym}|${b.tfMin}`;
    let math = this.aggregators.get(key);
    if (!math) this.aggregators.set(key, (math = new StreamMath()));
    return math.higher(b, factor).hb;
  }
  private records = new Map<
    string,
    {
      grid: Grid;
      cap: number;
      cols: { t: Float64Array; ret: Float64Array; idx: Float64Array; act: Float64Array };
      members: Array<{
        sym: string;
        tf: number;
        n: number;
        first: number;
        last: number;
        vs: number;
        start: number;
      }>;
    }
  >();
  get(key: string, series: readonly Bars[]): Grid {
    let record = this.records.get(key);
    if (!record) {
      const grid = buildMarket(series);
      const members = series.map((b) => {
        let vs = 0,
          start = 0;
        for (let i = 0; i < b.n; i++) {
          if (!i || b.t[i] - b.t[i - 1] !== b.tfMin * 60_000) {
            vs = 0;
            start = i;
          }
          vs += Number.isFinite(b.v[i]) ? b.v[i] : 0;
          if (i - start + 1 > MARKET_ACT_P)
            vs -= Number.isFinite(b.v[i - MARKET_ACT_P]) ? b.v[i - MARKET_ACT_P] : 0;
        }
        return { sym: b.sym, tf: b.tfMin, n: b.n, first: b.t[0], last: b.t[b.n - 1], vs, start };
      });
      record = {
        grid,
        cap: grid.t.length,
        cols: { t: grid.t, ret: grid.ret, idx: grid.idx, act: grid.act },
        members,
      };
      this.records.set(key, record);
      return grid;
    }
    if (record.members.length !== series.length)
      throw new Error("market constituents changed; bootstrap required");
    const oldN = record.grid.t.length,
      last = record.grid.t[oldN - 1] ?? -Infinity;
    const times: number[] = [];
    for (let s = 0; s < series.length; s++) {
      const b = series[s],
        previous = record.members[s];
      if (
        b.sym !== previous.sym ||
        b.tfMin !== previous.tf ||
        b.n < previous.n ||
        (previous.n && (b.t[0] !== previous.first || b.t[previous.n - 1] !== previous.last))
      )
        throw new Error("market history changed; bootstrap required");
      for (let i = previous.n; i < b.n; i++) {
        if (!(b.t[i] > last))
          throw new Error("late peer candle changes market history; bootstrap required");
        times.push(b.t[i]);
      }
    }
    if (!times.length) return record.grid;
    times.sort((a, b) => a - b);
    const unique = times.filter((t, i) => !i || t !== times[i - 1]);
    const n = oldN + unique.length;
    if (record.cap < n) {
      record.cap = Math.max(n, Math.ceil(n * 1.25) + 64);
      for (const name of ["t", "ret", "idx", "act"] as const) {
        const col = new Float64Array(record.cap);
        col.set(record.grid[name]);
        record.cols[name] = col;
      }
    }
    const { t, ret, idx, act } = record.cols;
    const nr = new Uint32Array(unique.length),
      na = new Uint32Array(unique.length);
    const slot = new Map(unique.map((time, i) => [time, i]));
    t.set(unique, oldN);
    ret.fill(0, oldN, n);
    act.fill(0, oldN, n);
    for (let s = 0; s < series.length; s++) {
      const b = series[s],
        state = record.members[s];
      for (let i = state.n; i < b.n; i++) {
        const j = slot.get(b.t[i])!,
          g = oldN + j;
        if (!i || b.t[i] - b.t[i - 1] !== b.tfMin * 60_000) {
          state.vs = 0;
          state.start = i;
        }
        if (i && b.t[i] - b.t[i - 1] === b.tfMin * 60_000) {
          const r = Math.log(b.c[i] / b.c[i - 1]);
          if (Number.isFinite(r)) {
            ret[g] += r;
            nr[j]++;
          }
        }
        const volume = Number.isFinite(b.v[i]) ? b.v[i] : 0;
        state.vs += volume;
        const bars = i - state.start + 1;
        if (bars > MARKET_ACT_P)
          state.vs -= Number.isFinite(b.v[i - MARKET_ACT_P]) ? b.v[i - MARKET_ACT_P] : 0;
        if (bars >= MARKET_ACT_P && state.vs > 0) {
          act[g] += volume / (state.vs / MARKET_ACT_P);
          na[j]++;
        }
      }
      state.n = b.n;
      state.first = b.t[0];
      state.last = b.t[b.n - 1];
    }
    let cumulative = oldN ? idx[oldN - 1] : 0;
    for (let i = oldN; i < n; i++) {
      ret[i] = nr[i - oldN] ? ret[i] / nr[i - oldN] : NaN;
      act[i] = na[i - oldN] ? act[i] / na[i - oldN] : NaN;
      if (Number.isFinite(ret[i])) cumulative += ret[i];
      idx[i] = cumulative;
    }
    return (record.grid = {
      t: t.subarray(0, n),
      ret: ret.subarray(0, n),
      idx: idx.subarray(0, n),
      act: act.subarray(0, n),
      syms: series.length,
    });
  }
}

/** first index of sorted `t` with t[i] ≥ x */
function lowerBound(t: Float64Array, x: number): number {
  let lo = 0;
  let hi = t.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (t[mid] < x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** The equal-weight market of a set of series of one timeframe, on the union of their open times. */
export function buildMarket(series: readonly Bars[]): Grid {
  let tot = 0;
  for (const b of series) tot += b.n;
  // the union grid: every open time once, ascending (one series, or series on one grid: no sort needed)
  let t: Float64Array;
  const f = series[0];
  const onOne = (b: Bars) => {
    if (b.n !== f.n) return false;
    for (let i = 0; i < b.n; i++) if (b.t[i] !== f.t[i]) return false;
    return true;
  };
  if (f && series.every(onOne)) t = f.t.slice(0, f.n);
  else {
    const all = new Float64Array(tot);
    let o = 0;
    for (const b of series) {
      all.set(b.t.subarray(0, b.n), o);
      o += b.n;
    }
    all.sort();
    let u = 0;
    for (let i = 0; i < all.length; i++) if (i === 0 || all[i] !== all[u - 1]) all[u++] = all[i];
    t = all.slice(0, u);
  }
  const G = t.length;
  const ret = new Float64Array(G);
  const act = new Float64Array(G);
  const nr = new Uint32Array(G);
  const na = new Uint32Array(G);
  const P = MARKET_ACT_P;
  for (const b of series) {
    if (!b.n) continue;
    let j = lowerBound(t, b.t[0]);
    let vs = 0;
    let runStart = 0;
    const barMs = b.tfMin * 60_000;
    for (let i = 0; i < b.n; i++) {
      // Missing candles split the return and activity histories. Treating a multi-bar close change as a one-bar
      // market return distorts market volatility/beta, while carrying the old volume window makes activity stale.
      if (i === 0 || b.t[i] - b.t[i - 1] !== barMs) {
        vs = 0;
        runStart = i;
      }
      while (t[j] < b.t[i]) j++;
      if (i >= 1 && b.t[i] - b.t[i - 1] === barMs) {
        const r = Math.log(b.c[i] / b.c[i - 1]);
        if (Number.isFinite(r)) {
          ret[j] += r;
          nr[j]++;
        }
      }
      // the series' own rolling volume SMA (its last P bars, this one included)
      const v = Number.isFinite(b.v[i]) ? b.v[i] : 0;
      vs += v;
      const runBars = i - runStart + 1;
      if (runBars > P) vs -= Number.isFinite(b.v[i - P]) ? b.v[i - P] : 0;
      if (runBars >= P && vs > 0) {
        act[j] += v / (vs / P);
        na[j]++;
      }
    }
  }
  const idx = new Float64Array(G);
  let cum = 0;
  for (let g = 0; g < G; g++) {
    ret[g] = nr[g] ? ret[g] / nr[g] : NaN;
    act[g] = na[g] ? act[g] / na[g] : NaN;
    if (Number.isFinite(ret[g])) cum += ret[g];
    idx[g] = cum;
  }
  return { t, ret, idx, act, syms: series.length };
}

/** A grid's values at a series' bars, matched by open time (views when the series is a contiguous run of it). */
export function alignMarket(g: Grid, b: Bars): MarketRef {
  const n = b.n;
  const s0 = n ? lowerBound(g.t, b.t[0]) : 0;
  let run = s0 + n <= g.t.length;
  for (let i = 0; run && i < n; i++) if (g.t[s0 + i] !== b.t[i]) run = false;
  if (run)
    return {
      ret: g.ret.subarray(s0, s0 + n),
      idx: g.idx.subarray(s0, s0 + n),
      act: g.act.subarray(s0, s0 + n),
      syms: g.syms,
    };
  const ret = new Float64Array(n).fill(NaN);
  const idx = new Float64Array(n).fill(NaN);
  const act = new Float64Array(n).fill(NaN);
  for (let i = 0, j = s0; i < n; i++) {
    while (j < g.t.length && g.t[j] < b.t[i]) j++;
    if (j < g.t.length && g.t[j] === b.t[i]) {
      ret[i] = g.ret[j];
      idx[i] = g.idx[j];
      act[i] = g.act[j];
    }
  }
  return { ret, idx, act, syms: g.syms };
}

/**
 * The market reference of a universe: the grids are built on first use, one per timeframe (and per higher-view
 * factor: the timeframe's series aggregated to completed higher bars, as SeriesCache.htf does), and kept.
 */
export class MarketSource {
  private readonly bars: readonly Bars[];
  private grids = new Map<string, Grid>();
  private stream?: IncrementalMarkets;
  constructor(bars: readonly Bars[], stream?: IncrementalMarkets) {
    this.bars = bars;
    this.stream = stream;
  }
  grid(tf: number, factor = 1): Grid {
    const key = `${tf}x${factor}`;
    let g = this.grids.get(key);
    if (!g) {
      const members = this.bars.filter((b) => b.tfMin === tf && b.n > 0);
      const inputs =
        factor === 1
          ? members
          : members.map((b) => this.stream?.higher(b, factor) ?? htfBars(b, factor).hb);
      g = this.stream ? this.stream.get(key, inputs) : buildMarket(inputs);
      this.grids.set(key, g);
    }
    return g;
  }
  /** the market at the bars of `b` (a series of timeframe tf, or its `factor` × higher view) */
  ref(b: Bars, tf: number, factor = 1): MarketRef {
    const grid = this.grid(tf, factor);
    return this.stream?.align(`market:${tf}x${factor}`, grid, b) ?? alignMarket(grid, b);
  }
  /** One named peer, matched by completed bar time. Missing/self/ambiguous peers stay unavailable. */
  reference(b: Bars, sym: string, tf: number, factor = 1): MarketRef | null {
    if (b.sym === sym) return null;
    const members = this.bars.filter((x) => x.sym === sym && x.tfMin === tf && x.n > 0);
    if (members.length !== 1) return null;
    const key = `peer:${sym}:${tf}x${factor}`;
    let g = this.grids.get(key);
    if (!g) {
      const inputs = [
        factor === 1
          ? members[0]
          : (this.stream?.higher(members[0], factor) ?? htfBars(members[0], factor).hb),
      ];
      g = this.stream ? this.stream.get(key, inputs) : buildMarket(inputs);
      this.grids.set(key, g);
    }
    return this.stream?.align(key, g, b) ?? alignMarket(g, b);
  }

}
