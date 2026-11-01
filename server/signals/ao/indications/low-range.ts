/** Low-Range registry and Short-grid bridge. Its signals remain independently gated research candidates. */
import type { IndicationSpec } from "./registry.ts";
import type { LowRangeGrid, Protect } from "../domain/types.ts";
import { RT_COST } from "../model-cost.ts";
import { INTRADAY_TIMEFRAMES } from "./intraday-timeframes.ts";
import {
  DEFAULT_LOW_RANGE_POLICY, LOW_RANGE_IDS, LOW_RANGE_METHODS, LOW_RANGE_RELATIONS, LOW_RANGE_WINDOWS,
  lowRangeProtects, lowRangeState,
} from "./low-range-research.ts";

export { LOW_RANGE_IDS, LOW_RANGE_METHODS, LOW_RANGE_RELATIONS, LOW_RANGE_WINDOWS };
export const LOW_RANGE_TIMEFRAMES = INTRADAY_TIMEFRAMES;
export const isLowRangeInd = (id: string) => id.startsWith("low-range-");

/** Candidate calculation is registered; protection cells remain opt-in until forward validation supports them. */
export const DEFAULT_LOW_RANGE_GRID: LowRangeGrid = {
  enabled: false, step: DEFAULT_LOW_RANGE_POLICY.step, steps: [...DEFAULT_LOW_RANGE_POLICY.steps],
  // The same-feed 5m comparison (15/30/60/180m) selected 60m; longer time exits lost, shorter caps exited too early.
  slRatios: [...DEFAULT_LOW_RANGE_POLICY.slRatios], trailRatios: [...DEFAULT_LOW_RANGE_POLICY.trailRatios], maxHoldMin: 60,
};

export function lowRangeSpecs(): IndicationSpec[] {
  return LOW_RANGE_WINDOWS.flatMap((window) => LOW_RANGE_METHODS.flatMap((method) =>
    LOW_RANGE_RELATIONS.map((relation) => ({
      id: `low-range-${window}-${method}-${relation}`,
      kind: "low-range" as const,
      label: `Low-Range · ${method} · ${window} bars · ${relation}`,
      params: { window },
      fn: (k) => lowRangeState(k, window, method, relation),
    })),
  ));
}

/** Exact same effective cells are supplied to Base and tape construction. */
export function lowRangeGridProtects(grid: LowRangeGrid | undefined, cost = RT_COST, tfMin = 15): Protect[] {
  if (!grid?.enabled) return [];
  if (!(Number.isFinite(grid.maxHoldMin) && grid.maxHoldMin >= 1 && Number.isFinite(tfMin) && tfMin > 0))
    throw new RangeError("invalid Low-Range hold/timeframe");
  return lowRangeProtects({ step: grid.step, cost, steps: grid.steps, slRatios: grid.slRatios,
    trailRatios: grid.trailRatios, tags: ["sh"] }).map((p) => ({
      ...p, lowRangeOnly: true, hold: Math.floor(grid.maxHoldMin / tfMin), maxHoldMin: grid.maxHoldMin,
    }));
}
