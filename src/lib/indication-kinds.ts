/**
 * One list of indication kinds for the desk. Mirrors the engine's
 * contracts.INDICATION_KINDS; every per-kind table (hints, short codes,
 * historic calc toggles) is generated from it so a kind cannot go missing.
 */
export const INDICATION_KINDS = [
  "state", "signals", "active", "direction", "move", "common", "trend", "break",
  "msi", "vwap", "retest", "squeeze", "sweep", "rsi2", "keltner", "impulse",
] as const;

export type IndicationKind = (typeof INDICATION_KINDS)[number];

/** Wording shared with the Settings › Indication types sliders. */
export const KIND_HINT: Record<IndicationKind, string> = {
  state: "tf_combined + low-stop consensus — the Indication",
  signals: "per-TF evaluateSignalCandles",
  active: "outbreak 3/5/10 vs previous window",
  direction: "post-reversal two-window, independent Long/Short",
  move: "same-dir displacement, independent Long/Short",
  common: "RSI + MACD + EMA + Bollinger",
  trend: "trend slope / direction vote",
  break: "breakout / range vote",
  msi: "price extreme not confirmed by RSI · exit: swing fail",
  vwap: "z-stretch from VWAP on a volume surge · exit: VWAP touch",
  retest: "broken level retested and held · exit: level fail",
  squeeze: "low band-width percentile, close outside band · exit: back through mid",
  sweep: "wick through range high/low, close back inside · exit: beyond the wick",
  rsi2: "RSI(2) extreme against the EMA trend · exit: EMA5 snap-back",
  keltner: "wick outside Keltner band + StochRSI turn · exit: middle line",
  impulse: "sigma impulse bar on a volume surge · exit: beyond the extreme",
};

/** Unique 2-3 letter codes for compact per-symbol vote strips. */
export const KIND_SHORT: Record<IndicationKind, string> = {
  state: "St",
  signals: "Sg",
  active: "Ac",
  direction: "Dir",
  move: "Mv",
  common: "Cm",
  trend: "Tr",
  break: "Brk",
  msi: "Msi",
  vwap: "Vw",
  retest: "Rt",
  squeeze: "Sq",
  sweep: "Sw",
  rsi2: "Rsi",
  keltner: "Kc",
  impulse: "Im",
};

export function kindShort(kind: string): string {
  return (KIND_SHORT as Record<string, string>)[kind] ?? kind.slice(0, 3);
}

export function kindLabel(kind: string): string {
  return kind ? kind[0].toUpperCase() + kind.slice(1) : kind;
}

/** Historic calc / overlay toggle name for one kind (`indTypeState`). */
export function indTypeKey(kind: string): `indType${string}` {
  return `indType${kindLabel(kind)}`;
}

export const IND_TYPE_KEYS = INDICATION_KINDS.map(indTypeKey);

/** Coverage flags win over the scan blob; a kind missing from both is unknown, not on. */
export function mergeKindTypes(
  scan: Record<string, boolean | undefined> | null | undefined,
  coverage: Record<string, boolean | undefined> | null | undefined,
): Record<string, boolean | undefined> {
  return { ...(scan || {}), ...(coverage || {}) };
}
