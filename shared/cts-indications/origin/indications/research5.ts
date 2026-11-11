// Frozen research candidates, 2026-10-20. Primary formula sources and entry adaptations:
// docs/signal-research-20261020.md. Completed bars only; never auto-promoted to live by a literature citation.
import type { Bars } from "../domain/types.ts";
import type { IndicationSpec } from "./registry.ts";
import type { SeriesCache } from "./cache.ts";
import { ema } from "../math/indicators.ts";

/** Ehlers/MetaQuotes FRAMA, causal trailing halves (the platform's reverse-index i+Length is older data). */
export function frama(b: Bars, period: number): Float64Array {
  if (!(Number.isInteger(period) && period >= 4 && period % 2 === 0))
    throw new RangeError("FRAMA period must be even and at least four");
  const out = new Float64Array(b.n).fill(NaN);
  const half = period / 2;
  let previous = NaN;
  let start = 0;
  for (let i = 0; i < b.n; i++) {
    const valid = Number.isFinite(b.c[i]) && b.c[i] > 0 && Number.isFinite(b.h[i]) &&
      Number.isFinite(b.l[i]) && b.h[i] >= b.c[i] && b.l[i] <= b.c[i] && b.l[i] > 0;
    if (!valid) { start = i + 1; previous = NaN; continue; }
    if (i > 0 && b.t[i] - b.t[i - 1] !== b.tfMin * 60_000) { start = i; previous = NaN; }
    if (i - start < period - 1) { previous = b.c[i]; continue; }
    let high1 = -Infinity, high2 = -Infinity, low1 = Infinity, low2 = Infinity;
    for (let j = i - period + 1; j <= i; j++) {
      if (j <= i - half) { high1 = Math.max(high1, b.h[j]); low1 = Math.min(low1, b.l[j]); }
      else { high2 = Math.max(high2, b.h[j]); low2 = Math.min(low2, b.l[j]); }
    }
    const n1 = (high1 - low1) / half, n2 = (high2 - low2) / half;
    const n3 = (Math.max(high1, high2) - Math.min(low1, low2)) / period;
    const dimension = n1 > 0 && n2 > 0 && n3 > 0 ? Math.log((n1 + n2) / n3) / Math.LN2 : 1;
    const alpha = Math.max(0.01, Math.min(1, Math.exp(-4.6 * (dimension - 1))));
    previous = alpha * b.c[i] + (1 - alpha) * previous;
    out[i] = previous;
  }
  return out;
}

function events(k: SeriesCache, source: string, period: number, width: number): Int8Array {
  const b = k.b;
  const out = new Int8Array(b.n);
  if (source === "apz-reclaim") {
    const center = ema(ema(b.c, period), period);
    const range = Float64Array.from(b.h, (h, i) => h - b.l[i]);
    const spread = ema(ema(range, period), period);
    const adx = k.dmi(14).adx;
    for (let i = Math.max(2 * period, 28); i < b.n; i++) {
      const lowerPrev = center[i - 1] - width * spread[i - 1];
      const upperPrev = center[i - 1] + width * spread[i - 1];
      // Entry adaptation: require recovery inside the zone on a closed bar, with ADX below 25.
      if (!(adx[i] < 25 && spread[i] > 0)) continue;
      if (b.c[i - 1] <= lowerPrev && b.c[i] > center[i] - width * spread[i]) out[i] = 1;
      else if (b.c[i - 1] >= upperPrev && b.c[i] < center[i] + width * spread[i]) out[i] = -1;
    }
  } else if (source === "frama-pullback") {
    const average = frama(b, period);
    for (let i = period + 3; i < b.n; i++) {
      if (!(Number.isFinite(average[i]) && Number.isFinite(average[i - 3]))) continue;
      if (average[i] > average[i - 3] && b.c[i - 1] <= average[i - 1] && b.c[i] > average[i]) out[i] = 1;
      else if (average[i] < average[i - 3] && b.c[i - 1] >= average[i - 1] && b.c[i] < average[i]) out[i] = -1;
    }
  } else {
    const st = k.st(period, width);
    const fast = k.ema(9);
    for (let i = Math.max(30, period + 2); i < b.n; i++) {
      // A pullback/reclaim inside an unchanged Supertrend; distinct from the existing trend-flip source.
      if (st.dir[i] !== st.dir[i - 1]) continue;
      if (st.dir[i] === 1 && b.c[i] > st.line[i] && b.c[i - 1] <= fast[i - 1] && b.c[i] > fast[i]) out[i] = 1;
      else if (st.dir[i] === -1 && b.c[i] < st.line[i] && b.c[i - 1] >= fast[i - 1] && b.c[i] < fast[i]) out[i] = -1;
    }
  }
  return out;
}

export const RESEARCH5_SOURCES = ["apz-reclaim", "frama-pullback", "st-pullback"] as const;
export function research5Specs(): IndicationSpec[] {
  return RESEARCH5_SOURCES.flatMap((source) => [false, true].map((medium) => {
    const period = source === "apz-reclaim" ? (medium ? 8 : 5) : source === "frama-pullback" ? (medium ? 32 : 16) : (medium ? 14 : 7);
    const width = medium ? 2.5 : 2;
    const id = `r-${source}${medium ? "-m" : ""}`;
    return { id, kind: source === "apz-reclaim" ? "osc" : "trend", label: `${source} research (${medium ? "medium" : "short"})`,
      params: { period, width }, fn: (k: SeriesCache) => k.memo(`research5:${id}`, () => events(k, source, period, width)) } as IndicationSpec;
  }));
}
