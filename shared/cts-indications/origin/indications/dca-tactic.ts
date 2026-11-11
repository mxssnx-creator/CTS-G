/** Independent decline/recovery indications. Levels measure adverse distance, never an automatic volume raise. */
import type { IndicationSpec } from "./registry.ts";
import type { SeriesCache } from "./cache.ts";

export type DcaTacticMethod = "bounce" | "trend" | "vwap" | "sweep";
export type DcaTacticBasis = "atr" | "percent";
export interface DcaTacticConfig {
  lookback: number;
  basis: DcaTacticBasis;
  spacing: number;
  level: number;
  recovery: number;
  method: DcaTacticMethod;
}
export const DCA_TACTIC_GRID = {
  lookbacks: [8, 20, 40, 80],
  bases: ["atr", "percent"] as const,
  spacings: [0.5, 1],
  levels: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
  recoveries: [0.25, 0.5, 1],
  methods: ["bounce", "trend", "vwap", "sweep"] as const,
};
export const dcaTacticId = (c: DcaTacticConfig) =>
  `tactic-dca-${c.method}-${c.lookback}-${c.basis}-${c.spacing}-${c.level}-${c.recovery}`;
export function dcaTacticConfigs(
  grid: {
    lookbacks: readonly number[];
    bases: readonly DcaTacticBasis[];
    spacings: readonly number[];
    levels: readonly number[];
    recoveries: readonly number[];
    methods: readonly DcaTacticMethod[];
  } = DCA_TACTIC_GRID,
): DcaTacticConfig[] {
  const out: DcaTacticConfig[] = [];
  for (const lookback of grid.lookbacks)
    for (const basis of grid.bases)
      for (const spacing of grid.spacings)
        for (const level of grid.levels)
          for (const recovery of grid.recoveries)
            for (const method of grid.methods) {
              if (
                !["atr", "percent"].includes(basis) ||
                !["bounce", "trend", "vwap", "sweep"].includes(method) ||
                !Number.isInteger(lookback) ||
                lookback < 2 ||
                lookback > 400 ||
                !Number.isInteger(level) ||
                level < 1 ||
                level > 12 ||
                !(spacing > 0 && spacing <= 10) ||
                !(recovery > 0 && recovery <= 2)
              )
                throw new Error("Invalid Tactic decline grid");
              out.push({ lookback, basis, spacing, level, recovery, method });
            }
  if (out.length > 4096)
    throw new Error("Tactic grid exceeds 4096 independent source configurations");
  // Preserve the original 576 source indices; deeper levels are additive.
  if (grid === DCA_TACTIC_GRID) out.sort((a, b) => Number(a.level > 3) - Number(b.level > 3));
  return out;
}

type DcaFeatures = {
  peak: Float64Array;
  trough: Float64Array;
  highAt: Float64Array;
  lowAt: Float64Array;
  volume: Float64Array;
  vwapNum: Float64Array;
  tr: Float64Array;
  recent: Float64Array;
  previous: Float64Array;
  valid: Int8Array;
};
/** One bounded closed-bar scan per lookback, shared by all spacings, levels, recoveries and methods. */
function declineFeatures(k: SeriesCache, lookback: number): DcaFeatures {
  return k.memo(`tactic-dca:features:${lookback}`, () => {
    const b = k.b,
      names = [
        "peak",
        "trough",
        "highAt",
        "lowAt",
        "volume",
        "vwapNum",
        "tr",
        "recent",
        "previous",
        "valid",
      ] as const;
    const frame = k.streamMath?.frame<DcaFeatures, number>(
      `tactic-dca:features:${lookback}`,
      b.n,
      names,
      () => 0,
      ["valid"],
    );
    const out =
      frame?.cols ??
      (Object.fromEntries(
        names.map((name) => [name, name === "valid" ? new Int8Array(b.n) : new Float64Array(b.n)]),
      ) as DcaFeatures);
    for (let i = Math.max(2 * lookback, 16, frame?.from ?? 0); i < b.n; i++) {
      let high = -Infinity,
        low = Infinity,
        total = 0,
        weighted = 0,
        tr = 0,
        recent = 0,
        previous = 0,
        highAt = -1,
        lowAt = -1,
        valid = true;
      for (let j = i - 2 * lookback; j < i; j++) {
        if (
          ![b.o[j], b.h[j], b.l[j], b.c[j], b.v[j]].every(Number.isFinite) ||
          b.l[j] <= 0 ||
          b.h[j] < Math.max(b.o[j], b.c[j]) ||
          b.l[j] > Math.min(b.o[j], b.c[j]) ||
          b.v[j] < 0 ||
          (j > i - 2 * lookback && b.t[j] - b.t[j - 1] !== b.tfMin * 60000)
        ) {
          valid = false;
          break;
        }
        if (j < i - lookback) {
          previous += b.c[j];
          continue;
        }
        recent += b.c[j];
        if (b.h[j] >= high) {
          high = b.h[j];
          highAt = j;
        }
        if (b.l[j] <= low) {
          low = b.l[j];
          lowAt = j;
        }
        tr += Math.max(
          b.h[j] - b.l[j],
          Math.abs(b.h[j] - b.c[j - 1]),
          Math.abs(b.l[j] - b.c[j - 1]),
        );
        total += b.v[j];
        weighted += ((b.h[j] + b.l[j] + b.c[j]) / 3) * b.v[j];
      }
      if (
        !valid ||
        ![b.o[i], b.h[i], b.l[i], b.c[i], b.v[i]].every(Number.isFinite) ||
        b.l[i] <= 0 ||
        b.h[i] < Math.max(b.o[i], b.c[i]) ||
        b.l[i] > Math.min(b.o[i], b.c[i]) ||
        b.v[i] < 0 ||
        b.t[i] - b.t[i - 1] !== b.tfMin * 60000
      )
        continue;
      out.peak[i] = high;
      out.trough[i] = low;
      out.highAt[i] = highAt;
      out.lowAt[i] = lowAt;
      out.volume[i] = total;
      out.vwapNum[i] = weighted;
      out.tr[i] = tr;
      out.recent[i] = recent;
      out.previous[i] = previous;
      out.valid[i] = 1;
    }
    frame?.commit(0);
    return out;
  });
}
export function dcaTacticState(k: SeriesCache, c: DcaTacticConfig): Int8Array {
  return k.memo(dcaTacticId(c), () => {
    const b = k.b,
      features = declineFeatures(k, c.lookback);
    const frame = k.streamMath?.frame<{ value: Int8Array }, number>(
      dcaTacticId(c) + ":stream",
      b.n,
      ["value"],
      () => 0,
      ["value"],
    );
    const out = frame?.cols.value ?? new Int8Array(b.n);
    for (let i = Math.max(2 * c.lookback, 16, frame?.from ?? 0); i < b.n; i++) {
      if (!features.valid[i]) continue;
      const high = features.peak[i],
        low = features.trough[i],
        prev = b.c[i - 1],
        now = b.c[i];
      const unit =
        c.basis === "atr" ? (features.tr[i] / c.lookback) * c.spacing : (prev * c.spacing) / 100;
      if (!(unit > 0) || !Number.isFinite(unit)) continue;
      const depth = (high - low) / unit,
        bounce = unit * c.recovery;
      let long =
        features.highAt[i] < features.lowAt[i] &&
        depth >= c.level &&
        depth < c.level + 1 &&
        prev - low < bounce &&
        now - low >= bounce &&
        now > prev;
      let short =
        features.lowAt[i] < features.highAt[i] &&
        depth >= c.level &&
        depth < c.level + 1 &&
        high - prev < bounce &&
        high - now >= bounce &&
        now < prev;
      if (c.method === "trend") {
        long &&= features.recent[i] > features.previous[i];
        short &&= features.recent[i] < features.previous[i];
      }
      if (c.method === "vwap") {
        if (!(features.volume[i] > 0)) continue;
        const vw = features.vwapNum[i] / features.volume[i];
        long &&= now > vw;
        short &&= now < vw;
      }
      if (c.method === "sweep") {
        long &&= b.l[i] < low && now > low;
        short &&= b.h[i] > high && now < high;
      }
      if (long !== short) out[i] = long ? 1 : -1;
    }
    frame?.commit(0);
    return out;
  });
}

export function dcaTacticSpecs(): IndicationSpec[] {
  return dcaTacticConfigs().map((c) => ({
    id: dcaTacticId(c),
    kind: "tactic",
    label: `Tactic · DCA ${c.method} · ${c.lookback} / ${c.basis} ${c.spacing} · level ${c.level} · recovery ${c.recovery}`,
    params: {
      lookback: c.lookback,
      basis: c.basis === "atr" ? 0 : 1,
      spacing: c.spacing,
      level: c.level,
      recovery: c.recovery,
    },
    fn: (k) => dcaTacticState(k, c),
  }));
}
