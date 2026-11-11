// Relative Vigor Index research candidate. Parameters were frozen before market scoring in
// docs/signal-research-protocol-20261015.json; provenance and adaptations: docs/signal-research-sources.md.
import type { Bars } from "../domain/types.ts";
import type { IndicationSpec } from "./registry.ts";

type F64 = Float64Array;
const finite = Number.isFinite;

/**
 * MetaQuotes RVI: ratio of period sums of separately [1,2,2,1]-weighted close-open and high-low, then the same
 * normalized 4-bar signal filter. A zero denominator is unknown, never a fabricated oscillator value.
 * Input bars are completed bars. Gaps/invalid OHLC restart the full causal window; no later bar changes a prefix.
 */
export function relativeVigorIndex(b: Bars, period: number): { rvi: F64; signal: F64 } {
  if (!Number.isInteger(period) || period < 1)
    throw new RangeError("RVI period must be a positive integer");
  const { n, t, o, h, l, c } = b;
  const rvi = new Float64Array(n).fill(NaN);
  const signal = new Float64Array(n).fill(NaN);
  const numerator = new Float64Array(n).fill(NaN);
  const denominator = new Float64Array(n).fill(NaN);
  const step = b.tfMin * 60_000;
  let start = 0;
  for (let i = 0; i < n; i++) {
    const valid =
      finite(t[i]) &&
      finite(o[i]) &&
      finite(h[i]) &&
      finite(l[i]) &&
      finite(c[i]) &&
      h[i] >= Math.max(o[i], c[i], l[i]) &&
      l[i] <= Math.min(o[i], c[i], h[i]);
    if (!valid || !(step > 0)) {
      start = i + 1;
      continue;
    }
    if (i > 0 && t[i] - t[i - 1] !== step) start = i;
    if (i - start < 3) continue;
    numerator[i] =
      c[i] - o[i] + 2 * (c[i - 1] - o[i - 1]) + 2 * (c[i - 2] - o[i - 2]) + (c[i - 3] - o[i - 3]);
    denominator[i] =
      h[i] - l[i] + 2 * (h[i - 1] - l[i - 1]) + 2 * (h[i - 2] - l[i - 2]) + (h[i - 3] - l[i - 3]);
    if (i - start < period + 2) continue;
    // The frozen periods are 10/20: summing this short window avoids cancellation drift after a flat range.
    let up = 0;
    let down = 0;
    for (let j = i - period + 1; j <= i; j++) {
      up += numerator[j];
      down += denominator[j];
    }
    if (finite(up) && finite(down) && down > 0) rvi[i] = up / down;
    if (
      i - start >= period + 5 &&
      finite(rvi[i]) &&
      finite(rvi[i - 1]) &&
      finite(rvi[i - 2]) &&
      finite(rvi[i - 3])
    )
      signal[i] = (rvi[i] + 2 * rvi[i - 1] + 2 * rvi[i - 2] + rvi[i - 3]) / 6;
  }
  return { rvi, signal };
}

/** New events only: bullish cross above zero, bearish cross below zero. */
export function rviCross(b: Bars, period: number): Int8Array {
  const { rvi, signal } = relativeVigorIndex(b, period);
  const out = new Int8Array(b.n);
  for (let i = 1; i < b.n; i++) {
    if (!(finite(rvi[i]) && finite(signal[i]) && finite(rvi[i - 1]) && finite(signal[i - 1])))
      continue;
    if (rvi[i - 1] <= signal[i - 1] && rvi[i] > signal[i] && rvi[i] > 0) out[i] = 1;
    else if (rvi[i - 1] >= signal[i - 1] && rvi[i] < signal[i] && rvi[i] < 0) out[i] = -1;
  }
  return out;
}

export function research4Specs(): IndicationSpec[] {
  return [10, 20].map((period) => ({
    id: period === 10 ? "r-rvi" : "r-rvi-m",
    kind: "osc" as const,
    label: `Relative Vigor Index cross (${period === 10 ? "short" : "medium"})`,
    params: { period },
    fn: (k) => k.memo(`research4:rvi:${period}`, () => rviCross(k.b, period)),
  }));
}
