/** Price percentages in settings; modeled costs are fractions of notional. */
export const SL_TP_RATIOS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5] as const;
export type SlTpRatio = (typeof SL_TP_RATIOS)[number];
export const SL_TP_MIN = SL_TP_RATIOS[0];
export const SL_TP_MAX = SL_TP_RATIOS[SL_TP_RATIOS.length - 1];
export const SL_TP_STEP = SL_TP_RATIOS[1] - SL_TP_RATIOS[0];
export const MIN_SL_DISTANCE_PCT = 0.1;
export const MAX_SL_DISTANCE_PCT = 1;
export const DEFAULT_MIN_SL_DISTANCE_PCT = MIN_SL_DISTANCE_PCT;

export function normalizeMinSlDistancePct(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? Math.min(MAX_SL_DISTANCE_PCT, Math.max(MIN_SL_DISTANCE_PCT, n))
    : DEFAULT_MIN_SL_DISTANCE_PCT;
}

/** The initial stop pays for one modeled round trip plus the configured net distance. */
export function minimumStopFraction(costFraction: number, minSlDistancePct: unknown = DEFAULT_MIN_SL_DISTANCE_PCT): number {
  if (!Number.isFinite(costFraction) || costFraction < 0) throw new RangeError("Invalid modeled cost");
  return costFraction + normalizeMinSlDistancePct(minSlDistancePct) / 100;
}

export function minimumStopDistance(entry: number, costFraction: number, minSlDistancePct: unknown = DEFAULT_MIN_SL_DISTANCE_PCT): number {
  if (!Number.isFinite(entry) || entry <= 0) throw new RangeError("Invalid entry price");
  return entry * minimumStopFraction(costFraction, minSlDistancePct);
}

/** Raise only the initial stop. A ratio under the floor never silently moves the requested target. */
export function initialProtection(side: "long" | "short", entry: number, requestedSl: number, requestedTp: number,
  costFraction: number, minSlDistancePct: unknown = DEFAULT_MIN_SL_DISTANCE_PCT, venueMinFraction = 0) {
  if (![requestedSl, requestedTp, venueMinFraction].every(Number.isFinite) || requestedSl < 0 ||
      requestedTp <= 0 || venueMinFraction < 0) throw new RangeError("Invalid protection distance");
  const slDist = Math.max(requestedSl, minimumStopDistance(entry, costFraction, minSlDistancePct), entry * venueMinFraction);
  const tpDist = requestedTp;
  const signed = side === "long" ? 1 : -1;
  const sl = entry - signed * slDist, tp = entry + signed * tpDist;
  if (!(sl > 0) || !(tp > 0)) throw new RangeError("Protection lies outside positive prices");
  return { sl, tp, slDist, tpDist };
}
