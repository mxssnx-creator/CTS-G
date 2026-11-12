// Tactic is an indication family, separate from the existing remove-only tactics filters. Each technique reads
// only the current CLOSED bar and its past. These are research candidates: the ordinary per-config PF gates still
// decide qualification, and adding the family does not promote a technique into a measured live preset.
import { SeriesCache } from "./cache.ts";
import type { IndicationSpec } from "./registry.ts";
import type { Bars } from "../domain/types.ts";

export const TACTIC_TIMEFRAMES = [1, 5, 15, 30] as const;
export const TACTIC_TECHNIQUES = [
  {
    id: "trend",
    label: "Trend",
    description: "EMA direction and ADX strength with controlled extension.",
  },
  {
    id: "pullback",
    label: "Pullback",
    description: "A return through the fast EMA in an established trend.",
  },
  {
    id: "mean-reversion",
    label: "Mean reversion",
    description: "A stretched price turning back in a ranging market.",
  },
  {
    id: "breakout",
    label: "Breakout",
    description: "A close beyond prior structure with volume participation.",
  },
  {
    id: "adaptive",
    label: "Regime adaptive",
    description: "Trend techniques in trends and mean reversion in ranges.",
  },
] as const;
export type TacticTechnique = (typeof TACTIC_TECHNIQUES)[number]["id"];
export type TacticRange = "short" | "medium";
export type TacticDirection = -1 | 0 | 1;
export type TacticRegime = "insufficient" | "trend" | "range" | "transition";
export type TacticMethod = Exclude<TacticTechnique, "adaptive"> | "wait";

const RANGE = {
  short: { fast: 12, slow: 36, look: 20, z: 20 },
  medium: { fast: 24, slow: 72, look: 40, z: 40 },
} as const;

export interface TacticSnapshot {
  sym: string;
  tfMin: number;
  range: TacticRange;
  /** Input Bars contain completed candles; barTime is the open and closedAt their close time. */
  barTime: number | null;
  closedAt: number | null;
  ready: boolean;
  warmupBars: number;
  trend: TacticDirection;
  regime: TacticRegime;
  volatility: {
    atr: number | null;
    atrPct: number | null;
    ratio: number | null;
    state: "unknown" | "low" | "normal" | "high";
  };
  structure: { high: number | null; low: number | null; breakout: TacticDirection };
  zScore: number | null;
  volumeRatio: number | null;
  adaptiveMethod: TacticMethod;
  methods: Array<{ id: TacticTechnique; label: string; state: TacticDirection; reasons: string[] }>;
}

interface TacticSeries {
  warmup: number;
  ready: Int8Array;
  trend: Int8Array;
  regime: Int8Array;
  atr: Float64Array;
  adx: Float64Array;
  z: Float64Array;
  atrRatio: Float64Array;
  volumeRatio: Float64Array;
  high: Float64Array;
  low: Float64Array;
  breakout: Int8Array;
  states: Record<TacticTechnique, Int8Array>;
  adaptive: Int8Array;
}

const fin = (...xs: number[]) => xs.every(Number.isFinite);
const nullable = (x: number) => (Number.isFinite(x) ? x : null);
const sign = (x: number): TacticDirection => (x > 0 ? 1 : x < 0 ? -1 : 0);
const METHOD: readonly TacticMethod[] = ["wait", "trend", "pullback", "mean-reversion", "breakout"];

function validBar(b: Bars, i: number): boolean {
  return (
    b.tfMin > 0 &&
    fin(b.t[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i]) &&
    b.l[i] > 0 &&
    b.h[i] >= Math.max(b.o[i], b.c[i]) &&
    b.l[i] <= Math.min(b.o[i], b.c[i]) &&
    b.v[i] >= 0
  );
}

/** Fresh valid segments reset recursive indicator state as well as readiness after an outage or invalid candle. */
function segmented(k: SeriesCache, range: TacticRange, warmup: number): TacticSeries | null {
  const b = k.b;
  // In the append-only worker, validate only unseen bars. Once a gap is observed the full segmented
  // oracle remains authoritative; continuous lanes avoid rescanning their entire history each minute.
  if (k.streamMath) {
    const check = k.streamMath.frame("tactic-valid", b.n, ["valid"], () => ({ invalid: false }), [
      "valid",
    ]);
    for (let i = check.from; i < b.n; i++)
      if (!validBar(b, i) || (i > 0 && b.t[i] - b.t[i - 1] !== b.tfMin * 60_000))
        check.state.invalid = true;
    check.commit(check.state);
    if (!check.state.invalid) return null;
  }
  const spans: Array<[number, number]> = [];
  let start = -1;
  for (let i = 0; i < b.n; i++) {
    const valid = validBar(b, i);
    const adjacent = i > 0 && b.t[i] - b.t[i - 1] === b.tfMin * 60_000;
    if (start >= 0 && (!valid || !adjacent)) {
      spans.push([start, i]);
      start = -1;
    }
    if (valid && start < 0) start = i;
  }
  if (start >= 0) spans.push([start, b.n]);
  if (spans.length === 1 && spans[0][0] === 0 && spans[0][1] === b.n) return null;
  const n = b.n;
  const nan = () => new Float64Array(n).fill(NaN);
  const out: TacticSeries = {
    warmup,
    ready: new Int8Array(n),
    trend: new Int8Array(n),
    regime: new Int8Array(n),
    atr: nan(),
    adx: nan(),
    z: nan(),
    atrRatio: nan(),
    volumeRatio: nan(),
    high: nan(),
    low: nan(),
    breakout: new Int8Array(n),
    adaptive: new Int8Array(n),
    states: Object.fromEntries(TACTIC_TECHNIQUES.map((x) => [x.id, new Int8Array(n)])) as Record<
      TacticTechnique,
      Int8Array
    >,
  };
  for (const [from, to] of spans) {
    const part = tacticSeries(
      new SeriesCache({
        ...b,
        n: to - from,
        t: b.t.slice(from, to),
        o: b.o.slice(from, to),
        h: b.h.slice(from, to),
        l: b.l.slice(from, to),
        c: b.c.slice(from, to),
        v: b.v.slice(from, to),
      }),
      range,
    );
    for (const key of [
      "ready",
      "trend",
      "regime",
      "atr",
      "adx",
      "z",
      "atrRatio",
      "volumeRatio",
      "high",
      "low",
      "breakout",
      "adaptive",
    ] as const)
      out[key].set(part[key], from);
    for (const method of TACTIC_TECHNIQUES) out.states[method.id].set(part.states[method.id], from);
  }
  return out;
}

/** Shared context and technique series; five techniques reuse the same calculations on each range and lane. */
function tacticSeries(k: SeriesCache, range: TacticRange): TacticSeries {
  return k.memo(`tactic:${range}:v1`, () => {
    const p = RANGE[range];
    const b = k.b;
    const n = b.n;
    const warmup = Math.max(p.slow + 3, p.look + 15, 30);
    const reset = segmented(k, range, warmup);
    if (reset) return reset;
    const fast = k.ema(p.fast);
    const slow = k.ema(p.slow);
    const atr = k.atr(14);
    const { adx, pdi, mdi } = k.dmi(14);
    // A flat, fully warmed window has no stretch (z = 0), rather than an unready context forever.
    const rawZ = k.z(p.z);
    const normalizeZ = (v: number, i: number) => (Number.isFinite(v) ? v : i >= p.z - 1 ? 0 : NaN);
    const z = k.streamMath
      ? k.streamMath.vector(
          `tactic-z:${range}`,
          n,
          () => 0,
          (i, out) => {
            out[i] = normalizeZ(rawZ[i], i);
          },
        )
      : Float64Array.from(rawZ, normalizeZ);
    const volume = k.volSma(p.look);
    const { hi: high, lo: low } = k.don(p.look);
    const integerNames = [
      "ready",
      "trend",
      "regime",
      "breakout",
      "adaptive",
      ...TACTIC_TECHNIQUES.map((x) => `state:${x.id}`),
    ];
    const names = [...integerNames, "atrRatio", "volumeRatio"];
    const frame = k.streamMath?.frame<
      Record<string, Float64Array | Int8Array>,
      { contiguous: number; atrSum: number; atrCount: number }
    >(
      `tactic-series:${range}`,
      n,
      names,
      () => ({ contiguous: 0, atrSum: 0, atrCount: 0 }),
      integerNames,
    );
    const int = (name: string) => (frame ? (frame.cols[name] as Int8Array) : new Int8Array(n));
    const ready = int("ready"),
      trend = int("trend"),
      regime = int("regime");
    const atrRatio = frame ? (frame.cols.atrRatio as Float64Array) : new Float64Array(n).fill(NaN);
    const volumeRatio = frame
      ? (frame.cols.volumeRatio as Float64Array)
      : new Float64Array(n).fill(NaN);
    const breakout = int("breakout"),
      adaptive = int("adaptive");
    const states = Object.fromEntries(
      TACTIC_TECHNIQUES.map((x) => [x.id, int(`state:${x.id}`)]),
    ) as Record<TacticTechnique, Int8Array>;
    let { contiguous, atrSum, atrCount } = frame?.state ?? {
      contiguous: 0,
      atrSum: 0,
      atrCount: 0,
    };
    for (let i = frame?.from ?? 0; i < n; i++) {
      // The reference ATR and volume exclude this bar: a breakout cannot increase its own participation baseline.
      if (i > 0 && Number.isFinite(atr[i - 1])) {
        atrSum += atr[i - 1];
        atrCount++;
      }
      if (i > p.look && Number.isFinite(atr[i - p.look - 1])) {
        atrSum -= atr[i - p.look - 1];
        atrCount--;
      }
      const valid =
        fin(b.t[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i]) &&
        b.l[i] > 0 &&
        b.h[i] >= Math.max(b.o[i], b.c[i]) &&
        b.l[i] <= Math.min(b.o[i], b.c[i]) &&
        b.v[i] >= 0;
      const consecutive = i === 0 || b.t[i] - b.t[i - 1] === b.tfMin * 60_000;
      contiguous = valid ? (consecutive ? contiguous + 1 : 1) : 0;
      if (atrCount === p.look && atrSum > 0) atrRatio[i] = atr[i] / (atrSum / p.look);
      if (i > 0 && Number.isFinite(volume[i - 1]))
        volumeRatio[i] = volume[i - 1] > 0 ? b.v[i] / volume[i - 1] : 0;
      if (
        contiguous < warmup ||
        i < 3 ||
        !fin(
          fast[i],
          slow[i],
          slow[i - 3],
          atr[i],
          adx[i],
          pdi[i],
          mdi[i],
          z[i],
          z[i - 1],
          high[i],
          low[i],
          atrRatio[i],
          volumeRatio[i],
        ) ||
        !(atr[i] > 0)
      )
        continue;
      ready[i] = 1;
      const dir =
        fast[i] > slow[i] && slow[i] > slow[i - 3]
          ? 1
          : fast[i] < slow[i] && slow[i] < slow[i - 3]
            ? -1
            : 0;
      trend[i] = dir;
      regime[i] = adx[i] >= 22 && dir !== 0 ? 1 : adx[i] <= 18 ? 2 : 3;
      const move = b.c[i] - b.c[i - 1];
      const extension = Math.abs(b.c[i] - fast[i]) / atr[i];
      if (
        regime[i] === 1 &&
        dir * (pdi[i] - mdi[i]) > 0 &&
        dir * (b.c[i] - fast[i]) > 0 &&
        extension <= 2 &&
        volumeRatio[i] >= 0.7 &&
        atrRatio[i] <= 2.5
      )
        states.trend[i] = dir;
      if (
        dir !== 0 &&
        adx[i] >= 18 &&
        dir * (b.c[i - 1] - fast[i - 1]) <= 0 &&
        dir * (b.c[i] - fast[i]) > 0 &&
        dir * move > 0 &&
        volumeRatio[i] >= 0.6 &&
        atrRatio[i] <= 2
      )
        states.pullback[i] = dir;
      if (adx[i] <= 22 && atrRatio[i] <= 1.5) {
        if (z[i - 1] <= -1.5 && move > 0 && z[i] > z[i - 1]) states["mean-reversion"][i] = 1;
        else if (z[i - 1] >= 1.5 && move < 0 && z[i] < z[i - 1]) states["mean-reversion"][i] = -1;
      }
      const br = b.c[i] > high[i] ? 1 : b.c[i] < low[i] ? -1 : 0;
      breakout[i] = br;
      const span = b.h[i] - b.l[i];
      const location = span > 0 ? (b.c[i] - b.l[i]) / span : 0.5;
      if (
        br !== 0 &&
        br * (b.c[i] - b.o[i]) > 0 &&
        (br > 0 ? location >= 0.7 : location <= 0.3) &&
        volumeRatio[i] >= 1.15 &&
        atrRatio[i] >= 0.8
      )
        states.breakout[i] = br;
      if (regime[i] === 1) {
        if (states.breakout[i] === dir) {
          states.adaptive[i] = dir;
          adaptive[i] = 4;
        } else if (states.pullback[i] === dir) {
          states.adaptive[i] = dir;
          adaptive[i] = 2;
        } else if (states.trend[i] === dir) {
          states.adaptive[i] = dir;
          adaptive[i] = 1;
        }
      } else if (regime[i] === 2 && states["mean-reversion"][i] !== 0) {
        states.adaptive[i] = states["mean-reversion"][i];
        adaptive[i] = 3;
      } else if (regime[i] === 3 && states.breakout[i] !== 0 && volumeRatio[i] >= 1.3) {
        states.adaptive[i] = states.breakout[i];
        adaptive[i] = 4;
      }
    }
    frame?.commit({ contiguous, atrSum, atrCount });
    return {
      warmup,
      ready,
      trend,
      regime,
      atr,
      adx,
      z,
      atrRatio,
      volumeRatio,
      high,
      low,
      breakout,
      states,
      adaptive,
    };
  });
}

/** One technique's causal directional state, independently evaluated by the normal bot × indication pipeline. */
export function tacticState(
  k: SeriesCache,
  technique: TacticTechnique,
  range: TacticRange = "short",
): Int8Array {
  return tacticSeries(k, range).states[technique];
}

/** Current context, or the context at a historical closed-bar index; future bars never change this snapshot. */
export function tacticSnapshot(
  k: SeriesCache,
  range: TacticRange = "short",
  index = k.b.n - 1,
): TacticSnapshot {
  const x = tacticSeries(k, range);
  const i = Number.isInteger(index) && index >= 0 && index < k.b.n ? index : -1;
  const ready = i >= 0 && x.ready[i] === 1;
  const regime: TacticRegime = !ready
    ? "insufficient"
    : x.regime[i] === 1
      ? "trend"
      : x.regime[i] === 2
        ? "range"
        : "transition";
  const ratio = ready ? x.atrRatio[i] : NaN;
  const atr = ready ? x.atr[i] : NaN;
  const adaptiveMethod = ready ? METHOD[x.adaptive[i]] : "wait";
  return {
    sym: k.b.sym,
    tfMin: k.b.tfMin,
    range,
    barTime: i >= 0 ? nullable(k.b.t[i]) : null,
    closedAt: i >= 0 ? nullable(k.b.t[i] + k.b.tfMin * 60_000) : null,
    ready,
    warmupBars: x.warmup,
    trend: ready ? sign(x.trend[i]) : 0,
    regime,
    volatility: {
      atr: nullable(atr),
      atrPct: ready ? (atr / k.b.c[i]) * 100 : null,
      ratio: nullable(ratio),
      state: !ready ? "unknown" : ratio < 0.75 ? "low" : ratio > 1.5 ? "high" : "normal",
    },
    structure: {
      high: ready ? nullable(x.high[i]) : null,
      low: ready ? nullable(x.low[i]) : null,
      breakout: ready ? sign(x.breakout[i]) : 0,
    },
    zScore: ready ? nullable(x.z[i]) : null,
    volumeRatio: ready ? nullable(x.volumeRatio[i]) : null,
    adaptiveMethod,
    methods: TACTIC_TECHNIQUES.map((method) => ({
      id: method.id,
      label: method.label,
      state: ready ? sign(x.states[method.id][i]) : 0,
      reasons: !ready
        ? [`Requires ${x.warmup} consecutive completed valid bars.`]
        : method.id === "adaptive"
          ? [
              `${regime} regime`,
              adaptiveMethod === "wait" ? "No qualifying technique." : `Uses ${adaptiveMethod}.`,
            ]
          : method.id === "trend"
            ? [
                `EMA direction ${x.trend[i]}`,
                `ADX ${x.adx[i].toFixed(1)}; requires 22 and controlled extension.`,
              ]
            : method.id === "pullback"
              ? [
                  "Requires a return through the fast EMA in the trend direction.",
                  `ADX ${x.adx[i].toFixed(1)}; requires 18.`,
                ]
              : method.id === "mean-reversion"
                ? [
                    `Z score ${x.z[i].toFixed(2)}; requires a prior 1.5 stretch turning back.`,
                    `ADX ${x.adx[i].toFixed(1)}; maximum 22.`,
                  ]
                : [
                    `Prior ${RANGE[range].look}-bar structure; requires a closing break.`,
                    `Volume ${x.volumeRatio[i].toFixed(2)}×; requires 1.15×.`,
                  ],
    })),
  };
}

export function tacticSpecs(): IndicationSpec[] {
  return TACTIC_TECHNIQUES.flatMap((method) =>
    (["short", "medium"] as const).map((range) => ({
      id: `tactic-${method.id}-${range === "short" ? "s" : "m"}`,
      kind: "tactic" as const,
      label: `Tactic · ${method.label} · ${range === "short" ? "Short" : "Medium"}`,
      params: {
        fast: RANGE[range].fast,
        slow: RANGE[range].slow,
        look: RANGE[range].look,
        adx: 14,
        atr: 14,
      },
      fn: (k: SeriesCache) => tacticState(k, method.id, range),
    })),
  );
}
