// CTS-A Core v2 — domain types. Pure data, no runtime imports.

export type Side = 1 | -1;

/** Column-oriented OHLCV series (one symbol, one timeframe). Index 0 = oldest bar. */
export interface Bars {
  sym: string;
  tfMin: number;
  n: number;
  t: Float64Array; // bar open time, ms
  o: Float64Array;
  h: Float64Array;
  l: Float64Array;
  c: Float64Array;
  v: Float64Array;
  /**
   * The timeframe lanes processed beside this series (CoreSettings.tfs; the engine's lane builder sets it): a
   * combined lane ("…@m<tf>c") agrees with the higher ones of these. Absent: the whole ladder (TF_LADDER).
   */
  lanes?: readonly number[];
}

export interface Candle {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export const INDICATION_KINDS = [
  "trend",
  "break",
  "active",
  "direction",
  "move",
  "rsi",
  "bollinger",
  "sar",
  "macd",
  "ema",
  "osc",
  "volume",
  "channel",
  "ichimoku",
  "smooth",
  "tactic",
  "relate",
  "micro",
  "inverse",
  "low-range",
] as const;
export type IndicationKind = (typeof INDICATION_KINDS)[number];

export interface IndicationDef {
  id: string;
  kind: IndicationKind;
  label: string;
  params: Record<string, number>;
}

export const BOT_TYPES = [
  "sandwich",
  "snap",
  "pulse",
  "ribbon",
  "sweep",
  "clamp",
  "magnet",
  "pivot",
  "follow",
  "revert",
] as const;
/** `exit` is a synthetic, bot-free direction-onset source used only by Historic Exit. */
export type BotType = (typeof BOT_TYPES)[number] | "exit";

/** Protective exit config. All distances are fractions of entry price. */
export interface Protect {
  tp: number;
  sl: number;
  /** trailing distance from peak; 0 = off. Activates once MFE >= trail. */
  trail: number;
  /** Internal Short-grid ownership: this cell is only for Low-Range sources. */
  lowRangeOnly?: boolean;
  /** trailing distance after activation as a share of `trail` (default 1: stop = peak · (1 − trail)) */
  trailStep?: number;
  /** once the trail is active the target is dropped and the trail alone exits (let winners run) */
  trailFree?: boolean;
  /** No take-profit from entry: the hard stop, completed-bar trailing stop and time limit are the only exits. */
  trailOnly?: boolean;
  /** Signal stop/activation/peak-gap ceiling as a fraction of entry; persisted in the exact config identity. */
  trailMax?: number;
  /** Immutable DCA ladder economics carried in its config identity; absent on legacy, equal-unit ladders. */
  dca?: Pick<DcaConfig, "levels" | "step" | "stepOfTp" | "stopGap" | "addRatio" | "maxDistance">;
  /** max bars in trade (time exit at close) */
  hold: number;
  /** Explicit wall-clock ceiling. Capped minute execution is independent of the indication lane's duration. */
  maxHoldMin?: number;
  /**
   * ATR-scaled exits (Stable-02 model): resolved at every entry from ATR(14) of the signal bar into the distances
   * above — stop = sl × ATR, target = tpRatio × stop, trailing (Stable-02 trail %) arming at 0.95 × stop. `tp` /
   * `sl` / `trail` then hold the nominal values at an ATR of 1 % of price (fallback while ATR is warming up; display).
   */
  atr?: AtrProtect;
  /**
   * Range of the cell: "mn" minimal, "sh" short, "gn" general, "lg" long (position-cost multiples), "mc" micro,
   * "mp" minimal plus. Unset = the wide grid.
   * A range cell keeps its price distances on every lane (no lane scaling, no global stop floor: the range has its
   * own minimum stop / trail), and its orders carry their own tracking kind.
   */
  tag?: RangeTag;
  /** Historical exit candidate, selected on a chronological fit window and validated on its later holdout. */
  exitType?: "historic";
  /** Historical exits are independent by direction. 1 = long, -1 = short. */
  exitSide?: 1 | -1;
  /** Independently evaluated Historic Exit config family; encoded in its lane id. */
  exitSet?: ExitConfigSet;
  /** Historic Exit's own trailing-gap floor, captured in the qualified config identity. */
  exitTrailFloor?: number;
}

/** Historic Exit config families share the Exit strategy kind, but keep separate fits and execution identities. */
export type ExitConfigSet =
  "static-sl" | "adaptive-sl" | "trailing" | "breakeven" | "trailing-only";

/** Bounded causal state of one Historic Exit position; anchors keep the qualified config identity stable. */
export interface ExitProtectionState {
  set: ExitConfigSet;
  baseTp: number;
  baseSl: number;
  baseTrail: number;
  baseTrailDist: number;
  /** Entry-time Historic Exit trailing-gap floor; absent on legacy held states. */
  trailFloor?: number;
  /** Modeled round-trip cost captured at entry; absent legacy states retain their original zero-cost lock. */
  cost?: number;
  previousClose: number;
  rangeEma: number;
  moveEma: number;
  samples: number;
  /** last completed lane bar applied; duplicate/rebuilt bars cannot adjust twice */
  lastBarT: number;
}

/**
 * One position-cost range's settings. EVERY range (Micro, Minimal, Short, General, Long) takes every lever here —
 * there is no lever a range silently lacks. The Micro-only ones live on `MicroGrid`, and `checkSettings` refuses
 * them on another range rather than ignoring them.
 */
export interface RangeGrid {
  /** execution volume relative to the balance-based base unit (0.2–5 in 0.2 steps; explicit 2.5 remains supported) */
  volumeFactor?: number;
  /** targets of this range (fractions of price; Micro: net of the round-trip cost unless `tpNetOfCost` is false) */
  tp: readonly number[];
  /** stop as a multiple of the target */
  slOfTp: readonly number[];
  /** trailing distance as a share of the target (0 = no trail) */
  trailOfTp: readonly number[];
  /** trailing variants use at least this stop ÷ target (a higher stop than the trail) */
  trailSlOfTp?: number;
  /** minimum absolute stop distance (fraction of price) */
  minSl?: number;
  /** minimum absolute trailing distance (fraction of price) */
  minTrail?: number;
  /** shortest lane (minutes) this range trades on; 0 = every lane (General / Long default 15) */
  minTf?: number;
  /**
   * MAXIMUM position hold time of this range in MINUTES: its cells close by time after at most this long (earlier at target, stop or trail), in place of the grid's
   * holdH (hours; default 16 / 24 h) — one hold per cell, so the range's cell count is its tp × stop × trail. Unset =
   * the grid's holdH. A lane cannot hold shorter than one of its bars (a 30m lane holds 30 min at least).
   */
  maxHoldMin?: number;
  /** Base judges this range at its own cell (Micro / Minimal default) or at the default protect */
  ownBase?: boolean;
  /** Base on this range's best cell (overrides grid.baseBest) */
  baseBest?: boolean;
  /**
   * The evaluation stop floor for THIS range, in place of `grid.minSlEval`. The global floor is a blanket 0.5 %; a
   * range whose targets are below it can only ever be evaluated with a stop wider than its target, which caps its
   * reward:risk below 1. Unset = the global floor.
   */
  minSlEval?: number;
}

/** The Minimal range: `RangeGrid` plus the one lever only Minimal reads. */
export interface MinimalGrid extends RangeGrid {
  /**
   * Minimal only: the Micro indications ("mc-…") also take Minimal cells, so Minimal runs every indication — its own
   * and Micro's (default on; with Micro's own indications off every indication takes every cell anyway)
   */
  microInds?: boolean;
}

/** The Micro range: `RangeGrid` plus the three levers only Micro reads. */
export interface MicroGrid extends RangeGrid {
  /** Micro only: trade only the Micro indications ("mc-…"), and they only Micro cells (default on) */
  ownInds?: boolean;
  /**
   * Micro only: `tp` is the net profit per winning order after the round-trip cost; the price target is
   * tp + settings.cost (default on)
   */
  tpNetOfCost?: boolean;
  /**
   * Micro only: the smallest NET target the range computes, as a multiple of the round-trip cost (0 / unset = every
   * target). A floor of 1 keeps only net targets at or above the configured round-trip cost itself.
   */
  minNetOfCost?: number;
  /**
   * Micro only: the stop floor NET of the position cost (operator, 6 Oct: "minimal SL 0.2 % deducting position
   * cost — dynamically, automatically calculate"). The floor every Micro cell is evaluated and traded at is
   * minSlNet + settings.cost, so it follows the cost (the measured live cost raises it); it replaces `minSl` and
   * `minSlEval` for Micro. Live, the exchange's own minimum stop distance still applies on top. Unset = minSl.
   */
  minSlNet?: number;
}

/** Protect ranges beside the wide grid (see minimal-coord.ts). */
export type RangeTag = "mp" | "mc" | "mn" | "sh" | "gn" | "lg";

/** ATR exit parameters (see Protect.atr and resolveAtrProtect). */
export interface AtrProtect {
  /** stop distance in ATR(14) multiples (Stable-02 SL_ATR 0.2–2) */
  sl: number;
  /** target = stop × ratio (Stable-02 TP_SL_RATIOS 0.2–3) */
  tpRatio: number;
  /** Stable-02 trailing percent (TRAIL_PCTS 0.4–2.4; 0 / absent = no trail) */
  trail?: number;
  /** live-feedback floors of the resolved stop / trailing distance (fractions; see adjustProtect) */
  minSl?: number;
  minTrail?: number;
}

/** A strategy configuration = bot trigger × indication filter × protect. */
export interface StrategyConfig {
  id: string;
  bot: BotType;
  /** indication id or "none" */
  ind: string;
  protect: Protect;
}

/** "be": the stop at or beyond the entry (moved to breakeven) — a stop exit that is not a loss before the cost */
export type ExitReason = "tp" | "sl" | "trail" | "time" | "disarm" | "be";

/** Sub-strategy that produced a trade. */
export type StratKind =
  "normal" | "trailing" | "trailing-only" | "exit" | "dca" | "dca-active" | "axis";

/** Strategy toggles. Intern calculations always cover every kind; toggles only filter execution. */
export interface StrategyToggles {
  normal: boolean;
  trailing: boolean;
  /** independently qualified historic exit configs */
  exit?: boolean;
  block: boolean;
  /** Block Active: execute only positions at Block level >= minActiveLevel (skip normal / lower levels) */
  blockActive: boolean;
  dca: boolean;
  /** DCA Active: skip the base leg; enter only at the first DCA level (limit), i.e. the higher-level position */
  dcaActive: boolean;
  /** Axis: mean-reversion ladder toward the axis price (EMA centre), rungs at ATR spacing */
  axis: boolean;
}

export type RelateSource =
  | "config"
  | "overall"
  | "symbol"
  | "direction"
  | "indication"
  | "strategy"
  | "coordination"
  | "range";

/** Entry-time logical volume of one relation in original-base units. Shadow portions do not claim an execution. */
export type RelateParts = Partial<Record<RelateSource, { volume: number; level: number; shadow?: true }>>;

/** Settled-order volume adjustment. It is deliberately disabled until a desk opts in. */
export interface RelateAdjustmentConfig {
  enabled: boolean;
  /** Average qualified relation additions, or sum independent additions. */
  mode: "shared" | "additive";
  /** Auto mode searches the supported ratio, depth and Active grids per relation. */
  auto: boolean;
  /** Each qualified step adds this fraction of the relation's last recorded volume (0.2–6, by 0.2). */
  ratio: number;
  /** Maximum positive-result run used for the manual/automatic level (2–15). */
  steps: number;
  /** Skip entries below this positive-result step; 0 keeps the entry at base volume. */
  activeStep: number;
  /** Minimum settled-order unit PF required before a relation can adjust size. */
  minPf: number;
  /** Settled-order sample window, 60–250. */
  lastN: number;
  /** Minimum sample before the PF window is judged; 60–lastN. */
  minTrades: number;
  /** Modeled total multiplier ceiling, reserving capacity for intrinsic ladder fills as well as external factors. */
  maxStack: number;
  /** Maximum original-entry stop distance for a modeled Relate raise, as a fraction of entry. */
  maxSl: number;
  sources: Partial<Record<RelateSource, boolean>>;
}

export interface AxisConfig {
  /** Execution volume of each independent Axis set relative to its corresponding base set (0.1 =10%). */
  volumeFactor?: number;
  /** legs incl. the base leg */
  levels: number;
  /** rung spacing in ATR(14) */
  spacing: number;
  /** size of each extra rung relative to a normal position */
  ratio: number;
  /** enter only between these displacements from the axis, in ATR */
  minDisp: number;
  maxDisp: number;
  /** EMA period of the axis (bars; used when centerMin is not set) */
  center: number;
  /** EMA of the axis in minutes, converted to each lane's bars (the old desk: ≈ 132 one-minute ticks) */
  centerMin?: number;
  /**
   * ladder range type: atr (spacing × ATR), linear (price % × 1.8 + ¼ ATR), geo (price %), fib (0.809 ATR),
   * volume (ATR × (1.15 − min(vol × 8, 0.45)), vol = realized volatility ATR ÷ price ÷ 1.6; see axisSpacing)
   */
  range?: AxisRange;
  /** every Axis set computed: each range type × each ladder depth (levels) — its own independent tape */
  ranges?: AxisRange[];
  levelsSet?: number[];
  /**
   * managed (default): target just past the moving axis (≥ 0.85 step from the average entry), stop at most the
   * target distance, target tightened toward the axis, breakeven at 0.85 risk. fixed: target = the axis at the
   * signal, stop beyond the last rung by the protect's SL (the former behaviour).
   */
  exits?: "managed" | "fixed";
  /**
   * revert (default): the mean-reversion ladder above (entries only back toward the axis, base leg at the next
   * open). desk: the Stable-02 desk structure (vst.ts armUniverse / handleAxis): the combo signal picks the side,
   * `levels` resting limit rungs at axis ∓ k × spacing, each with the desk's SL (slDist) and TP (ratio × SL), the
   * stop tightened every bar and never loosened, the target never reduced (see simulateAxisDesk).
   */
  mode?: AxisMode;
  /** every mode computed in one run, each its own set (overrides `mode`): e.g. ["revert", "desk"] */
  modes?: AxisMode[];
  /** desk: stop distance in ATR multiples (Stable-02 slAtr, default 0.7) */
  slAtr?: number;
  /** desk: target = stop × ratio (Stable-02 tpRatio, snapped to 0.2 … 3 step 0.2, default 2.2) */
  tpRatio?: number;
  /** desk: hybrid = ladder + DCA-style rung fills + the Stable-02 trailing exit */
  hybrid?: boolean;
  /** desk: both the plain and the hybrid set computed (overrides `hybrid`): e.g. [false, true] */
  hybrids?: boolean[];
  /** desk hybrid: Stable-02 trailing percent (0.4 … 2.4, default 0.8) */
  trailPct?: number;
  /** desk: bars an unfilled rung rests before it is cancelled (0 / absent = the protect's hold) */
  expiry?: number;
  /**
   * Axis per range: besides the Wide ladders, each range a pair passed in Base (Micro, Minimal, Short, …) gets
   * its own desk ladders, their targets held inside that range's target band (stop = target ÷ tpRatio, clamped),
   * id tagged with the range — judged at the range's minimum PF and range gate, reported under the range, pooled
   * in its own direction group. One depth (the middle of levelsSet) × every rung-spacing type per range. With it
   * on, a pair builds Wide Axis / DCA only when it passed Base for Wide. Off by default until measured.
   */
  perRange?: boolean;
}

export type AxisMode = "revert" | "desk";

export type AxisRange = "atr" | "linear" | "geo" | "fib" | "volume";

/** Tactics: causal entry filters and pacing applied to every combo's signal (Base, Main, Real, Live alike).
 *  Each one only removes entries; with all off the engine computes the plain signals. */
export interface Tactics {
  /** EU/US session only: signal bar opens 07:00–20:59 UTC */
  session: boolean;
  /** volatility regime: ATR% in the upper half of its last ~2 weeks of bars */
  volRegime: boolean;
  /** trend strength: ADX(14) >= 20 */
  trendStrength: boolean;
  /**
   * choppiness regime: no entry while CHOP(14) >= 61.8 (a range — the market is going sideways, the regime trend and
   * breakout entries fail in). Optional: unset = off, so presets written before it keep their meaning.
   */
  chopRegime?: boolean;
  /**
   * the tactics on the whole state run (a variant, unset = off): a follow / revert entry is the first bar of each
   * state run the tactics keep, not only the run's onset bar — an onset in a regime that cleared a bar later lost the
   * whole run (audit 13 Oct, C16: the default tactics cut the median indication's entries by 74 %)
   */
  onRun?: boolean;
  /** pacing: after a closed trade the same config waits `cooldownBars` before re-entering the symbol */
  cooldown: boolean;
  cooldownBars: number;
}

export type BlockAttribution = Partial<Record<"config" | "overall" | "symbol" | "direction" | "indication" | "type", { level: number; extra: number }>>;

export interface BlockConfig {
  /** extra volume per passing level (additive) */
  ratio: number;
  /** Shared-only ratio override. Unset preserves ratio; a higher value still shares the total maxMult cap. */
  sharedRatio?: number;
  /** Per-relation multiple of ratio (or sharedRatio in Shared mode). Missing relations use 1. */
  sourceRatios?: Partial<Record<"config" | "overall" | "symbol" | "direction" | "indication" | "type", number>>;
  /** Retain each raised relation's qualified level until its realized extra-leg net recovers to zero. */
  holdUntilNet?: boolean;
  /** Closed accepted Real orders, or the explicitly labeled legacy candidate-learning diagnostic. */
  historySource?: "accepted-orders" | "real-candidates";
  /** last-N windows 1..maxLevel are checked independently */
  maxLevel: number;
  minActiveLevel: number;
  /** total external volume cap including the base and every Block source, maximum 8× */
  maxMult: number;
  /** which positions the levels are judged on (config set only by default) */
  sources?: {
    config?: boolean;
    overall?: boolean;
    symbol?: boolean;
    direction?: boolean;
    indication?: boolean;
    /** the strategy type (Normal, Trailing, DCA, DCA Active, Axis) */
    type?: boolean;
  };
  /**
   * shared: the strongest source's level; additive: the sources' levels add up; overall: every source is its own
   * Block (its own level and extra position, tracked per source inside one shared total maxMult cap)
   */
  mode?: "shared" | "additive" | "overall";
  /**
   * pooled sources' judging window (overall, symbol, direction, indication, type): level n judges the source's last
   * n × window closes. A pooled source closes thousands of times a day — at 1 (the default) its last 8 closes span
   * minutes and are positive most of the time, so nearly every entry ran at the top level and Block scaled the book
   * without selecting. The config source always judges its own closes one by one.
   */
  window?: number;
  /**
   * choose the window by results: one book per candidate window, fed the same closes; each close counts for a window
   * when that window's level (from the closes before it) would have raised it. The window whose raised closes had the
   * best PF over its last `windowLookback` raised closes is the one in use (causal; `window` until every candidate has
   * 30). Default off.
   */
  windowAuto?: boolean;
  /** candidate windows for windowAuto (1–500). Default 5, 10, 15, 25, 35. */
  windowCandidates?: number[];
  /** raised closes per candidate window that windowAuto judges on. Default 300. */
  windowLookback?: number;
  /**
   * signals judged on their own: a signal config's Block level comes only from its own symbol × side closes,
   * never from the pooled overall / symbol / direction / indication / type books of the other positions
   */
  signalsOwn?: boolean;
  /**
   * strategy types Block never raises: they trade at their own volume (Axis sizes its own ladder; a Block multiple
   * on top of it stacked 8 × its legs). Block Active does not skip them either.
   */
  excludeKinds?: string[];
  /**
   * target ranges Block never raises ("gn", "lg", …; "wide" = the untagged Normal / Trailing grid, as in
   * live.excludeRanges): their configs trade at their own unit on their own record (the
   * Normal base PF still applies), and Block Active does not skip them. Few, long-held trades: a Block stack of 8×
   * on a losing streak cost General / Long the window (24 h, 3 October: 87 of 99 Long trades raised to level 8)
   */
  excludeRanges?: string[];
  /** after a raised position closes positive, its sources raise nothing for this many closes (0 = no pause) */
  pause?: number;
  /** volume steps: the raise moves in this many equal steps up to maxMult (0 = continuous) */
  steps?: number;
  /**
   * with steps: any raise adds at least one step (true, the default — today's rule) or rounds to the nearest step
   * (false: a raise under half a step adds nothing). With 7 steps to 8× a step is a full unit, so at the default an
   * Overall leg of ratio × level = 0.1 still adds one whole unit (proposal pending a causal eval:
   * docs/positive-coordinations.md, "Block Overall: corrected rules")
   */
  stepFloor?: boolean;
  /**
   * pooled sources' activity-scaled window: each pooled source's window is its own closes in its last windowHours
   * hours ÷ maxLevel (never below `window`), so its top level spans about windowHours of that source whatever its
   * rate (the overall pool closes far more often than one symbol's). 0 / unset = off (`window` for every source).
   */
  windowHours?: number;
  /** a level also needs this share (0–1) of winning closes in its tail; 0 / unset = off (a positive sum is enough) */
  levelMinWin?: number;
  /**
   * signal candidates feed the pooled sources (overall, symbol, direction, indication, type) the engine's entries are
   * judged on. true / unset = yes (today's); false = the pooled sources hold the engine's candidates only (a signal's
   * own config source is unaffected; with signalsOwn the signals never read the pool either)
   */
  poolSignals?: boolean;
  /**
   * identical closes (same symbol, side, entry, exit and result — one entry under two indication names) count once in
   * the pooled sources. Default off (each config's candidate counts)
   */
  poolDedupe?: boolean;
  /**
   * the config source judged per direction: its level counts only the closes of the entry's own direction, and a raised
   * win pauses only that direction's entries of the config (the closes of the other direction never raise or hold it).
   * true = on; false / unset = off (both directions pooled, as before). The symbol, indication and direction sources
   * are per direction already, and the overall and type sources pool by definition (6 Oct: long and short run
   * independently, docs/positive-coordinations.md). Off changes nothing.
   * It acts only while the config source is on (sources.config). DEFAULT_BLOCK sets sources.config false, so under the
   * defaults it changes no engine entry. A settings block.sources replaces DEFAULT_BLOCK.sources whole, and a sources
   * object without `config` reads it as on, so a comparison arm sets config: true in full. A signal's own config level
   * is per direction with this option whatever sources.config says (signalsOwn, on by default, turns the config source
   * on for signal tapes).
   */
  sideKeys?: boolean;
  /** volume added per passing relation. Default 0.4. */
  increase?: number;
  /** allowed min/max for the Block knobs. The active value stays inside. */
  ranges?: {
    levels: readonly [number, number];
    volRatio: readonly [number, number];
    steps: readonly [number, number];
    increase: readonly [number, number];
    pause: readonly [number, number];
  };
}

export interface DcaConfig {
  levels: number;
  /** Each additional leg in ORIGINAL base units, never compounded. Missing = legacy equal-unit legs. */
  addRatio?: number;
  /** distance between levels as a fraction of the reference price */
  step: number;
  /** Maximum adverse distance of the deepest addition from the original entry; 0/unset = legacy uncapped. */
  maxDistance?: number;
  /** distance between levels as a multiple of the target (overrides step when set): step = TP × stepOfTp */
  stepOfTp?: number;
  /** stop beyond the deepest level, in level steps (default 0.5): stop = ref ∓ step × (levels + stopGap) */
  stopGap?: number;
  /** DCA targets (fractions; default 0.8 / 1.2 / 2.6 / 3.5 %) */
  tp?: readonly number[];
  /** stop of a DCA target as a multiple of it (default 1.5 for wide targets, 2 for short ones); the stop is
   *  never inside the deepest level + stopGap */
  slOfTp?: number;
}

export interface Trade {
  /** Immutable entry-time extra-leg ownership for independent Block recovery accounting. */
  blockAttribution?: BlockAttribution;
  cfg: string;
  sym: string;
  side: Side;
  entryT: number;
  exitT: number;
  entry: number;
  exit: number;
  /** net return on notional after round-trip cost (fraction) */
  r: number;
  reason: ExitReason;
  bars: number;
  mfe: number;
  mae: number;
  /** sub-strategy (defaults to normal / trailing by protect) */
  kind?: StratKind;
  /** notional units held (DCA legs); r already includes all legs */
  vol?: number;
  /** Block level at execution (broker) or DCA legs added (sim) */
  level?: number;
  /** External execution multiplier (Block × relation × type factor); r / mult is the underlying strategy return. */
  mult?: number;
  /** Exact Block multiplier before coordination, strategy, range and Relate sizing. */
  blockVol?: number;
  /** part of mult from coordination volume (Stable-02 relation volume). */
  coordVol?: number;
  /** Relate volume multiplier captured at entry, included in the total execution multiple. */
  relateVol?: number;
  /** Optional entry-time stop ceiling, in fractions of the original entry, captured by the simulator. */
  entryStopMax?: number;
  /** Proven entry-time relation portions; absent legacy provenance must not be inferred. */
  relateMeasured?: boolean;
  relateBase?: number;
  relateParts?: RelateParts;
  /** additional type / combined-lane factor in mult; removed from Block reporting and audit. */
  volFactor?: number;
  /** Negative-hour hedge volume factor captured at entry. */
  hedgeVol?: number;
  /** Independent range sizing factor captured at entry. */
  rangeVol?: number;
  /** taken by the negative-hour hedge (a signal outside the ranked set, while the book was losing) */
  hedge?: boolean;
  /** Block type overall: the extra volume each source added as its own position (part of mult) */
  legs?: Partial<Record<string, number>>;
  /** Opt-in audit trace: independent Block levels before and after enabled/paused-source rules. */
  blockLevels?: Partial<Record<string, number>>;
  blockEffectiveLevels?: Partial<Record<string, number>>;
  /** Available closes in each bounded judging tail, including unknown outcomes; not funded-fill counts. */
  blockSampleCounts?: Partial<Record<string, number>>;
  /** still open at the end of the run: marked to market at the last close (r = its mark incl. cost), not closed */
  markedOpen?: boolean;
}

export interface OpenPosition {
  blockAttribution?: BlockAttribution;
  cfg: string;
  sym: string;
  side: Side;
  entryT: number;
  entryI: number;
  /** Actual model price clock captured at entry; absent legacy positions retain their indication clock. */
  executionTfMin?: number;
  /** Captured explicit hold deadline; absent legacy positions do not infer one from current settings. */
  holdEndT?: number;
  /** Observed realtime close instruction, not evidence of a venue fill. */
  hitReason?: ExitReason;
  entry: number;
  stop: number;
  target: number;
  peak: number;
  trailOn: boolean;
  /** the trail in force: arming distance (fraction of the entry) and the distance it trails the peak at */
  trail?: number;
  trailDist?: number;
  trailMax?: number;
  /** The position's own exit mode, retained across compute/restart instead of reading a mutable global grid. */
  trailFree?: boolean;
  trailOnly?: boolean;
  /** Per-position Historic Exit adaptation, serialized with the held paper book. */
  exitProtection?: ExitProtectionState;
  /** mark-to-market return incl. cost at the last close, per unit of volume (from the average entry) */
  mtm: number;
  /**
   * ladder weight (Axis / DCA): the position's volume in units of one base leg — Σ leg weights (base 1 + ratio per
   * filled rung). Absent = 1. Paper / live volume = execution multiple × w (positionVolume); a closed or
   * marked-open order's r already carries it (r = Σ legs), so it is never multiplied into r again.
   */
  w?: number;
  /** execution factors retained at entry for exact closes after settings change or restart */
  blockVol?: number;
  coordVol?: number;
  /** Relate volume multiplier captured at entry; held positions never get resized by later settings. */
  relateVol?: number;
  /** Optional stop ceiling captured at this position's original entry; not inferred for legacy positions. */
  entryStopMax?: number;
  /** Entry-time relation portions used for subsequent independently scoped adjustments. */
  relateMeasured?: boolean;
  relateBase?: number;
  relateParts?: RelateParts;
  volFactor?: number;
  /** Negative-hour hedge volume factor captured at entry. */
  hedgeVol?: number;
  /** Independent range sizing factor captured at entry. */
  rangeVol?: number;
  /** Opt-in Block audit trace captured at entry and retained while this position is held. */
  blockLevels?: Partial<Record<string, number>>;
  blockEffectiveLevels?: Partial<Record<string, number>>;
  blockSampleCounts?: Partial<Record<string, number>>;
}

/** Net-return evidence from the last requested closes of one Base cell, in percentage points. */
export interface BaseLastNStats {
  requested: number;
  n: number;
  gp: number;
  gl: number;
  stop: number;
  invalid?: true;
}

export interface Stats {
  n: number;
  wins: number;
  losses: number;
  wr: number;
  pf: number;
  /** sum of net returns, in percent of one notional */
  net: number;
  avg: number;
  gp: number;
  gl: number;
  /** max drawdown of the cumulative net curve, percent */
  mdd: number;
  /** drawdown time: longest time under a prior equity peak, hours */
  ddt: number;
  /** current (ongoing) drawdown time, hours */
  ddtNow: number;
  expectancy: number;
  sqn: number;
  recovery: number;
  avgHoldMin: number;
  firstT: number;
  lastT: number;
  /** clock hours (by exit) that had at least one close */
  hours: number;
  /** hours with net > 0 */
  greenHours: number;
  /** greenHours / hours */
  gh: number;
  /** closes per active hour */
  tph: number;
  /** worst single-hour net, percent */
  worstHour: number;
  /** Base's default cell against Gates.baseWindows (set only while it is) */
  bw?: BaseWindowsResult;
  /** Exact recent closes when the optional Base Last-N gate is enabled. */
  baseLast?: BaseLastNStats;
}

export interface LastNRow {
  n: number;
  taken: number;
  pf: number;
  net: number;
  ddt: number;
  score: number;
}

export interface LastNResult {
  cfg: string;
  total: number;
  baseline: { is: Stats; oos: Stats };
  rows: LastNRow[]; // in-sample rows per N
  bestN: number; // 0 = ungated wins
  is: Stats;
  oos: Stats;
  oosRows: LastNRow[];
  success: boolean;
  /** gate open for the next trade as of its decision time; N=0 means the ungated baseline was selected */
  gateOpen: boolean;
}

export interface EvalWindow {
  key: string; // "4h" | "24h" | "N20"...
  kind: "time" | "trades";
  span: number; // hours or trades
  n: number;
  pf: number;
  net: number;
  ddt: number;
  wr: number;
  pass: boolean;
}

export interface EvalResult {
  cfg: string;
  at: number;
  windows: EvalWindow[];
  passRatio: number;
  success: boolean;
  score: number;
}

export interface Gates {
  /** Internal rollout flag: qualification uses only each configuration's completed Last-N returns. */
  lastNOnly?: boolean;
  /** stage minimum PF; the runtime keeps it within 1.05–1.50 (a value outside is snapped to the nearest bound) */
  minPf: number;
  /** maximum drawdown time (hours over a 72 h window); kept within 2–35 h by the runtime */
  maxDdtH: number;
  /**
   * floor under the drawdown-time limit, hours. The limit scales with the history a config has (maxDdtH per 72 h),
   * so a config with 24 h of history was held to 11.7 h and one with 6 h to 2.9 h — a few slow hours failed it
   * before it had a sample worth judging. The limit is never below this. 0 / unset = no floor. 0–72.
   */
  minDdtH?: number;
  /**
   * max drawdown ratio (DDR): the largest drawdown of a config's closed-result curve ÷ its net result over the same
   * window. 0.5 = it never gave back more than half of what it made; 0 / unset = off.
   */
  maxDdr?: number;
  /**
   * last-N floor: a config with fewer than N closes but at least this many is judged on all of them (every last-N
   * gate: validation, entry, range gate, Normal base), so a short pre-calculation (20 h) can still seat configs whose
   * targets close rarely (Long). 0 / unset = strict: fewer than N closes fails.
   */
  lastNFloor?: number;
  /** Base cell's most recent closed trades must clear its own PF floor. 0 = off; defaults to 40. */
  baseLastN?: number;
  /** Independent Base close windows for the Micro and Minimal range cells. */
  baseRangeLastN?: Partial<Record<"micro" | "minimal", number>>;
  /**
   * Loss prior (measurement, default off): every evaluation PF (window gates, every last-N gate, the range gate)
   * counts one virtual stop-out at the config's own stop beside its closes — a sample without a loss no longer
   * scores PF 4 (PF_NO_LOSS) and passes every gate, and a wide stop pays for the tail it has not shown yet.
   */
  lossPrior?: boolean;
  /**
   * Sample warm-up (default on): a check that cannot be computed yet because the config has too few closes counts as
   * VALID until it has enough, and is then judged normally (operator, 5 Oct: "if no DDT available because of too few
   * previous positions, calculate as valid until enough exist, then evaluate normally"). It covers the three gates
   * that used to refuse a short sample outright: every last-N gate (validation, entry, range gate, Normal base),
   * the per-symbol gate in "proven" mode (fewer than symMinN own closes on that symbol), and the stability blocks
   * (fewer than two blocks with a sample). Each gate's own judgement is unchanged once the closes are there.
   * `lastNFloor`, when set, still wins for the last-N gates: it judges a partial sample instead of passing it.
   * false = strict (the old behaviour: too few closes fails).
   */
  warmup?: boolean;
  /**
   * minimum hourly success: the share of a config's exit-hours in its window with a positive summed result (fixed
   * selection). Default 0.5 — a config red most hours does not run, even if a few large wins clear PF.
   */
  minGreen?: number;
  /**
   * stage minimum PF per target range, in place of `minPf` for that range's configs (every PF gate a config passes:
   * the Main and Real windows, the pre-historic window, the validation last-N, the entry last-N and the symbol gate).
   * Tagged ranges use their configured floor; Wide and untagged Signals keep `minPf`. Values 1.05–3.
   */
  rangeMinPf?: Partial<Record<RangeMinPfKey, number>>;
  /**
   * Base: fallback PF floor for the untagged / Wide Base cell. Unset = the stage minimum. A tagged cell still uses its
   * rangeMinPf when configured. Lowering this floor widens only Wide set construction; it never weakens tagged ranges.
   * 0.5–1.5.
   */
  baseSetsMinPf?: number;
  /**
   * continuous stability: a set's evaluation window is cut into this many consecutive time blocks and every block with
   * at least 2 closes must clear the set's minimum PF with a positive net (at least 2 such blocks) — a set that made
   * its PF in one stretch and lost after it is not validated. 0 / unset = off. 2–12.
   */
  stableBlocks?: number;
  /**
   * Two-window Base (measurement, default off): a pair's Base cell is also judged on windows of its lane's own Base
   * history [lane's first bar, the Base cut) — consecutive blocks and / or the last hours — not on the whole history
   * alone. `report` rules never gate: each Base cell carries whether it passes them (RangeBaseStat.bw), the Base run the
   * targets that pass today's Base and the rule (ComboRun.ruleTps), and the session dump every passing pair's
   * (basePairs[].rules), so one run reads each rule's orders (scripts/eval-base-rules.mjs). `gate` is enforced: a cell
   * that fails it fails Base (passesBase). Unset: nothing is computed and every result is as before. At most 8 report
   * rules. null = off (as unset).
   */
  baseWindows?: { gate?: BaseWindowRule; report?: BaseWindowRule[] } | null;
  minTrades: number;
  quorum: number;
}

/**
 * One two-window Base rule (Gates.baseWindows). A window with fewer than `minN` closes is not judged (passes), as the
 * sample warm-up does. A rule sets `split`, `recentH` or both (both must pass).
 */
export interface BaseWindowRule {
  /** the lane's Base history in this many equal time blocks, 2–6 */
  split?: number;
  /** the blocks that must pass, 1–split (default: every block) */
  need?: number;
  /** the last this many hours before the Base cut as a window of their own, 1–336 */
  recentH?: number;
  /** with fewer than `minN` closes in the last `recentH`, judge the last 2 × recentH */
  widen?: boolean;
  /** each window's PF floor, 0.5–3 (default the Base floor gates.baseSetsMinPf, else 1); under 1 drops net > 0 */
  minPf?: number;
  /** closes a window needs to be judged, 1–100 (default 3) */
  minN?: number;
  /**
   * the range tags the rule judges (mc mn sh gn lg; "wide" = the untagged default cell); default every range. A
   * range without a Base cell of its own (Minimal plus) is judged at the default cell, so it follows "wide":
   * checkSettings refuses "mp" (it would judge nothing) and an empty list (judges nothing; leave it out instead)
   */
  ranges?: string[];
}

/** A Base cell against Gates.baseWindows: the gate rule's verdict and each report rule's (only the set ones). */
export interface BaseWindowsResult {
  gate?: boolean;
  report?: boolean[];
}

/** The ranges with their own stage minimum PF (Gates.rangeMinPf). */
export type RangeMinPfKey = "micro" | "minimal" | "short" | "general" | "long";

export interface LowRangeGrid {
  enabled: boolean;
  /** Fixed range step as a fraction of price. */
  step: number;
  steps: readonly number[];
  slRatios: readonly number[];
  trailRatios: readonly number[];
  maxHoldMin: number;
}

export interface ProtectGridSpec {
  /** Additional independent no-TP sets derived from every nonzero trailing cell; hard start stop at most 3%. */
  trailingOnly?: boolean;
  /** execution volume relative to the normal factor for untagged Wide configs (0.2–5 in 0.2 steps; default 1) */
  volumeFactor?: number;
  tp: readonly number[];
  /** SL as a multiple of TP (max ratio e.g. 2 or 2.5) */
  slOfTp: readonly number[];
  /** trailing distance as a share of TP (0 = no trail) */
  trailOfTp: readonly number[];
  /** minimum absolute trailing distance (fraction) */
  minTrail: number;
  /** minimum absolute SL distance (fraction) */
  minSl: number;
  /** Wide grid only: upper bound of the stop and of the trailing distance (fractions of price; unset = none) */
  maxSl?: number;
  maxTrail?: number;
  holdH: readonly number[];
  /** trailing distance after activation as a share of the activation move (default 1) */
  trailStep?: number;
  /** drop the target once the trail is active (default false) */
  trailFree?: boolean;
  /**
   * Extra short range (position-cost targets, 8-14x cost, tagged "sh"). Counted on top of the wide grid; the
   * walk-forward keeps whichever cell actually holds PF and green hours.
   */
  short?: false | RangeGrid;
  /** Dedicated cost-aware Short cells for Low-Range indication identities. */
  lowRange?: LowRangeGrid;
  /**
   * Minimal range, under the short range (4-8x cost, tagged "mn"): targets from 1x position cost up to the short
   * range. Counted on top of the wide and short grids. The walk-forward seats only the cells that clear PF and
   * drawdown.
   */
  minimal?: false | MinimalGrid;
  /** General range: 14-22x position cost, step 2x (tagged "gn"). */
  general?: false | RangeGrid;
  /** Long range: 22-32x position cost, step 2x (tagged "lg"). */
  long?: false | RangeGrid;
  /**
   * Micro range: net targets use the frozen range reference basis (default 0.20%-0.80%); price targets add settings.cost
   * (default 0.15%, so 0.35%-0.95%),
   * stops 1x-5x the price target step 0.25 (MICRO_SL, 17 ratios), both trailing distances. Tagged "mc" so the orders are not mixed
   * with the minimal range. Every lever of `RangeGrid` plus the three Micro-only ones.
   */
  micro?: false | MicroGrid;
  /**
   * Range gate: a range cell (micro, minimal, short, plus) takes a seat only when its last `lastN` previous closes
   * clear `minPf` (higher than the usual gate). Causal in the simulation, the same rule in live.
   */
  rangeGate?: { enabled: boolean; lastN: number; minPf: number };
  /**
   * Every range at every config in Base: a pair is judged at each target × stop of every range (Micro: every target ×
   * stops 1 / 1.75 / 2.5 / 3.5 / 5, MICRO_BASE_SL; no trail) and a range passes when its best cell (by net) clears the range's own minimum PF;
   * off: one middle cell per range. A range's own `baseBest` overrides this. Default on.
   */
  baseBest?: boolean;
  /**
   * The tape stage builds only the range targets whose Base cells passed (`ComboRun.rangeTps`). Default on: a range
   * tag used to unlock every target of the range, most of them never Base-evaluated — best-of-28 selection, and the
   * Real net gate then threw ~95 % of the built Micro cells away. Off = every target of a passed range.
   */
  baseTargets?: boolean;
  /**
   * The tape stage builds only the ranges a pair passed in Base (its pass tags). Default on. Off: a pair that passed
   * Base in any range builds every range that applies to its indication for diagnostics. Tagged ranges must pass
   * Base to enter execution; independently allowed untagged DCA/Axis ladders retain their separate policy.
   */
  baseRanges?: boolean;
  /**
   * Base measures every range cell at trail 0. On, it also measures one trailed cell per target (the middle
   * non-zero trail ratio at the middle stop), so a target whose edge needs a trailing stop can pass Base as well.
   * With `baseTargets` on, such a target used to be dropped entirely: its plain cell failed and the tape stage then
   * built none of its trailing configs. Default off until a run shows it earns its Base cost.
   */
  baseTrailCells?: boolean;
  /**
   * Cell level (default off = today): Base records the CELLS that passed (`tag|tp|sl|trail|hold`, ComboRun.rangeCells) and
   * the tape stage builds only those, not every stop × trail × hold of a passed target. Base then measures every hold of a range, and with `baseTrailCells` every trail ratio at every stop; without it the trailed cells are not
   * measured and not built. A range without its own Base cell (ownBase off) and the Wide grid keep the pair / target level.
   */
  baseCells?: boolean;
  /**
   * every indication builds its complete config sets: its best Base pair (passed or not) and every passed pair build
   * every range they apply to, every target × stop × trail × hold (no Base target filter) — the evaluation alone
   * decides the seats. A measurement mode (sessions; memory: every indication's whole grid), off by default.
   */
  allSets?: boolean;
  /**
   * Shortest lane (minutes) the Wide grid's Normal / Trailing cells are built on; 0 / unset = every lane. Short,
   * General and Long default to 15 because their 1.6–6.4 % targets lost on 1m / 5m lanes; Wide's 2.6 %+ targets had
   * no such floor (measured as a variant before it becomes a default).
   */
  wideMinTf?: number;
  /**
   * Minimum stop of EVERY evaluated config, as a fraction of price (default EVAL_MIN_SL = 0.005 = 0.5 %). Every
   * grid cell, Base cell, DCA rung and Axis desk floor is held to it: a tighter stop sits inside the spread and
   * ordinary noise and is taken out before the target can be reached.
   */
  minSlEval?: number;
  /** Legacy range-seat option; economic range identities are always separate in selection. */
  rangeSeats?: boolean;
  /**
   * Range cells fitted to each indication's horizon: a cell is computed when its target is within lo…hi × the
   * indication's typical move (σ₁ₘ · √(period · lane minutes)); at least `keep` targets per range stay.
   */
  rangeFit?: { enabled: boolean; lo?: number; hi?: number; keep?: number };
  /**
   * Additional minimal range (2×–5× cost). Disabled by default. When on, only `cells` are built.
   */
  minimalPlus?:
    | false
    | {
        enabled?: boolean;
        lastN?: number;
        minPf?: number;
        /** execution volume relative to the normal factor for Minimal plus (0.2–5 in 0.2 steps; default 1) */
        volumeFactor?: number;
        tp: readonly number[];
        slOfTp: readonly number[];
        trailOfTp: readonly number[];
        trailSlOfTp?: number;
        minSl?: number;
        minTrail?: number;
        cells?: ReadonlyArray<{ tp: number; sl: number; trail: number }>;
      };
}
