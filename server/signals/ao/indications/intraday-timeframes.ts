/** Stable original ordering, followed by every additional subset containing30m. */
export const LEGACY_INTRADAY_SUBSETS = [[1], [5], [15], [1, 5], [1, 15], [5, 15], [1, 5, 15]] as const;
export const INTRADAY_TIMEFRAMES = [1, 5, 15, 30] as const;
export const INTRADAY_SUBSETS = [...LEGACY_INTRADAY_SUBSETS,
  [30], [1, 30], [5, 30], [15, 30], [1, 5, 30], [1, 15, 30], [5, 15, 30], [1, 5, 15, 30],
] as const;
const keys = new Set<string>(INTRADAY_SUBSETS.map((x) => x.join("x")));
/** Reject duplicate/out-of-order/unsupported frames instead of aliasing another config's history. */
export const intradayAnchor = (subset: string): number | null => keys.has(subset) ? Number(subset.split("x")[0]) : null;
