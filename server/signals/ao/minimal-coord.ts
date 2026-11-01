/**
 * Minimal Coord.
 * Position-cost ranges, independent of the wide protect grid. TP multiples use the named 0.2 % reference basis
 * (kept stable across changes to the modeled execution cost):
 *   Minimal  4–8×   (0.8–1.6 %, step 1×)
 *   Short    8–14×  (1.6–2.8 %, step 1×)
 *   General  14–22× (2.8–4.4 %, step 2×)
 *   Long     22–32× (4.4–6.4 %, step 2×)
 * A boundary multiple (8, 14, 22) belongs to the lower range, so no cell is computed twice.
 * Micro net targets use that reference basis; their price targets add the configured execution cost. Minimal plus stay
 * optional. Wide targets stay in the main grid.
 */
import type { Gates, MicroGrid, MinimalGrid, ProtectGridSpec, RangeMinPfKey, RangeTag } from "./domain/types.ts";
import type { MicroIndRule } from "./indications/micro.ts";
import { POSITION_COST_RT, RT_COST } from "./model-cost.ts";
import { SIGNAL_MIN_PF } from "./signal-policy.ts";
import { profitFactorRatio } from "./metrics/profit-factor.mjs";

export const MINIMAL_COORD = "Minimal Coord.";

/** Display names of the ranges ("" = the wide grid). */
export const RANGE_LABEL: Record<RangeTag | "", string> = {
  "": "Wide",
  mn: "Minimal",
  sh: "Short",
  gn: "General",
  lg: "Long",
  mc: "Micro",
  mp: "Minimal plus",
};

/** Every range tag, smallest targets first. */
export const RANGE_TAGS: readonly RangeTag[] = ["mc", "mn", "mp", "sh", "gn", "lg"];

/** Execution volume by target range. The balance-relative default uses neutral volume in every range. */
export const RANGE_VOLUME_FACTOR_DEFAULTS: Readonly<Record<"wide" | RangeTag, number>> = {
  wide: 1,
  mc: 1,
  mn: 1,
  mp: 1,
  sh: 1,
  gn: 1,
  lg: 1,
};

export type RangeVolumeFactors = Partial<Record<"wide" | RangeTag, number>>;

/** Resolved sizing factors for every strategy target range, including the untagged Wide grid. */
export function rangeVolumeFactorsOf(g?: Partial<ProtectGridSpec> | null): RangeVolumeFactors {
  const factor = (r: { volumeFactor?: number } | false | undefined, key: "wide" | RangeTag) =>
    r && typeof r === "object" && r.volumeFactor !== undefined ? r.volumeFactor : RANGE_VOLUME_FACTOR_DEFAULTS[key];
  const plus = g?.minimalPlus;
  return {
    wide: g?.volumeFactor ?? RANGE_VOLUME_FACTOR_DEFAULTS.wide,
    mc: factor(g?.micro, "mc"),
    mn: factor(g?.minimal, "mn"),
    mp: factor(plus === false ? false : plus, "mp"),
    sh: factor(g?.short, "sh"),
    gn: factor(g?.general, "gn"),
    lg: factor(g?.long, "lg"),
  };
}

/** Range of a config id ("" = the wide grid). */
export function rangeOfId(id: string | undefined): RangeTag | "" {
  if (!id) return "";
  // memoized: called per order in every simulation step; the regexp match cost 0.8 s of a 24-symbol compute
  let r = RANGE_OF_ID.get(id);
  if (r === undefined) {
    const m = /\|(mc|mp|mn|sh|gn|lg)(?=\||$)/.exec(id);
    r = m ? (m[1] as RangeTag) : "";
    if (RANGE_OF_ID.size > 200_000) RANGE_OF_ID.clear();
    RANGE_OF_ID.set(id, r);
  }
  return r;
}
const RANGE_OF_ID = new Map<string, RangeTag | "">();

/** Fixed historical range-construction basis; changing modeled costs must not silently shrink configured distances. */
export const RANGE_COST_BASIS = POSITION_COST_RT;

/**
 * One range's settings. The type lives in domain/types.ts as `RangeGrid` + `MicroGrid` so the Settings surface and
 * the engine read the same levers — every range takes every lever, and `checkSettings` refuses the three Micro-only
 * ones on another range instead of ignoring them. The engine walks ranges generically, so it uses the Micro shape.
 */
export type CoordRange = MicroGrid;

/**
 * Ranges judged in Base at their own cell by default. 12 h, 3 October 04-16 UTC: Minimal at its own cell 375
 * trades PF 2.14 (+119) against 106 / 1.15 at the default; Short, General and Long at their own cells let 3x the
 * pairs through and fell to PF 1.02 / 0.85 / 0.84 (1.61 / 1.10 / 4.00 at the default).
 */
export const RANGE_OWN_BASE: Readonly<Partial<Record<RangeTag, boolean>>> = { mc: true, mn: true };

/**
 * Default shortest lane per range. Short: 24 h of x01's live settings (2 Oct 18:00 – 3 Oct 18:00, 20 symbols) lost
 * −5,558 on Short's 1m / 5m lanes (11,327 orders, PF 0.66 of the whole run); on 15m and slower PF 1.12, +38.8 %.
 * General and Long targets (3.2–6.4 %) are hours of movement: on 1m / 5m lanes
 * the signal says nothing about a move that size (12 h, 3 October, 02–14 UTC: General PF 0.16 on 1m and 0.45 on
 * 5m against 1.52 on 15m; Long 0.17 / 0.45 against 1.25 on 15m and 3.12 on 30m).
 * Micro: the 1m lane fires 5–10× more often than 5m and is the worst lane of every Micro indication (50 symbols ×
 * 20 days, 14 Sep – 4 Oct: best Micro cell PF 0.65 on 1m against 0.68–0.87 on 5m / 15m / 30m; the 1m move after a
 * one-bar stretch is symmetric, so the 0.2 % cost decides every trade) — 5m and slower.
 */
export const RANGE_MIN_TF: Readonly<Partial<Record<RangeTag, number>>> = { mc: 5, sh: 15, gn: 15, lg: 15 };

/** Shortest lane per range tag of a grid (tags without one are absent: every lane). */
export function rangeMinTfOf(g: {
  micro?: CoordRange | false;
  minimal?: CoordRange | false;
  short?: CoordRange | false;
  general?: CoordRange | false;
  long?: CoordRange | false;
  wideMinTf?: number;
}): Partial<Record<RangeTag | "wide", number>> {
  const out: Partial<Record<RangeTag | "wide", number>> = {};
  // Wide (untagged Normal / Trailing cells): its own lever, no default
  if (typeof g.wideMinTf === "number" && g.wideMinTf > 0) out.wide = g.wideMinTf;
  for (const [tag, r] of [
    ["mc", g.micro],
    ["mn", g.minimal],
    ["sh", g.short],
    ["gn", g.general],
    ["lg", g.long],
  ] as const) {
    const v = r ? (r.minTf ?? RANGE_MIN_TF[tag]) : undefined;
    if (typeof v === "number" && v > 0) out[tag] = v;
  }
  return out;
}

/**
 * Plain plus three trailing distances: a quarter, half and three quarters of the target (operator, 13 Oct: "all TP ×
 * SL × Trail" in every range). A trail of the whole target is not a config: it arms only when the move reaches the
 * target, which fills at that bar (trailFree off), so it is the plain cell again.
 */
export const TRAIL_CONFIGS = [0, 0.25, 0.5, 0.75] as const;

/** TP multiples of the fixed range-construction basis, lo..hi by step (whole multiples). */
export const costTps = (lo: number, hi: number, step: number): number[] =>
  steps(lo, hi, step).map((n) => +(RANGE_COST_BASIS * n).toFixed(4));

/**
 * Minimal: 4–8× cost (0.8–1.6 %), step 0.5× (13 Oct, was 1×). Stops 1.5–3× the target, step 0.25 (MINIMAL_SL).
 * Every trailing distance; a trailing cell keeps its own stop ratio from 1× (a forced 2× stop made each loss twice
 * the trailed win: 12 h, 3 October, Trailing PF 1.04 → 1.47 and the engine 1.15 → 1.49 with 1×).
 */
/**
 * The minimum stop of EVERY evaluated config (operator, 5 Oct): 0.5 % of price. A tighter stop sits inside the
 * spread and ordinary noise — it is taken out before the target can be reached whatever the entry's edge (the Micro
 * cells at 0.1-0.2 % stops traded at a 0 % win rate). Every grid, Base cell, DCA rung and Axis desk floor is held
 * to it, so no setting can evaluate a config below it.
 */
export const EVAL_MIN_SL = 0.005;

/**
 * Minimal's stops widened (operator, 6 Oct: "set Minimal stop loss wider and min trailing a bit wider"): stop ratios
 * 1.5–3× the target (was 1–2×; the measured best Minimal ratio, 2.0, was the top of the old ladder), the stop floor
 * 0.6 % (was 0.5 %) and the trailing floor 0.3 % (was 0.2 %) — so a trailed Minimal position is not stopped by
 * ordinary noise before its 0.8–1.6 % target.
 */
export const MINIMAL_SL: readonly number[] = steps(1.5, 3, 0.25);
export const MINIMAL_RANGE: CoordRange = {
  volumeFactor: 1,
  tp: costTps(4, 8, 0.5),
  slOfTp: MINIMAL_SL,
  trailOfTp: TRAIL_CONFIGS,
  trailSlOfTp: 1,
  minSl: +(RANGE_COST_BASIS * 3).toFixed(4),
  minTrail: +(RANGE_COST_BASIS * 1.5).toFixed(4),
};

/** Short's stop ratios: 1–2.5× the target, step 0.25 (13 Oct, was 1 / 1.5 / 2). */
export const SHORT_SL: readonly number[] = steps(1, 2.5, 0.25);
/** Short: 9–14× cost (1.8–2.8 %), step 1× (8× belongs to Minimal). Stops 1–2.5× the target, trailing cells too. */
export const SHORT_RANGE: CoordRange = {
  volumeFactor: 1,
  tp: costTps(9, 14, 1),
  slOfTp: SHORT_SL,
  trailOfTp: TRAIL_CONFIGS,
  trailSlOfTp: 1,
  minSl: +(RANGE_COST_BASIS * 3).toFixed(4),
  minTrail: +RANGE_COST_BASIS.toFixed(4),
};

/**
 * General and Long stop ratios (operator, 13 Oct: "SL ratios from 0.25 to 1.5, step 0.25"): 0.25–1.5× the target,
 * on the plain and the trailing cells alike (trailSlOfTp 0.25: every TP × SL × Trail is a config). The range's stop
 * floor (minSl) holds the tightest ratios at the floor.
 */
export const WIDE_TARGET_SL: readonly number[] = steps(0.25, 1.5, 0.25);

/** General: 15–22× cost (3.0–4.4 %), step 1× (13 Oct, was 16–22 step 2). Stops 0.25–1.5× the target (WIDE_TARGET_SL). */
export const GENERAL_RANGE: CoordRange = {
  volumeFactor: 1,
  tp: costTps(15, 22, 1),
  slOfTp: WIDE_TARGET_SL,
  trailOfTp: TRAIL_CONFIGS,
  trailSlOfTp: 0.25,
  minSl: +(RANGE_COST_BASIS * 5).toFixed(4),
  minTrail: +(RANGE_COST_BASIS * 2).toFixed(4),
};

/** Long: 23–32× cost (4.6–6.4 %), step 1× (13 Oct, was 24–32 step 2). Stops 0.25–1.5× the target (WIDE_TARGET_SL). */
export const LONG_RANGE: CoordRange = {
  volumeFactor: 1,
  tp: costTps(23, 32, 1),
  slOfTp: WIDE_TARGET_SL,
  trailOfTp: TRAIL_CONFIGS,
  trailSlOfTp: 0.25,
  minSl: +(RANGE_COST_BASIS * 8).toFixed(4),
  minTrail: +(RANGE_COST_BASIS * 3).toFixed(4),
};

/**
 * Micro (operator, 13 Oct: "Micro range 2–5×"): price targets are the reference-basis net target plus configured cost;
 * the 1–4× reference-basis net targets are 0.2–0.8 %, and default-cost price targets are 0.35–0.95 %. Every stop
 * ratio is 1–5× (step 0.25, MICRO_SL's 17 ratios) of the price target, plain and both trailing distances.
 * Traded only by the Micro indications (ownInds); the Base PF evaluation decides which cells run. Before (price
 * targets 0.20–0.40 %): no cell above PF 1 — a 0.2 % target nets nothing after the 0.2 % cost, a 0.4 % one needs a
 * win rate above 75 % at a 1× stop, and the one-bar reversal events have no gross edge on the 1m lane. With the net
 * targets (50 symbols × 20 days, 14 Sep – 4 Oct 2026, 18 Micro indications × 4 lanes × 273 cells, engine simulation)
 * still no cell clears PF 1: the best 0.87 (30m, 0.6 % / 3.5×), the Base cell 0.27–0.58 — Base passes no Micro pair,
 * so the range trades nothing until an entry wins 80–90 % of 0.3–0.6 % targets.
 */
export const MICRO_TP: readonly number[] = costTps(1, 4, 0.5);
/** The Micro net targets before 13 Oct (price 1.5–3× cost): the settings migration v25 moves a stored copy. */
export const FORMER_MICRO_TP: readonly number[] = [0.001, 0.0015, 0.002, 0.0025, 0.003, 0.0035, 0.004];
/**
 * Micro stop ratios: from 1.0 of the PRICE target upward (operator, 5 Oct — "run SL from ratio 1.0 away,
 * additionally to the position cost situation"). The price target already includes the round-trip cost
 * (tpNetOfCost), so ratio 1.0 means the stop is the whole net target plus the cost away, never inside it; the
 * tighter 0.5 / 0.75 ratios were stopped out by noise before the target could be reached.
 */
export const MICRO_SL: readonly number[] = steps(1, 5, 0.25);
export const MICRO_RANGE: CoordRange = {
  volumeFactor: 1,
  tp: MICRO_TP,
  slOfTp: MICRO_SL,
  trailOfTp: TRAIL_CONFIGS,
  /** Trailing cells keep the stated stop ratio from 1×. */
  trailSlOfTp: 1,
  minSl: EVAL_MIN_SL,
  // at least half the 0.2% PositionCost (13 Oct, with the ¼ trail share); every other range's trail floor is 0.2 % or more
  minTrail: +(RANGE_COST_BASIS * 0.5).toFixed(4),
  tpNetOfCost: true,
  /** the stop floor net of configured cost; at defaults 0.20 % net + 0.15 % cost = 0.35 % price distance */
  minSlNet: 0.002,
};

/**
 * The price target of a Micro cell: with tpNetOfCost (default on) the configured net target plus the round-trip cost
 * (0.1 % net at a 0.2 % cost → 0.3 %), else the configured value.
 */
export function microPriceTp(tp: number, range: Pick<CoordRange, "tpNetOfCost"> | false | undefined, cost = RT_COST): number {
  const c = Number.isFinite(cost) && cost > 0 ? cost : 0;
  return +(range && range.tpNetOfCost === false ? tp : tp + c).toFixed(6);
}

/**
 * The Micro NET targets the range computes at this cost: its `tp` list, minus the ones below
 * `minNetOfCost` × cost (0 / unset = all of them).
 */
export function microNetTps(
  range: Pick<CoordRange, "tp" | "minNetOfCost"> | false | undefined,
  cost = RT_COST,
): number[] {
  if (!range) return [];
  const c = Number.isFinite(cost) && cost > 0 ? cost : 0;
  const floor = (range.minNetOfCost ?? 0) * c;
  return floor > 0 ? range.tp.filter((x) => x >= floor - 1e-12) : [...range.tp];
}

/** "0.30 % (net 0.10 %)" for a Micro price target at this cost; other ranges: "0.30 %". */
export function rangeTpLabel(tp: number, tag: string | null | undefined, cost = RT_COST): string {
  const pct = (x: number) => `${(x * 100).toFixed(2)} %`;
  return tag === "mc" ? `${pct(tp)} (net ${pct(tp - cost)})` : pct(tp);
}

type GridSlice = Pick<
  ProtectGridSpec,
  "holdH" | "minSl" | "minTrail" | "short" | "minimal" | "general" | "long" | "micro"
>;
/** The position-cost ranges built by forEachCoord, smallest first. */
export type CoordTag = "mn" | "sh" | "gn" | "lg";

/**
 * The holds (hours) a range's cells are built at: its own hold limit (maxHoldMin, minutes) when set, else the grid's holdH.
 */
export function rangeHolds(range: Pick<CoordRange, "maxHoldMin"> | false | undefined, holdH: readonly number[]): readonly number[] {
  return range && range.maxHoldMin != null && range.maxHoldMin > 0 ? [range.maxHoldMin / 60] : holdH;
}

/** The shortest hold, in bars, a cell of this range may be given: the engine floor of 2, or 1 when the range sets a MAXIMUM hold time (15 min on a 15m grid). It is a floor on the value of the maximum, not a minimum hold: a position still closes earlier at target, stop or trail. */
export function holdFloorBars(range: Pick<CoordRange, "maxHoldMin"> | false | undefined): number {
  return range && range.maxHoldMin != null && range.maxHoldMin > 0 ? 1 : 2;
}

/** Cells a range adds before identical distances collapse. */
export function coordVariants(holdN: number, range: CoordRange | false | undefined): number {
  if (!range) return 0;
  const hold = range.maxHoldMin != null && range.maxHoldMin > 0 ? 1 : holdN || 1;
  return range.tp.length * range.slOfTp.length * range.trailOfTp.length * hold;
}

/** Emit every minimal, short, general and long cell. Trailing cells use at least trailSlOfTp. */
export function forEachCoord(
  g: GridSlice,
  emit: (
    tp: number,
    slRatio: number,
    trailRatio: number,
    holdH: number,
    minSl: number,
    minTrail: number,
    tag: CoordTag,
    /** this range's own evaluation stop floor, in place of the grid-wide floor */
    slEval?: number,
  ) => void,
): void {
  for (const [tag, range] of [
    ["mn", g.minimal],
    ["sh", g.short],
    ["gn", g.general],
    ["lg", g.long],
  ] as const) {
    if (!range) continue;
    const minSl = range.minSl ?? g.minSl;
    const minTrail = range.minTrail ?? g.minTrail;
    const trailStop = range.trailSlOfTp ?? 2;
    for (const tp of range.tp)
      for (const k of range.slOfTp)
        for (const tr of range.trailOfTp)
          for (const h of rangeHolds(range, g.holdH))
            emit(tp, tr > 0 ? Math.max(k, trailStop) : k, tr, h, minSl, minTrail, tag, range.minSlEval);
  }
}

/** Keep normal distance rounding unless it would put a configured hard minimum below its price precision. */
export function distanceAtFloor(distance: number, minimum: number, digits: number): number {
  const rounded = +Math.max(distance, minimum).toFixed(digits);
  // Ordinary binary roundoff is not a missing price quantum; nearest rounding otherwise stays unchanged.
  if (rounded + Number.EPSILON * Math.max(Math.abs(rounded), minimum) >= minimum) return rounded;
  const scale = 10 ** digits;
  return Math.ceil(minimum * scale) / scale;
}

/** Round an arming distance without letting its actual trailing gap fall below the configured minimum. */
export function trailAtFloor(activation: number, minTrail: number, step: number, digits: number): number {
  return distanceAtFloor(activation, minTrail / step, digits);
}


/** Micro's stop floor from `minSlNet`: the net floor plus the round-trip cost (undefined = not set). */
export function microSlFloor(range: Pick<CoordRange, "minSlNet"> | false | undefined, cost = RT_COST): number | undefined {
  if (!range || range.minSlNet == null || !Number.isFinite(range.minSlNet)) return undefined;
  const c = Number.isFinite(cost) && cost > 0 ? cost : 0;
  return +(range.minSlNet + c).toFixed(6);
}

/**
 * Every micro cell, its target as the PRICE target (net + cost with tpNetOfCost, see microPriceTp). The stop ratio is
 * the one configured, including on a trailing cell, and applies to the price target.
 */
export function forEachMicro(
  g: { holdH: readonly number[]; micro?: CoordRange | false },
  emit: (
    tp: number,
    slRatio: number,
    trailRatio: number,
    holdH: number,
    minSl: number,
    minTrail: number,
    /** the range's own evaluation stop floor, when it sets one */
    slEval?: number,
  ) => void,
  cost = RT_COST,
): void {
  const range = g.micro;
  if (!range) return;
  // minSlNet: the floor follows the cost (net floor + round trip) and stands in for minSl and minSlEval
  const net = microSlFloor(range, cost);
  const minSl = net ?? range.minSl ?? 0.001;
  const minTrail = range.minTrail ?? 0.0005;
  for (const tp of [...new Set(microNetTps(range, cost).map((x) => microPriceTp(x, range, cost)))])
    for (const k of range.slOfTp)
      for (const tr of range.trailOfTp)
        for (const h of rangeHolds(range, g.holdH)) emit(tp, k, tr, h, minSl, minTrail, net ?? range.minSlEval);
}

/** 2×–5× position cost, step 0.25. Stops 0.5×–3× in steps of 0.25. Off unless a setting enables it. */
export const MINIMAL_PLUS_TP = steps(2, 5, 0.25).map((n) => +(RANGE_COST_BASIS * n).toFixed(4));
export const MINIMAL_PLUS_SL = steps(0.5, 3, 0.25);
export const MINIMAL_PLUS_TRAIL = TRAIL_CONFIGS;
/** Trailing cells use a higher stop than the short range (2×): at least 2.5× the target. */
export const MINIMAL_PLUS_TRAIL_SL = 2.5;

export const MINIMAL_PLUS_RANGE: CoordRange = {
  volumeFactor: 1,
  tp: MINIMAL_PLUS_TP,
  slOfTp: MINIMAL_PLUS_SL,
  trailOfTp: MINIMAL_PLUS_TRAIL,
  trailSlOfTp: MINIMAL_PLUS_TRAIL_SL,
  minSl: +(RANGE_COST_BASIS * 0.5).toFixed(4),
  minTrail: +(RANGE_COST_BASIS * 0.5).toFixed(4),
};

export interface MinimalPlusSettings {
  /** Off by default. On: only the selected cells are built, then kept if the last N clear min PF. */
  enabled: boolean;
  /** Previous closes required. Never below 50. */
  lastN: number;
  /** min PF over the last N closes (default 1.35; never below the Base minimum 1.05) */
  minPf: number;
  /** Cells the test kept. Empty until a run clears the gate. */
  cells?: ReadonlyArray<{ tp: number; sl: number; trail: number }>;
}

export function minimalPlusSettings(p?: Partial<MinimalPlusSettings> | null): MinimalPlusSettings {
  const lastN = Number(p?.lastN);
  const minPf = Number(p?.minPf);
  return {
    enabled: p?.enabled === true,
    lastN: Number.isFinite(lastN) ? Math.max(50, Math.round(lastN)) : 50,
    minPf: Number.isFinite(minPf) ? Math.max(1.05, minPf) : 1.35,
    cells: p?.cells ?? [],
  };
}

/** Cells a enabled plus-range adds. Disabled, or enabled with no selected cells, adds none. */
export function plusVariants(enabled: boolean, cells: number, holdN: number): number {
  if (!enabled || cells < 1) return 0;
  return cells * (holdN || 1);
}

/**
 * Drop plus-range tapes that do not have `lastN` previous closes at `minPf`.
 * When the range is off, every plus-range tape is dropped. `before`: only closes before it count (the run start — a
 * gate on the whole tape kept or dropped a config for the whole run on closes from inside the run; bug hunt, 13 Oct).
 */
export function gateMinimalPlus<
  T extends { id: string; n: number; gp: ArrayLike<number>; gl: ArrayLike<number>; exitT?: ArrayLike<number> },
>(
  tapes: readonly T[],
  plus: { enabled?: boolean; lastN?: number; minPf?: number } | false | null | undefined,
  before?: number,
): T[] {
  const on = !!plus && plus.enabled === true;
  if (!on) return tapes.filter((t) => !t.id.includes("|mp"));
  const lastN = Math.max(50, Math.round(plus.lastN ?? 50));
  const minPf = Math.max(1.05, plus.minPf ?? 1.35);
  return tapes.filter((t) => {
    if (!t.id.includes("|mp")) return true;
    // the closes before `before` (exit times ascending)
    let n = t.n;
    if (before !== undefined && t.exitT) {
      let lo = 0;
      let hi = t.n;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (t.exitT[mid] < before) lo = mid + 1;
        else hi = mid;
      }
      n = lo;
    }
    if (n < lastN) return false;
    const from = n - lastN;
    const gp = t.gp[n] - t.gp[from];
    const gl = t.gl[n] - t.gl[from];
    const pf = profitFactorRatio(gp, gl, 99);
    const allGl = t.gl[n] - t.gl[0];
    const allGp = t.gp[n] - t.gp[0];
    const allPf = profitFactorRatio(allGp, allGl, 99);
    // the last window and the whole tape both have to clear: a hot tail on a losing book does not seat
    return pf + 1e-12 >= minPf && allPf + 1e-12 >= minPf;
  });
}

function steps(from: number, to: number, step: number): number[] {
  const out: number[] = [];
  for (let n = from; n <= to + 1e-9; n += step) out.push(+n.toFixed(2));
  return out;
}

/** Range gate defaults: 50 previous closes at PF 1.35 (the usual gate is 1.1–1.25). Never below 50 closes. */
// range gate over the last 75 closes: PF 3.75 against 3.62 at 50 (and 3.63 at 100) on the same tapes, 5-6 Oct
// (12 symbols, 6 h + 6 h); operator, 6 Oct: the best last-N windows as defaults
export const RANGE_GATE = { enabled: true, lastN: 75, minPf: 1.35 } as const;

/**
 * The ranges the range gate (and its min-closes pruning) applies to: the small targets that close often and can
 * churn the cost (micro, minimal, short, plus). General and Long replace the former wide targets and are judged like
 * them (the stage gates, the validation last-N) — with the 50-close gate a slow lane would lose its whole plain base.
 */
export const GATED_RANGES: ReadonlySet<string> = new Set(["mc", "mn", "sh", "mp"]);
export const rangeGated = (tag: string | undefined | null) => !!tag && GATED_RANGES.has(tag);

/** Walk-forward form of the range gate (null = off). lastN never below 50, min PF never below the Base minimum 1.05. */
export function rangeGateOf(
  g: { rangeGate?: { enabled?: boolean; lastN?: number; minPf?: number } } | null | undefined,
): { lastN: number; minPf: number } | null {
  const r = g?.rangeGate;
  if (!r || r.enabled === false) return null;
  const lastN = Number(r.lastN);
  const minPf = Number(r.minPf);
  return {
    lastN: Number.isFinite(lastN) ? Math.max(50, Math.round(lastN)) : RANGE_GATE.lastN,
    minPf: Number.isFinite(minPf) ? Math.max(1.05, minPf) : RANGE_GATE.minPf,
  };
}

const RANGE_MIN_PF_KEY: Readonly<Record<RangeTag, RangeMinPfKey>> = {
  mc: "micro",
  mn: "minimal",
  mp: "minimal",
  sh: "short",
  gn: "general",
  lg: "long",
};

/**
 * The stage minimum PF of a config with this range tag: its range's own value (Gates.rangeMinPf, 1.05–3) or the
 * stage minimum. Short is never below 1.1; older saved settings below that floor remain readable. Untagged
 * configs (Wide, the signals) keep their own stage minimum.
 */
export function minPfOf(gates: Pick<Gates, "minPf" | "rangeMinPf">, tag: string | null | undefined, ind?: string): number {
  // A coordination Signal may borrow tagged protection distances. Its economic range is still Signals;
  // the ordinary range's PF floor must neither raise nor lower its independent stage floor.
  if (ind?.startsWith("sig-")) return Math.max(SIGNAL_MIN_PF, gates.minPf);
  const key = tag ? RANGE_MIN_PF_KEY[tag as RangeTag] : undefined;
  const v = key ? gates.rangeMinPf?.[key] : undefined;
  const configured = typeof v === "number" && Number.isFinite(v) ? Math.min(3, Math.max(1.05, v)) : gates.minPf;
  const min = tag === "sh" ? Math.max(1.1, configured) : configured;
  // Tagged research families use the same independent range floor as ordinary indications. The former
  // hard-coded 1.1 silently overrode requested Micro/Minimal floors at every downstream stage.
  return ind && (ind.startsWith("low-range-") || ind.startsWith("micro-") || ind.startsWith("inverse-") || ind.startsWith("tactic-") || ind.startsWith("relate-") || ind.startsWith("break-rel-") || ind.startsWith("break-mc-rel-"))
    ? Math.max(key === "micro" || key === "minimal" ? 1.05 : 1.1, min)
    : min;
}

/**
 * The Micro-indication rule of a grid (microIndFits): Micro on its own indications (grid.micro.ownInds, default on)
 * keeps them on Micro cells; with grid.minimal.microInds (default on) they take Minimal cells as well, so Minimal runs
 * every indication like Micro (operator, 6 Oct). Micro off or ownInds off: no rule, every indication takes every cell.
 */
export function microIndRule(g: { micro?: false | MicroGrid; minimal?: false | MinimalGrid } | undefined): MicroIndRule {
  const m = g?.micro;
  if (!m || m.ownInds === false) return false;
  const mn = g?.minimal;
  return mn && mn.microInds !== false ? "minimal" : true;
}
