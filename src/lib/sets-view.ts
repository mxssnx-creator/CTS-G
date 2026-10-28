/**
 * The Sets overview: which Sets a view shows for a chosen step range and strategy type, and the post-Base figures of
 * exactly those Sets.
 *
 * The figures follow the engine (`SetBook._pf_split`): a Set is valid when it passes Base validation (`on`), the
 * effective PF is the pooled gross profit over the pooled gross loss of the valid Sets' gate windows, the unfiltered PF
 * pools every Set shown, and the validated share is valid / total. With the range "All" and the type "Normal" plus
 * "Trailing" together they equal `sets.pfBase`; a test pins that.
 */

/** One Set of `sets.index`. The gate-window fields are what the pooled PF needs; the others fill the table. */
export type SetIndexRow = {
  i: number;
  id: string;
  kind: "base" | "exit" | "trail";
  pack: string;
  /** SL ratio. */
  sl: number;
  /** Trailing key "arm:give" in percent; "" for a base Set. */
  tr: string;
  /** TP step (one step = one PositionCost); 0 for a trailing Set. */
  st: number;
  /** 1 when the Set passes Base validation. */
  on: number;
  /** PF over the gate window. */
  pf: number;
  /** Max drawdown time in seconds. */
  dd: number;
  /** All orders on the tape. */
  n?: number;
  /** Orders in the gate window. */
  gn?: number;
  /** Gross profit and gross loss (net fractions) inside the gate window. */
  gp?: number;
  gl?: number;
  /** Last-15 PF, last-25 average R, win rate %, expectancy, average hold (s), average drawdown time (s). */
  p15?: number;
  r25?: number;
  wr?: number;
  e?: number;
  h?: number;
  da?: number;
  /** Engine rank among the valid Sets (0 = first choice), -1 when not valid. */
  rk?: number;
};

export type StrategyType = "normal" | "exit" | "trailing" | "axis" | "block" | "dca";

/** The Set kinds a view shows: Normal = base Sets (take their TP), Exit = no-TP Sets (exitIgnoreTp), Trailing = trailing Sets. */
export const SET_VIEW_KIND: Partial<Record<StrategyType, string>> = { normal: "base", exit: "exit", trailing: "trail" };

export const STRATEGY_TYPES: Array<{ id: StrategyType; label: string; hint: string }> = [
  { id: "normal", label: "Normal", hint: "base Sets: pack x SL ratio x TP step, leave at their TP" },
  { id: "exit", label: "Exit", hint: "Exit Sets (exitIgnoreTp): the base stop with no TP, own lanes and validation" },
  { id: "trailing", label: "Trailing", hint: "independent trailing Sets: pack x arm x give" },
  { id: "axis", label: "Axis", hint: "the coordinator's axes and stage PFs" },
  { id: "block", label: "Block", hint: "Block counts and lanes" },
  { id: "dca", label: "DCA", hint: "DCA steps and lanes" },
];

export type StepRange = { id: string; label: string; min: number; max: number };

/** "All" first and the default. 2-6 is the Short Range band. */
export const STEP_RANGES: StepRange[] = [
  { id: "all", label: "All", min: 0, max: Number.POSITIVE_INFINITY },
  { id: "2-6", label: "2–6", min: 2, max: 6 },
  { id: "7-12", label: "7–12", min: 7, max: 12 },
  { id: "13-20", label: "13–20", min: 13, max: 20 },
  { id: "21-30", label: "21–30", min: 21, max: 30 },
];

export const DEFAULT_RANGE_ID = "all";
export const DEFAULT_TYPE: StrategyType = "normal";

export function rangeById(id: string | null | undefined): StepRange {
  return STEP_RANGES.find((r) => r.id === id) ?? STEP_RANGES[0];
}

export function typeById(id: string | null | undefined): StrategyType {
  return STRATEGY_TYPES.find((t) => t.id === id)?.id ?? DEFAULT_TYPE;
}

/** The arm of a trailing key in percent ("0.60:0.36" -> 0.6), NaN when the key does not parse. */
export function trailArmPct(key: string): number {
  const arm = Number.parseFloat(String(key).split(":")[0]);
  return Number.isFinite(arm) ? arm : Number.NaN;
}

/**
 * The step a Set is filtered by: the TP step of a base Set, the arm in PositionCost units for a trailing Set (an arm of
 * 0.60 % at a cost of 0.12 % is step 5).
 */
export function stepOf(row: SetIndexRow, costPct: number): number {
  if (row.kind === "trail") {
    const arm = trailArmPct(row.tr);
    return Number.isFinite(arm) && costPct > 0 ? Math.round(arm / costPct) : Number.NaN;
  }
  return row.st;
}

export function inRange(step: number, range: StepRange): boolean {
  if (range.id === "all") return true;
  return Number.isFinite(step) && step >= range.min && step <= range.max;
}

/** The Sets a view shows. Axis, Block and DCA are not Sets: they have their own panels and select none here. */
export function filterSets(
  index: readonly SetIndexRow[] | null | undefined,
  type: StrategyType,
  rangeId: string,
  costPct: number,
): SetIndexRow[] {
  const kind = SET_VIEW_KIND[type];
  if (!index || !kind) return [];
  const range = rangeById(rangeId);
  return index.filter((r) => r.kind === kind && inRange(stepOf(r, costPct), range));
}

export type SetsSummary = {
  total: number;
  valid: number;
  /** Share of the shown Sets that pass Base validation, percent with two decimals. */
  validatedPct: number;
  /** Effective PF: pooled over the valid Sets' gate windows. null when no Set is valid. */
  effectivePf: number | null;
  /** Orders behind it: the valid Sets' gate-window orders. */
  orders: number;
  /** PF of every shown Set pooled the same way (under 1 by design: the Sets Base exists to drop are in it). */
  unfilteredPf: number | null;
};

function pooledPf(rows: readonly SetIndexRow[]): number | null {
  if (rows.length === 0) return null;
  let gp = 0;
  let gl = 0;
  for (const r of rows) {
    gp += Number(r.gp ?? 0);
    gl += Number(r.gl ?? 0);
  }
  const pf = gl > 0 ? gp / gl : gp > 0 ? 99 : 0;
  return Math.round(pf * 1e4) / 1e4;
}

export function summarize(rows: readonly SetIndexRow[]): SetsSummary {
  const valid = rows.filter((r) => r.on === 1);
  return {
    total: rows.length,
    valid: valid.length,
    validatedPct: rows.length ? Math.round((10000 * valid.length) / rows.length) / 100 : 0,
    effectivePf: pooledPf(valid),
    orders: valid.reduce((a, r) => a + Number(r.gn ?? 0), 0),
    unfilteredPf: pooledPf(rows),
  };
}

/**
 * Table order: valid Sets first, in the engine's own order (`rk`: stable Sets with many orders first); then the rest by
 * PF. A row without a rank keeps its PF order, so an older engine's stats still sort sensibly.
 */
export function rankRows(rows: readonly SetIndexRow[]): SetIndexRow[] {
  return [...rows].sort((a, b) => {
    if (a.on !== b.on) return b.on - a.on;
    if (a.on === 1 && a.rk !== undefined && b.rk !== undefined && a.rk !== b.rk) return a.rk - b.rk;
    if (a.pf !== b.pf) return b.pf - a.pf;
    return Number(b.n ?? 0) - Number(a.n ?? 0);
  });
}

/** Per-pack split of a summary (the Base validation reports its share per strategy pack). */
export function summarizeByPack(rows: readonly SetIndexRow[]): Record<string, SetsSummary> {
  const packs = [...new Set(rows.map((r) => r.pack))].sort();
  const out: Record<string, SetsSummary> = {};
  for (const p of packs) out[p] = summarize(rows.filter((r) => r.pack === p));
  return out;
}

/** Counts of the shown Sets by step, for the distribution chart: { step: { total, valid } } in step order. */
export function stepHistogram(rows: readonly SetIndexRow[], costPct: number): Array<{ step: number; total: number; valid: number }> {
  const acc = new Map<number, { total: number; valid: number }>();
  for (const r of rows) {
    const s = stepOf(r, costPct);
    if (!Number.isFinite(s)) continue;
    const e = acc.get(s) ?? { total: 0, valid: 0 };
    e.total += 1;
    if (r.on === 1) e.valid += 1;
    acc.set(s, e);
  }
  return [...acc.entries()].sort((a, b) => a[0] - b[0]).map(([step, e]) => ({ step, ...e }));
}

/** Effective PF per step: the valid Sets of that step pooled the same way as the summary. A step with no valid Set has no PF. */
export function pfByStep(
  rows: readonly SetIndexRow[],
  costPct: number,
): Array<{ step: number; pf: number | null; valid: number; orders: number }> {
  const by = new Map<number, SetIndexRow[]>();
  for (const r of rows) {
    if (r.on !== 1) continue;
    const s = stepOf(r, costPct);
    if (!Number.isFinite(s)) continue;
    by.set(s, [...(by.get(s) ?? []), r]);
  }
  return [...by.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([step, rs]) => ({ step, pf: pooledPf(rs), valid: rs.length, orders: rs.reduce((a, r) => a + Number(r.gn ?? 0), 0) }));
}

/** A PF for display: a window without a loss has no finite PF (the engine reports 99), so it reads "no loss" rather than 99.00. */
export function formatPf(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  return Number(v) >= 99 ? "no loss" : Number(v).toFixed(digits);
}
