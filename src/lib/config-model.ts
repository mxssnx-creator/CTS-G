import {relationalVolumeSettings,type RelationalVolumeRatios} from "../../shared/cts-volume/ratios.ts";
import { DEFAULT_LAST_N_LANES, lastNLanes, type LastNLane } from "./lane-policy.ts";
import { relateConfigs, type RelateConfig } from "./relate-config.ts";
import { relationLanesOf, type RelationLanesConfig } from "./relation-lanes.ts";
import { blockRoundsOf, type BlockRoundsConfig } from "./block-rounds.ts";
import protectionPolicy from "../../server/pulse/protection-policy.json" with { type: "json" };
export const PULSE_SYMBOLS = [
  "SOL-USDT",
  "XRP-USDT",
  "HYPE-USDT",
  "JUP-USDT",
  "ETC-USDT",
  "TRX-USDT",
  "DOGE-USDT",
  "APT-USDT",
  "ENA-USDT",
  "LDO-USDT",
  "1000PEPE-USDT",
  "KAS-USDT",
] as const;

/** A list of last-N windows (2..200), sorted and unique; the fallback when empty (set_engine.parse_windows). */
export function windows(raw: unknown, fallback: number[]): number[] {
  const src = Array.isArray(raw) ? raw : typeof raw === "string" ? raw.split(/[,;]/) : [];
  const out = [...new Set(src.map((x) => Math.round(Number(x))).filter((x) => Number.isFinite(x)).map((x) => Math.max(1, Math.min(200, x))))];
  return out.length ? out.sort((a, b) => a - b) : [...fallback];
}

export const DEFAULT_SYMBOL_COUNT = 12;
export const MAX_SYMBOLS = 0; // 0 = unlimited

export const SYMBOL_SORTS = [
  { id: "vol1h", label: "Volatility 1H", hint: "Most volatile last 1 hour" },
  { id: "vol24h", label: "Range 24h", hint: "High–low range last 24 hours" },
  { id: "quoteVolume", label: "Quote volume", hint: "24h USDT quote volume" },
  { id: "changeAbs", label: "|24h %|", hint: "Absolute 24h percent move" },
  { id: "changePct", label: "24h %", hint: "Signed 24h change, gainers first" },
  { id: "leverage", label: "Max leverage", hint: "Exchange max long leverage" },
] as const;

export type SymbolSortId = (typeof SYMBOL_SORTS)[number]["id"];

export const SYMBOL_SORT_IDS: SymbolSortId[] = SYMBOL_SORTS.map((s) => s.id);

export type SymbolRankRow = {
  symbol: string;
  last?: number;
  quoteVolume?: number;
  changePct?: number;
  vol1h?: number;
  vol24h?: number;
  maxLeverage?: number;
};

export function coerceSymbolSort(raw: unknown): SymbolSortId {
  const s = String(raw || "vol1h");
  return (SYMBOL_SORT_IDS as string[]).includes(s) ? (s as SymbolSortId) : "vol1h";
}

export function symbolMetric(row: SymbolRankRow, sort: string): number {
  const id = coerceSymbolSort(sort);
  if (id === "vol24h") return Number(row.vol24h || 0);
  if (id === "quoteVolume") return Number(row.quoteVolume || 0);
  if (id === "changeAbs") return Math.abs(Number(row.changePct || 0));
  if (id === "changePct") return Number(row.changePct || 0);
  if (id === "leverage") return Number(row.maxLeverage || 0);
  const v1 = Number(row.vol1h || 0);
  if (v1 > 0) return v1;
  const v24 = Number(row.vol24h || 0);
  if (v24 > 0) return v24 / 4.9;
  return Math.abs(Number(row.changePct || 0));
}

export function symbolRankKey(row: SymbolRankRow, sort: string): [number, number] {
  const lev = -(Number(row.maxLeverage) || 0);
  if (coerceSymbolSort(sort) === "leverage") {
    return [lev, -symbolMetric(row, "vol1h")];
  }
  return [lev, -symbolMetric(row, sort)];
}

export function rankSymbolRows<T extends SymbolRankRow>(rows: T[], sort: string): T[] {
  return [...rows].sort((a, b) => {
    const ka = symbolRankKey(a, sort);
    const kb = symbolRankKey(b, sort);
    if (ka[0] !== kb[0]) return ka[0] - kb[0];
    if (ka[1] !== kb[1]) return ka[1] - kb[1];
    return a.symbol.localeCompare(b.symbol);
  });
}

export function capSymbols(list: string[]): string[] {
  if (!MAX_SYMBOLS || MAX_SYMBOLS <= 0) return list;
  return list.slice(0, MAX_SYMBOLS);
}
// Desk fallback for the PF gate. Mirrors server/pulse/position_cost.py GATE_MIN_PF_DEFAULT (1.25).
export const GATE_MIN_PF_DEFAULT = 1.25;
// Floor for the general/window-size settings; explicit per-step/range rules have their own bounds.
export const MIN_PF_FLOOR = 1.02;
const pfFloor = (v: number) => Math.max(MIN_PF_FLOOR, v);
/** Defaults for the last-N window classes and the separate Signal source. */
export const PF_TYPE_DEFAULTS = {
  minPfMicro: 1.02,
  minPfMinimal: 1.1,
  minPfShort: 1.15,
  minPfMedium: 1.25,
  minPfLong: 1.5,
  minPfSignals: 2.2,
} as const;
export const SL_TP_RATIOS: readonly number[] = protectionPolicy.slTpRatios;
/** Set grid: SL = TP x ratio, 0.25..2.5 step 0.25 (10 ratios, each one a Set). Matches position_cost.SL_RATIOS. */
export const SL_SET_RATIOS = SL_TP_RATIOS;
export function setSlRatiosOf(value: unknown): number[] {
  const rows = Array.isArray(value) ? value.filter((r): r is number => typeof r === "number" && Number.isFinite(r)).map(r => snapSlToTp(r)) : [];
  return rows.length ? [...new Set(rows)].sort((a, b) => a - b) : [...SL_SET_RATIOS];
}
export const TRAIL_VARIANTS = ["0.3:0.1", "0.6:0.2", "0.9:0.3", "1.2:0.4", "1.5:0.5"] as const;

export function snapSlToTp(v: number, lo = SL_TP_RATIOS[0], hi = SL_TP_RATIOS.at(-1)!, step = 0.25): number {
  const x = Math.min(hi, Math.max(lo, Number.isFinite(v) ? v : 0.5));
  return Math.round((lo + Math.round((x - lo) / step) * step) * 100) / 100;
}

export function trailGiveFromArm(arm: number, factor = 1 / 3, gmin = 0.1, gmax = 0.5): number {
  const g = arm * factor;
  return Math.round(Math.min(gmax, Math.max(gmin, g)) * 100) / 100;
}

/** TP grid, owner 2026-11-02: one TP step is an absolute 0.1 % of price (steps 2..32 = 0.2 % .. 3.2 %), not a multiple of PositionCost. Mirrors position_cost.py. */
export const TP_UNIT_PCT = 0.1;
export const TP_STEP_MIN = 2;
export const TP_STEP_MAX = 50;
export const TP_STEP_DEFAULT_MAX = 32;
/** Named TP ranges (server: position_cost.TP_RANGES): each a band of TP steps switched on or off on its own (overlay tpRanges). Wide (3.3-5.0 %) is off by default. */
export const TP_RANGES = [
  { id: "micro", label: "Micro", lo: 2, hi: 3 },
  { id: "minimal", label: "Minimal", lo: 4, hi: 6 },
  { id: "short", label: "Short", lo: 7, hi: 12 },
  { id: "normal", label: "Normal", lo: 13, hi: 20 },
  { id: "long", label: "Long", lo: 21, hi: 32 },
  { id: "wide", label: "Wide", lo: 33, hi: 50 },
] as const;
export type TpRangeId = (typeof TP_RANGES)[number]["id"];
export const MICRO_RANGE_PF_DEFAULT = 1.01;
export function pfRangeFloorsOf(raw: unknown): Partial<Record<TpRangeId, number>> {
  const out: Partial<Record<TpRangeId, number>> = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [key, value] of Object.entries(raw)) {
    const name = key.toLowerCase() as TpRangeId;
    if (!TP_RANGES.some(r => r.id === name) || (typeof value !== "number" && typeof value !== "string")) continue;
    const floor = Number(value);
    if (Number.isFinite(floor) && floor > 0) out[name] = Math.round(Math.max(1, Math.min(2, floor)) * 10000) / 10000;
  }
  return out;
}
export const TP_RANGES_DEFAULT: Record<TpRangeId, boolean> = { micro: true, minimal: true, short: true, normal: true, long: true, wide: false };
/** Trailing grid ceiling: arms up to trailStepMax steps (default 22 = 2.2 %, at most 30 = 3.0 %). */
export const TRAIL_STEP_MAX_DEFAULT = 22;
export const TRAIL_STEP_CEIL = 30;
/** Trailing stop families (server: position_cost.TRAIL_SL_RATIOS): each ratio r builds the arm / give pairs again with the initial stop at r x the arm. */
export const TRAIL_SL_RATIOS = [0.5, 1.0, 1.5, 2.0, 3.0] as const;
export const tpPctOfStep = (step: number) => Math.round(step * TP_UNIT_PCT * 1000) / 1000;
/** Minimum PF per TP step (owner 2026-11-02), mirrors set_engine.py: 1 + TP% x ratio on the 0.1 % grid rounded DOWN, 0.2 % at ratio 0.2 = 1.04.
 * The ratio runs 0.1 .. 1.5 in steps of 0.05 (default 0.2). Auto learns the floor per step from earlier orders and falls back to this value. */
export const STEP_PF_RATIO_MIN = 0.1;
export const STEP_PF_RATIO_MAX = 1.5;
export const STEP_PF_RATIO_STEP = 0.05;
export const STEP_PF_RATIO_DEFAULT = 0.2;
export const STEP_PF_TP_MIN = 0.2;
export const STEP_PF_TP_MAX = 3.5;
export const stepPfRatio = (raw: unknown): number => {
  const v = Number(raw);
  if (raw === null || raw === undefined || raw === "" || !Number.isFinite(v)) return STEP_PF_RATIO_DEFAULT;
  const bounded = Math.max(STEP_PF_RATIO_MIN, Math.min(STEP_PF_RATIO_MAX, v));
  const snapped = Math.floor(bounded / STEP_PF_RATIO_STEP + 0.5 + 1e-9) * STEP_PF_RATIO_STEP;
  return Math.round(Math.max(STEP_PF_RATIO_MIN, Math.min(STEP_PF_RATIO_MAX, snapped)) * 100) / 100;
};
export const stepPfGridPct = (tpPct: number): number =>
  Math.round(Math.max(STEP_PF_TP_MIN, Math.min(STEP_PF_TP_MAX, Math.floor(tpPct * 10 + 1e-9) / 10)) * 10) / 10;
export const manualStepPf = (tpPct: number, ratio: number): number => Math.round((1 + stepPfGridPct(tpPct) * ratio) * 1e6) / 1e6;
export type DirectionMode = "split" | "both";
export type PauseMode = "off" | "auto" | "fixed";
export const PAUSE_MODES: PauseMode[] = ["off", "auto", "fixed"];
export const PAUSE_CANDIDATES_DEFAULT = [2, 3, 5, 8, 12, 20, 30, 45, 60];
export const PAUSE_MAX = 240;
export const SET_TYPES = ["base", "exit", "trail"] as const;
export const INDICATION_TIMEFRAMES = [1, 5, 15, 30] as const;
/** One range configuration of the additional indications; mirrors server/pulse/indication_config.py (the engine expands and validates it). */
export type IndicationConfig = {
  lastNLanes?: LastNLane[];
  symbols?: string[];
  major?: string; minor?: string; coordination?: string;
  label: string;
  enabled: boolean;
  families: string[]; // library families, e.g. "donch", "kelt"
  names: string[]; // exact names (group votes, composites)
  ranges: number[]; // _n<range>
  timeframes: (number | "c" | "x")[]; // _t<tf>; "c" vote over 1/5/15, "x" over 1/5/15/30
  relations: string[]; // "" = none, else name@relation
  stepMin: number;
  stepMax: number;
  slRatios: number[];
  types: string[];
  pause: "auto" | number;
  /** The entry's own (thin) trailing arm range in steps of 0.1 %; 0 = every arm up to stepMax (server: indication_config trailStepMin / trailStepMax). */
  trailStepMin: number;
  trailStepMax: number;
};
export const DEFAULT_INDICATION_CONFIG: IndicationConfig = {
  lastNLanes: structuredClone(DEFAULT_LAST_N_LANES),
  label: "",
  enabled: true,
  families: [],
  names: [],
  ranges: [8, 14],
  timeframes: [1, 5],
  relations: [""],
  stepMin: 3,
  stepMax: 12,
  slRatios: [1, 1.5, 2],
  types: ["base", "exit", "trail"],
  pause: "auto",
  trailStepMin: 0,
  trailStepMax: 0,
};

export type PulseOverlay = {
  targetNotional: number;
  volumeFactor: number;
  /** Percent of the balance per position before the volume factor; 0 = the fixed targetNotional. */
  sizePctOfBalance: number;
  /** Skip orders when the exchange minimum exceeds the balance-relative entry budget. */
  strictSizeCap: boolean;
  /** Hard ceiling for one position's notional in USDT, whatever the balance. */
  sizeMaxNotional: number;
  /** Model-only initial stop plus cost limit; 0 = off. */
  maxModeledStopLossPct: number;
  /** A fill farther than this (percent) from the price the entry was decided at is closed at once and the symbol rests. */
  maxEntrySlipPct: number;
  /** Drawdown from the start equity (percent) that halts entries; 0 = off. */
  ddHaltPct: number;
  ddHaltBasis: "session" | "start";
  leverage: number;
  useMaxLeverage: boolean;
  /** Lower the leverage to 1 / (2 x stop + 0.5 %) so the venue never liquidates before a Set's stop; off (default, owner 2026-11-03): always the maximum. */
  leverageStopCap: boolean;
  /** Block v2 (server: block_rules): one entry per block count, each with its own step (PositionCosts after the count before), volume ratio (0.1 .. 1.0 of the parent's original volume) and switch. */
  blockCounts: BlockCount[];
  blockStackType: "additive" | "shared";
  blockSharedTotal: number;
  blockMinObservations: number;
  blockRelations: BlockRelation[];
  blockCarry: boolean;
  blockPauseSteps: number;
  blockSkipFirst: number;
  /** Deficit guard: no add while the book's executed adds are net negative; adding resumes when the adds followed on paper have earned the deficit back. */
  blockGuard: boolean;
  /** A count of a fixed-target parent adds only up to this share of the way to the target (0 = no rule). */
  blockTargetShare: number;
  /** Parent Set types that may receive adds. */
  blockKinds: string[];
  maxOpen: number;
  maxPerGroup: number;
  symbolsAll: boolean;
  symbolsDynamic: boolean;
  symbolSort: string;
  symbolCap: number;
  slPct: number;
  tpPct: number;
  trailArmPct: number;
  trailGivePct: number;
  timeStopS: number;
  scratchS: number;
  scratchMinPct: number;
  scanS: number;
  cooldownS: number;
  staggerS: number;
  controlOrders: boolean;
  blockEnabled: boolean;
  blockMaxStack: number;
  blockMinSamples: number;
  blockWindow: number;
  mainEvalPosCount: number;
  realEvalPosCount: number;
  prevPosMinCount: number;
  prevPosWindow: number;
  exitMinSamples: number;
  relateConfigs: RelateConfig[];
  /** Relation lanes: last-N lanes of the post-Base orders by relation (overall / type / range / step); passing lanes raise an entry's volume by their ratio. */
  relationLanes: RelationLanesConfig;
  /** Block rounds: blocks of N positions per group; a losing block raises the next entries until the weighted net is back at zero. */
  blockRounds: BlockRoundsConfig;
  blockVolumeRatio: number;
  blockRiskPctOfBalance: number;
  /** Minutes a symbol and direction rest after a position of it was closed outside the engine (manual close); 0 = off. */
  manualCloseRestMin: number;
  /** A deposit or withdrawal moves the drawdown basis (session and start equity) instead of reading as a gain or loss. */
  transferAdjust: boolean;
  blockProfitFactorRatio: number;
  blockPauseCountRatio: number;
  blockActiveLive: boolean;
  blockActiveReal: boolean;
  dcaEnabled: boolean;
  dcaMaxSteps: number;
  dcaCooldownSeconds: number;
  dcaBreakevenProfitPct: number;
  dcaTakeProfitMode: string;
  dcaStepDistancesPct: number[];
  dcaStepVolumeMultipliers: number[];
  dcaAutoDeact: boolean;
  dcaAuto: boolean;
  dcaOnSets: boolean;
  dcaOnAxis: boolean;
  dcaMaxExposureX: number;
  dcaMinPf: number;
  dcaPfWindow: number;
  dcaDeactN: number;
  symbols: string[];
  axisPrevEnabled: boolean;
  axisPrevMaxWindow: number;
  axisLastEnabled: boolean;
  axisLastMaxWindow: number;
  axisContEnabled: boolean;
  axisContMaxWindow: number;
  axisPauseEnabled: boolean;
  axisPauseMaxWindow: number;
  minPf: number;
  positionCostPct: number;
  slippageAllowancePct: number;
  makerFeeRate: number;
  takerFeeRate: number;
  slippageFeeRatio: number;
  windowsLinked: boolean;
  pfWindow: number;
  slMinPct: number;
  slMaxPct: number;
  tpMinPct: number;
  tpMaxPct: number;
  tpCostRatio: number;
  slToTpRatio: number;
  slToTpAuto: boolean;
  slToTpRecalcN: number;
  slToTpRecalcEvery: number;
  trailAuto: boolean;
  trailRecalcN: number;
  trailRecalcEvery: number;
  trailArmMin: number;
  trailArmMax: number;
  trailGiveMin: number;
  trailGiveMax: number;
  trailGiveFactor: number;
  trailRecalcGive: boolean;
  tf1m: boolean;
  tf5m: boolean;
  tf15m: boolean;
  tfCombined: boolean;
  tfMinAgree: number;
  stratIndications: boolean;
  stratBlock: boolean;
  stratTrailing: boolean;
  stratNormal: boolean;
  relationalVolumeRatios: RelationalVolumeRatios;
  strategyProcessingByRange: Record<string, { normal?: boolean; trailing?: boolean }>;
  stratGeneral: boolean;
  /** Short Range strategy: the Low-Range signals, TP steps shortRangeStepMin..Max (default 2-6). */
  stratShortRange: boolean;
  stratSignals: boolean;
  signalConfigs: SignalConfig[];
  stratTrend: boolean;
  stratBreakout: boolean;
  stratReversion: boolean;
  stratFlow: boolean;
  stratMulti: boolean;
  shortRangeStepMin: number;
  shortRangeStepMax: number;
  /** Short Range has its own Base PF floor, whatever the global setMinPf is (default 1.1). */
  shortRangeMinPf: number;
  /** Minimum initial SL = modeled round-trip cost + this net price percent. */
  minSlDistancePct: number;
  microMinSlDistancePct: number;
  microMinTrailDistancePct: number;
  /** Minimum trailing distance = PositionCost + trailMinSteps steps. */
  trailMinSteps: number;
  /** Smallest arm - give (percent) a trailing pair may have; pairs under it build no Set. 0 = every pair. */
  trailMinLockPct: number;
  /** Profit group gate: a group (pack x type x TP range x lane) opens positions only while its newest `groupGateN` executed results of the last `groupGateHours` hours have PF >= `groupGatePf`; judged on the results it has (`groupGateMin` 1). */
  groupGate: boolean;
  groupGateN: number;
  groupGatePf: number;
  groupGateHours: number;
  groupGateMin: number;
  stratDca: boolean;
  indTypeState: boolean;
  indTypeDirection: boolean;
  indTypeMove: boolean;
  indTypeActive: boolean;
  indTypeCommon: boolean;
  indTypeSignals: boolean;
  noise: number;
  volWeight: number;
  minStep: number;
  maxStopLossRatio: number;
  trailingMinStep: number;
  posCountsVolumeRatio: number;
  rearrange: boolean;
  rearrangeGap: number;
  indEnabled: boolean;
  indMinSources: number;
  indMinAgreement: number;
  indMinConfidence: number;
  indMinStrength: number;
  indStopMinPct: number;
  indStopMaxPct: number;
  indAtrMult: number;
  indRewardRisk: number;
  indExtraSources: boolean;
  histEnabled: boolean;
  histLookbackBars: number;
  histMinBars: number;
  histWarmup: number;
  histRefreshS: number;
  setPfWindow: number;
  setDeactN: number;
  setMinPf: number;
  setMaxDdtHours: number;
  setGateWindow: number;
  setGateMinTrades: number;
  /** Stages Base -> Main -> Real (off: the Base long window alone). */
  stagesEnabled: boolean;
  /** true: a stage window judges only once it holds its full N orders; false (default): the orders it has, at least one. */
  lastNDisjoint: boolean;
  setGateShortMode: "positive"|"negative";
  mainShortMode: "positive"|"negative";
  stageFullWindow: boolean;
  /** Basis of every last-N window: "time" = the newest N orders inside the lookback, "count" = the newest N orders of the configuration whatever their age. */
  lastNBasis: "time" | "count";
  /** Base: the second last N; both Base windows must pass (0 = off). */
  setGateShortN: number;
  /** Minimum PF by window size: every last-N window asks the PF of its class (Micro / Minimal / Short / Medium up to their bounds, Long above). */
  minPfBySize: boolean;
  minPfMicro: number;
  minPfMinimal: number;
  minPfShort: number;
  minPfMedium: number;
  minPfLong: number;
  /** Separate Signal-source floor; source integration is not yet active in CTS-G. */
  minPfSignals: number;
  minPfMicroN: number;
  minPfMinimalN: number;
  minPfShortN: number;
  minPfMediumN: number;
  /** The short window is at most this share of the long window (default 0.5: long 40, short at most 20). */
  setGateShortMaxRatio: number;
  /** Base minimal window: the newest N (1-3) results of a Set must all be positive / negative (0 = off). */
  setGateMinN: number;
  setGateMinMode: MinMode;
  mainGateEnabled: boolean;
  mainGateN: number;
  /** Main short window (earlier "Auto"): each Set picks its own window from these on its Main Set; the long window and the short one must both pass. */
  mainShortEnabled: boolean;
  mainShortWindows: number[];
  /** The short windows are at most this share of the Main long window (default 0.5: long 25, short at most 12). */
  mainShortMaxRatio: number;
  mainShortMin: number;
  mainShortMargin: number;
  /** Main minimal window: the newest N (1-3) results of a Set's Main Set must all be positive / negative (0 = off). */
  mainMinN: number;
  mainMinMode: MinMode;
  /** Main Negative lane: the average of the last N Main orders below zero (for some N) opens the Real Negative lane. */
  mainNegEnabled: boolean;
  mainNegWindows: number[];
  /** Real: the last N of the lane's Real Set. */
  realGateEnabled: boolean;
  realGateN: number;
  /** The overall strategies (coordinator stages, axes) read the Real Sets' paper stream. */
  realStagePaper: boolean;
  minPfLinked: boolean;
  histLookbackAuto: boolean;
  histLookbackMax: number;
  histLookbackTargetN: number;
  setUseHistoricGate: boolean;
  holdEnabled: boolean;
  holdAuto: boolean;
  holdAutoMax: number;
  holdAutoMin: number;
  // Pause after success: rest after a WINNING close of a Set lane, per indication (pack) and symbol. off / auto (chosen from reference Sets, only from
  // earlier results) / fixed. pausePack fixes minutes for one pack and wins over the mode.
  // Minimum PF by TP step: one floor per Set for every window and stage. Auto learns it per step from orders that closed before the evaluation window.
  // split (default): every Set exists once per trade direction, each with its own lane, tape, windows, stages, pause and floor; both: one Set trades both.
  directionMode: DirectionMode;
  minPfByStep: boolean;
  /** Explicit TP-range floors override manual and learned per-step floors in every stage. */
  minPfByRange: Partial<Record<TpRangeId, number>>;
  rankByLane: boolean;
  setScratchEnabled: boolean;
  // Set grid extensions (2026-11-03): named TP ranges on / off, SL ratios 0.2 / 0.3 / 0.4 x TP, trailing arms up to 3.0 %, trailing stop families.
  tpRanges: Record<TpRangeId, boolean>;
  setSlRatios: number[];
  trailStepMax: number;
  trailSlRatios: number[];
  // own volume multiples (0.1 .. 5, missing = 1) per indication pack, Set type (base / exit / trail) and TP range (micro .. wide, trail)
  volumeByPack: Record<string, number>;
  volumeByKind: Record<string, number>;
  volumeByRange: Record<string, number>;
  /** Volume multiple per pack and type, "<pack>:<type>" (server: pulse_trader.VOLUME_BY_PACK_KIND_DEFAULT, empty since 2026-11-04). */
  volumeByPackKind: Record<string, number>;
  /** Largest SL ratio per TP range (server: set_engine.SL_MAX_BY_RANGE_DEFAULT: Long and Wide 1.0). */
  slMaxByRange: Record<string, number>;
  /** Last-N windows per TP range (server: set_engine.last_n_by_range_of): a range that is absent, or a key that is absent, uses the global window. */
  lastNByRange: Record<string, LastNOverride>;
  /** Exit Sets (no-TP Sets) per indication pack: missing = on. */
  exitSetsByPack: Record<string, boolean>;
  /** Named trailing ranges by the arm's step (Micro .. Wide): missing = on. */
  trailRanges: Record<TpRangeId, boolean>;
  trailPick: TrailPick;
  /** Per pack or library group ("group:trend", "group:break", ...): the kind order for that pack's signals (server: SetBook.kind_order_for). */
  trailPickByPack: Record<string, TrailPick>;
  minPfStepAuto: boolean;
  minPfStepRatio: number;
  minPfStepAutoN: number;
  minPfStepAutoMin: number;
  minPfStepAutoScope: "step" | "set";
  minPfSetAutoMin: number;
  minPfStepAutoTol: number;
  /** The lowest floor Auto may choose (1.00 .. 2.00). */
  minPfStepAutoLow: number;
  /** Highest floor a TP step may ask (server: set_engine.STEP_PF_AUTO_HIGH_DEFAULT); 2 = no cap. */
  minPfStepAutoHigh: number;
  pauseMode: PauseMode;
  pauseFixedMin: number;
  pauseCandidates: number[];
  pauseAutoMax: number;
  pauseAutoMin: number;
  pausePack: Record<string, number>;
  // Indications by range: families x ranges x timeframes x relations, each entry with its own TP-step range, SL ratios, types and pause.
  indicationConfigs: IndicationConfig[];
  stepHoldOn: boolean[];
  stepHoldMin: number[];
  setMinSamples: number;
  setMinStep: number;
  setStepMax: number;
  setStepAdapt: boolean;
  trailMinStep: number;
  exitEnabled: boolean;
  exitIgnoreTp: boolean;
  exitBestOf: boolean;
  exitLockOn: boolean;
  exitPeakOn: boolean;
  exitRevOn: boolean;
  exitTimeOn: boolean;
  exitLockPct: number;
  exitBeBuffer: number;
  exitOptSlPct: number;
  exitOptSlMin: number;
  exitOptSlMax: number;
  exitMinHoldS: number;
  setSlMinRatio: number;
  execGateN: number;
  slRatiosByRange: Record<string, number[]>;
  exitSetHoldBars: number;
  exitPfWindow: number;
  exitDeactN: number;
  exitMinPf: number;
  exitAutoDeact: boolean;
  modules?: Record<string, boolean>;
};

export const DEFAULT_OVERLAY: PulseOverlay = {
  targetNotional: 2.15,
  volumeFactor: 0.05,
  sizePctOfBalance: 1,
  strictSizeCap: false,
  sizeMaxNotional: 25,
  maxModeledStopLossPct: 0,
  maxEntrySlipPct: 0.75,
  ddHaltPct: 80,
  ddHaltBasis: "session",
  leverage: 150,
  useMaxLeverage: true,
  leverageStopCap: false,
  // measured 2026-11-04 (stack_eval block2, 4,400 + 12,960 settings per window, sealed third read once): per-symbol evidence, steps of 3 PositionCosts, the first count only
  // collects evidence, counts 2 and 3 add 0.5 x the original volume, no minimum observations, no carry. Not proven: the sealed read was mixed (see CLAUDE.md).
  blockCounts: [{ step: 3, ratio: 1, on: true }, { step: 3, ratio: 1, on: true }, { step: 3, ratio: 1, on: true }],
  blockStackType: "additive",
  blockSharedTotal: 1,
  blockMinObservations: 1,
  blockRelations: ["symbol", "step"],
  blockCarry: false,
  blockPauseSteps: 1,
  blockSkipFirst: 1,
  blockGuard: true,
  blockTargetShare: 0.5,
  blockKinds: ["base", "trail"],
  maxOpen: 0,
  maxPerGroup: 0,
  symbolsAll: false,
  symbolsDynamic: true,
  symbolSort: "vol1h",
  symbolCap: 50,
  slPct: 0.48,
  tpPct: 0.75,
  trailArmPct: 0.3,
  trailGivePct: 0.1,
  timeStopS: 21600,
  scratchS: 600,
  scratchMinPct: 0.16,
  scanS: 0.2,
  cooldownS: 3,
  staggerS: 0.12,
  controlOrders: true,
  blockEnabled: true,
  blockMaxStack: 6,
  blockMinSamples: 8,
  blockWindow: 25,
  mainEvalPosCount: 20,
  realEvalPosCount: 15,
  prevPosMinCount: 8,
  prevPosWindow: 25,
  exitMinSamples: 8,
  relateConfigs: relateConfigs(undefined),
  relationLanes: relationLanesOf(undefined),
  blockRounds: blockRoundsOf(undefined),
  blockVolumeRatio: 1,
  blockRiskPctOfBalance: 0,
  manualCloseRestMin: 240,
  transferAdjust: true,
  blockProfitFactorRatio: 0.8,
  blockPauseCountRatio: 1,
  blockActiveLive: true,
  blockActiveReal: true,
  dcaEnabled: false,
  dcaMaxSteps: 0,
  dcaCooldownSeconds: 30,
  dcaBreakevenProfitPct: 0.2,
  dcaTakeProfitMode: "average",
  dcaStepDistancesPct: [0.5, 1, 1.5, 2],
  dcaStepVolumeMultipliers: [1.5, 2, 2.3, 2.5],
  dcaAutoDeact: true,
  dcaAuto: true,
  dcaOnSets: false,
  dcaOnAxis: false,
  dcaMaxExposureX: 0,
  dcaMinPf: 1.25,
  dcaPfWindow: 15,
  dcaDeactN: 25,
  symbols: [
    "SOL-USDT",
  "XRP-USDT",
  "HYPE-USDT",
  "JUP-USDT",
  "ETC-USDT",
  "TRX-USDT",
  "DOGE-USDT",
  "APT-USDT",
  "ENA-USDT",
  "LDO-USDT",
  "1000PEPE-USDT",
  "KAS-USDT",
  ],
  axisPrevEnabled: true,
  axisPrevMaxWindow: 12,
  axisLastEnabled: true,
  axisLastMaxWindow: 4,
  axisContEnabled: true,
  axisContMaxWindow: 8,
  axisPauseEnabled: true,
  axisPauseMaxWindow: 8,
  minPf: GATE_MIN_PF_DEFAULT,
  positionCostPct: 0.11,
  slippageAllowancePct: 0.01,
  makerFeeRate: 0.0002,
  takerFeeRate: 0.0005,
  slippageFeeRatio: 0.1,
  windowsLinked: true,
  pfWindow: 8,
  slMinPct: 0.2,
  slMaxPct: 15.75,
  tpMinPct: 0.35,
  tpMaxPct: 4.5,
  tpCostRatio: 5,
  slToTpRatio: 0.5,
  slToTpAuto: true,
  slToTpRecalcN: 8,
  slToTpRecalcEvery: 8,
  trailAuto: true,
  trailRecalcN: 8,
  trailRecalcEvery: 8,
  trailArmMin: 0.3,
  trailArmMax: 1.5,
  trailGiveMin: 0.1,
  trailGiveMax: 0.5,
  trailGiveFactor: 0.3333333333333333,
  trailRecalcGive: true,
  tf1m: true,
  tf5m: true,
  tf15m: true,
  tfCombined: true,
  tfMinAgree: 2,
  stratIndications: true,
  stratBlock: true,
  stratTrailing: true,
  stratNormal: true,
  relationalVolumeRatios: relationalVolumeSettings(undefined),
  strategyProcessingByRange: {},
  stratGeneral: true,
  stratShortRange: true,
  stratSignals: false,
  signalConfigs: [],
  stratTrend: true,
  stratBreakout: false,
  stratReversion: true,
  stratFlow: true,
  stratMulti: true,
  shortRangeStepMin: 2,
  shortRangeStepMax: 6,
  shortRangeMinPf: 1.1,
  minSlDistancePct: protectionPolicy.minSlDistancePct.default,
  microMinSlDistancePct: .1, microMinTrailDistancePct: .05,
  trailMinSteps: 2,
  trailMinLockPct: 0,
  groupGate: true,
  groupGateN: 3,
  groupGatePf: 1,
  groupGateHours: 6,
  groupGateMin: 1,
  stratDca: false,
  indTypeState: true,
  indTypeDirection: true,
  indTypeMove: true,
  indTypeActive: true,
  indTypeCommon: true,
  indTypeSignals: true,
  noise: 0.05,
  volWeight: 0.3,
  minStep: 10,
  maxStopLossRatio: 1.4,
  trailingMinStep: 6,
  posCountsVolumeRatio: 0.05,
  rearrange: true,
  rearrangeGap: 0.22,
  indEnabled: true,
  indMinSources: 3,
  indMinAgreement: 0.6,
  indMinConfidence: 0.6,
  indMinStrength: 0.2,
  indStopMinPct: 0.2,
  indStopMaxPct: 1.5,
  indAtrMult: 0.85,
  indRewardRisk: 1.8,
  indExtraSources: true,
  histEnabled: true,
  histLookbackBars: 1920,
  histMinBars: 120,
  histWarmup: 30,
  histRefreshS: 120,
  setPfWindow: 8,
  setDeactN: 25,
  setMinPf: GATE_MIN_PF_DEFAULT,
  setMaxDdtHours: 24,
  setGateWindow: 50,
  setGateMinTrades: 0,
  stagesEnabled: true,
  lastNDisjoint: false, setGateShortMode: "positive", mainShortMode: "positive",
  stageFullWindow: false,
  lastNBasis: "time",
  setGateShortN: 25,
  minPfBySize: true,
  ...PF_TYPE_DEFAULTS,
  minPfMicroN: 5,
  minPfMinimalN: 10,
  minPfShortN: 20,
  minPfMediumN: 35,
  setGateShortMaxRatio: 0.5,
  setGateMinN: 0,
  setGateMinMode: "positive",
  mainGateEnabled: true,
  mainGateN: 25,
  mainShortEnabled: true,
  mainShortWindows: [5, 10, 15],
  mainShortMaxRatio: 0.5,
  mainShortMin: 1,
  mainShortMargin: 1.02,
  mainMinN: 0,
  mainMinMode: "positive",
  mainNegEnabled: true,
  mainNegWindows: [5, 10],
  realGateEnabled: true,
  realGateN: 20,
  realStagePaper: true,
  minPfLinked: true,
  histLookbackAuto: false,
  histLookbackMax: 4320,
  histLookbackTargetN: 12,
  setUseHistoricGate: true,
  holdEnabled: true,
  holdAuto: false,
  holdAutoMax: 50,
  holdAutoMin: 10,
  directionMode: "split",
  minPfByStep: true,
  minPfByRange: { micro: MICRO_RANGE_PF_DEFAULT },
  rankByLane: true,
  setScratchEnabled: false,
  tpRanges: { ...TP_RANGES_DEFAULT, wide: true }, // shipped overlays: every range on, Wide included (owner 2026-11-03)
  setSlRatios: [...SL_SET_RATIOS],
  trailStepMax: TRAIL_STEP_MAX_DEFAULT,
  trailSlRatios: [],
  volumeByPack: {},
  volumeByKind: {},
  volumeByRange: {},
  volumeByPackKind: {},
  slMaxByRange: { long: 1, wide: 1 },
  lastNByRange: {},
  exitSetsByPack: { indications: false }, // shipped: the indications pack's Exit Sets off (measured 2026-11-03)
  trailRanges: { micro: true, minimal: true, short: true, normal: true, long: true, wide: true },
  trailPick: "after",
  trailPickByPack: {},
  minPfStepAuto: true,
  minPfStepRatio: STEP_PF_RATIO_DEFAULT,
  minPfStepAutoN: 10,
  minPfStepAutoMin: 1,
  minPfStepAutoScope: "set",
  minPfSetAutoMin: 10,
  minPfStepAutoTol: 5,
  minPfStepAutoLow: 1.02,
  minPfStepAutoHigh: 2,
  pauseMode: "auto",
  pauseFixedMin: 10,
  pauseCandidates: [...PAUSE_CANDIDATES_DEFAULT],
  pauseAutoMax: 100,
  pauseAutoMin: 30,
  pausePack: {},
  indicationConfigs: [],
  stepHoldOn: [false, false, false, false, false, false, false, false, false, false, false, false],
  stepHoldMin: [15, 15, 15, 15, 15, 120, 120, 120, 120, 120, 120, 120],
  setMinSamples: 12,
  setMinStep: 2,
  setStepMax: TP_STEP_DEFAULT_MAX,
  setStepAdapt: false,
  trailMinStep: 3,
  exitEnabled: true,
  exitIgnoreTp: true,
  exitBestOf: true,
  exitLockOn: true,
  exitPeakOn: true,
  exitRevOn: false,
  exitTimeOn: false,
  exitLockPct: 0.15,
  exitBeBuffer: 0.04,
  exitOptSlPct: 0.3,
  exitOptSlMin: 0.1,
  exitOptSlMax: 0.9,
  exitMinHoldS: 45,
  setSlMinRatio: 0.5,
  execGateN: 0,
  slRatiosByRange: { long: [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8], wide: [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8] },
  exitSetHoldBars: 720,
  exitPfWindow: 8,
  exitDeactN: 25,
  exitMinPf: 1.25,
  exitAutoDeact: true,
  modules: {
    "exchange.bingx": true,
    "strategy.exits": true,
    "strategy.sets": true,
    "core.historic": true,
    "strategy.block": true,
    "strategy.dca": false,
    "strategy.coord": true,
    "strategy.indications": true,
    "strategy.rearrange": true,
    "strategy.trailing": true,
    "feed.tf1m": true,
    "feed.tf5m": true,
    "feed.tf15m": true,
    "feed.tfCombined": true,
    "risk.slTpRatios": true,
    "exec.controls": true,
  },
};

/** Restore all desk defaults as an editable, unsaved copy. */
export function defaultOverlay(): PulseOverlay {
  return structuredClone(DEFAULT_OVERLAY);
}

export type CtsSettings = {
  coordination_settings?: Record<string, unknown>;
  coordinationSettings?: Record<string, unknown>;
  strategyBaseTrailingVariants?: string[];
  control_orders?: boolean | number | string;
  variantBlockEnabled?: boolean;
  variant_block?: boolean;
  blockMaxStack?: number;
  blockMinSamples?: number;
  blockCounts?: Partial<BlockCount>[];
  blockStackType?: string;
  blockSharedTotal?: number;
  blockMinObservations?: number;
  blockRelations?: string[];
  blockCarry?: boolean;
  blockPauseSteps?: number;
  blockSkipFirst?: number;
  blockGuard?: boolean;
  blockTargetShare?: number;
  blockKinds?: string[];
  blockWindow?: number;
  exitMinSamples?: number;
  relateConfigs?: RelateConfig[];
  relationLanes?: RelationLanesConfig;
  blockRounds?: BlockRoundsConfig;
  blockVolumeRatio?: number;
  blockProfitFactorRatio?: number;
  blockPauseCountRatio?: number;
  blockActiveLiveEnabled?: boolean;
  blockActiveRealEnabled?: boolean;
  dcaEnabled?: boolean;
  strictSizeCap?: boolean;
  maxModeledStopLossPct?: number;
  variantDcaEnabled?: boolean;
  variant_dca?: boolean;
  dcaMaxSteps?: number;
  dcaCooldownSeconds?: number;
  dcaBreakevenProfitPct?: number;
  dcaTakeProfitMode?: string;
  dcaStepDistancesPct?: number[];
  dcaStepVolumeMultipliers?: number[];
  dcaAutoDeact?: boolean;
  dcaAuto?: boolean;
  dcaOnSets?: boolean;
  dcaOnAxis?: boolean;
  dcaMaxExposureX?: number;
  dcaMinPf?: number;
  dcaPfWindow?: number;
  dcaDeactN?: number;
  volumeFactor?: number;
  axisPrevEnabled?: boolean;
  axisPrevMaxWindow?: number;
  axisLastEnabled?: boolean;
  axisLastMaxWindow?: number;
  axisContEnabled?: boolean;
  axisContMaxWindow?: number;
  axisPauseEnabled?: boolean;
  axisPauseMaxWindow?: number;
  strategies?: Record<string, unknown>;
  realProfitFactor?: number;
  exchangePositionCost?: number;
  positionCost?: number;
  windowsLinked?: boolean;
  pfWindow?: number;
  slMinPct?: number;
  slMaxPct?: number;
  tpMinPct?: number;
  tpMaxPct?: number;
  tpCostRatio?: number;
  slToTpRatio?: number;
  slToTpAuto?: boolean;
  slToTpRecalcN?: number;
  slToTpRecalcEvery?: number;
  trailAuto?: boolean;
  trailRecalcN?: number;
  trailRecalcEvery?: number;
  trailArmMin?: number;
  trailArmMax?: number;
  trailGiveMin?: number;
  trailGiveMax?: number;
  trailGiveFactor?: number;
  trailRecalcGive?: boolean;
  tf1m?: boolean;
  tf5m?: boolean;
  tf15m?: boolean;
  tfCombined?: boolean;
  tfMinAgree?: number;
  stratIndications?: boolean;
  stratBlock?: boolean;
  stratTrailing?: boolean;
  stratNormal?: boolean;
  relationalVolumeRatios?: RelationalVolumeRatios;
  strategyProcessingByRange?: Record<string, { normal?: boolean; trailing?: boolean }>;
  stratGeneral?: boolean;
  stratShortRange?: boolean;
  stratSignals?: boolean;
  signalConfigs?: Partial<SignalConfig>[];
  stratTrend?: boolean;
  stratBreakout?: boolean;
  stratReversion?: boolean;
  stratFlow?: boolean;
  stratMulti?: boolean;
  shortRangeStepMin?: number;
  shortRangeStepMax?: number;
  shortRangeMinPf?: number;
  minSlDistancePct?: number;
  microMinSlDistancePct?: number;
  microMinTrailDistancePct?: number;
  trailMinSteps?: number;
  trailMinLockPct?: number;
  groupGate?: boolean;
  groupGateN?: number;
  groupGatePf?: number;
  groupGateHours?: number;
  groupGateMin?: number;
  stratDca?: boolean;
  indTypeState?: boolean;
  indTypeDirection?: boolean;
  indTypeMove?: boolean;
  indTypeActive?: boolean;
  indTypeCommon?: boolean;
  indTypeSignals?: boolean;
  activeNoiseFilter?: number;
  activeVolatilityWeight?: number;
  posCountsVolumeRatio?: number;
  indRewardRisk?: number;
  indExtraSources?: boolean;
  histEnabled?: boolean;
  histLookbackBars?: number;
  histMinBars?: number;
  histWarmup?: number;
  histRefreshS?: number;
  setPfWindow?: number;
  setDeactN?: number;
  setMinPf?: number;
  setMaxDdtHours?: number;
  setGateWindow?: number;
  setGateMinTrades?: number;
  mainGateEnabled?: boolean;
  mainGateN?: number;
  stagesEnabled?: boolean;
  lastNDisjoint?: boolean;
  setGateShortMode?: "positive"|"negative";
  mainShortMode?: "positive"|"negative";
  stageFullWindow?: boolean;
  lastNBasis?: "time" | "count";
  setGateShortN?: number;
  minPfBySize?: boolean;
  minPfMicro?: number;
  minPfMinimal?: number;
  minPfShort?: number;
  minPfMedium?: number;
  minPfLong?: number;
  minPfSignals?: number;
  minPfMicroN?: number;
  minPfMinimalN?: number;
  minPfShortN?: number;
  minPfMediumN?: number;
  setGateShortMaxRatio?: number;
  setGateMinN?: number;
  setGateMinMode?: string;
  mainShortEnabled?: boolean;
  mainShortWindows?: number[];
  mainShortMaxRatio?: number;
  mainShortMin?: number;
  mainShortMargin?: number;
  mainMinN?: number;
  mainMinMode?: string;
  /** Earlier names of the Main short window keys (overlays written before the rename still load). */
  mainAutoEnabled?: boolean;
  mainAutoWindows?: number[];
  mainAutoMaxRatio?: number;
  mainAutoMin?: number;
  mainAutoMargin?: number;
  mainNegEnabled?: boolean;
  mainNegWindows?: number[];
  realGateEnabled?: boolean;
  realGateN?: number;
  realStagePaper?: boolean;
  minPfLinked?: boolean;
  histLookbackAuto?: boolean;
  histLookbackMax?: number;
  histLookbackTargetN?: number;
  setUseHistoricGate?: boolean;
  holdEnabled?: boolean;
  holdAuto?: boolean;
  holdAutoMax?: number;
  holdAutoMin?: number;
  directionMode?: string;
  minPfByStep?: boolean;
  minPfByRange?: Partial<Record<TpRangeId, number>>;
  rankByLane?: boolean;
  setScratchEnabled?: boolean;
  tpRanges?: Partial<Record<string, boolean>>;
  setSlRatios?: number[];
  trailStepMax?: number;
  trailSlRatios?: number[];
  volumeByPack?: Record<string, number>;
  volumeByKind?: Record<string, number>;
  volumeByRange?: Record<string, number>;
  volumeByPackKind?: Record<string, number>;
  slMaxByRange?: Record<string, number>;
  lastNByRange?: Record<string, LastNOverride>;
  exitSetsByPack?: Record<string, boolean>;
  trailRanges?: Partial<Record<string, boolean>>;
  trailPick?: string;
  trailPickByPack?: Record<string, string>;
  minPfStepAuto?: boolean;
  minPfStepRatio?: number;
  minPfStepAutoN?: number;
  minPfStepAutoMin?: number;
  minPfStepAutoScope?: string;
  minPfSetAutoMin?: number;
  minPfStepAutoTol?: number;
  minPfStepAutoLow?: number;
  minPfStepAutoHigh?: number;
  pauseMode?: string;
  pauseFixedMin?: number;
  pauseCandidates?: number[];
  pauseAutoMax?: number;
  pauseAutoMin?: number;
  pausePack?: Record<string, number>;
  indicationConfigs?: Partial<IndicationConfig>[];
  stepHoldOn?: boolean[];
  stepHoldMin?: number[];
  setMinSamples?: number;
  setMinStep?: number;
  minStepRange?: number;
  setStepMax?: number;
  setStepAdapt?: boolean;
  exitEnabled?: boolean;
  exitIgnoreTp?: boolean;
  exitBestOf?: boolean;
  exitLockOn?: boolean;
  exitPeakOn?: boolean;
  exitRevOn?: boolean;
  exitTimeOn?: boolean;
  exitLockPct?: number;
  exitBeBuffer?: number;
  exitOptSlPct?: number;
  exitOptSlMin?: number;
  exitOptSlMax?: number;
  exitMinHoldS?: number;
  setSlMinRatio?: number;
  execGateN?: number;
  slRatiosByRange?: Record<string, number[]>;
  exitSetHoldBars?: number;
  exitPfWindow?: number;
  exitDeactN?: number;
  exitMinPf?: number;
  exitAutoDeact?: boolean;
  rearrange?: boolean;
  rearrangeGap?: number;
  position_mode?: string;
  margin_mode?: string;
  leveragePercentage?: number;
  useMaximalLeverage?: boolean;
  live_trade_requested?: boolean;
  live_trading_enabled?: boolean;
  useSystemCloseOnly?: boolean;
  mainTradePfRatioSemantics?: string;
  settings_version?: string;
  updated_at?: string;
  prevPosWindow?: number;
  prev_pos_window?: number;
  prevPosMinCount?: number;
  prev_pos_min_count?: number;
  mainEvalPosCount?: number;
  realEvalPosCount?: number;
  minStep?: number;
  min_step?: number;
  trailingMinStep?: number;
  volume_type?: string;
  baseVolumeFactor?: number;
  volume_factor?: number;
  volume_factor_live?: number;
  live_volume_factor?: number;
  volume_factor_preset?: number;
  volume_factor_signal?: number;
  volume_step_ratio?: number;
  activeStopLossPositionCostRatios?: number[];
  activeTakeProfitMultipliers?: number[];
  activeOutbreakRanges?: number[];
  [key: string]: unknown;
};

export function num(v: unknown, d = 0): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : d;
}

export function bool(v: unknown, d = false): boolean {
  if (typeof v === "boolean") return v;
  if (v === 1 || v === "1" || v === "true") return true;
  if (v === 0 || v === "0" || v === "false") return false;
  if (v == null || v === "") return d;
  return d;
}

function arr<T>(v: unknown, d: T[] = []): T[] {
  return Array.isArray(v) ? (v as T[]) : d;
}

/** Minimal window rule: every one of the newest N results of one sign (positive / negative / both), or their average (avg*). */
export type MinMode = "positive" | "negative" | "both" | "avgpositive" | "avgnegative" | "avgboth";
const MIN_MODES: MinMode[] = ["positive", "negative", "both", "avgpositive", "avgnegative", "avgboth"];

export function minMode(v: unknown): MinMode {
  const m = String(v ?? "positive").toLowerCase() as MinMode;
  return MIN_MODES.includes(m) ? m : "positive";
}

const pauseMinutes = (v: unknown, d: number) => {
  const n = Math.round(Number(v));
  return Number.isFinite(n) ? Math.max(0, Math.min(PAUSE_MAX, n)) : d;
};
const pauseList = (v: unknown): number[] => {
  const raw = Array.isArray(v) ? v : typeof v === "string" ? v.split(/[,;\s]+/) : [];
  const out = [...new Set(raw.map((x) => pauseMinutes(x, 0)).filter((x) => x > 0))].sort((a, b) => a - b);
  return out.length ? out : [...PAUSE_CANDIDATES_DEFAULT];
};
const pausePackMap = (v: unknown): Record<string, number> => {
  const out: Record<string, number> = {};
  if (v && typeof v === "object" && !Array.isArray(v)) {
    for (const [k, m] of Object.entries(v as Record<string, unknown>)) {
      const n = pauseMinutes(m, 0);
      if (k && n > 0) out[k] = n;
    }
  }
  return out;
};
const strList = (v: unknown): string[] =>
  (Array.isArray(v) ? v : typeof v === "string" ? v.split(/[,;]/) : []).map((x) => String(x).trim()).filter((x) => x.length > 0);
const numList = (v: unknown, lo: number, hi: number): number[] =>
  [...new Set((Array.isArray(v) ? v : typeof v === "string" ? v.split(/[,;\s]+/) : []).map((x) => Math.round(Number(x))).filter((x) => Number.isFinite(x) && x >= lo && x <= hi))];
/** Normalise the saved range configurations; the engine expands and validates the names, so an unknown family is reported there, not silently kept here. */
/** Signals packs (engine `signal_packs`): every source x range x timeframe is its own pack sig-<source>-<range>-t<tf> with this entry's Set geometry. */
export const SIGNAL_SOURCES = ["ema-cross", "macd-cross", "rsi-reversal", "ema-trend", "rsi-momentum", "donchian", "bb-bounce", "cci-reversal", "obv-trend"] as const;
export const SIGNAL_RANGES = ["short", "medium"] as const;
export const SIGNAL_TIMEFRAMES = [1, 5, 15, 30] as const;
export interface SignalConfig {
  label: string;
  enabled: boolean;
  sources: string[];
  ranges: string[];
  timeframes: number[];
  stepMin: number;
  stepMax: number;
  slRatios: number[];
  types: string[];
}
export const DEFAULT_SIGNAL_CONFIG: SignalConfig = {
  label: "signals", enabled: true, sources: [...SIGNAL_SOURCES], ranges: [...SIGNAL_RANGES], timeframes: [5, 15],
  stepMin: 5, stepMax: 12, slRatios: [0.5, 0.75, 1.0], types: ["base", "trail"],
};
export function signalConfigList(v: unknown): SignalConfig[] {
  if (!Array.isArray(v)) return [];
  const words = (x: unknown): string[] => (Array.isArray(x) ? x : typeof x === "string" ? x.split(/[,;\s]+/) : []).map((y) => String(y).trim().toLowerCase()).filter(Boolean);
  return v.slice(0, 32).map((raw, i) => {
    const e = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
    const lo = Math.max(TP_STEP_MIN, Math.min(TP_STEP_MAX, Math.round(Number(e.stepMin) || DEFAULT_SIGNAL_CONFIG.stepMin)));
    const hi = Math.max(lo, Math.min(TP_STEP_MAX, Math.round(Number(e.stepMax) || DEFAULT_SIGNAL_CONFIG.stepMax)));
    const sources = words(e.sources).filter((s) => (SIGNAL_SOURCES as readonly string[]).includes(s));
    const ranges = words(e.ranges).filter((s) => (SIGNAL_RANGES as readonly string[]).includes(s));
    const tfs = words(e.timeframes).map(Number).filter((x) => (SIGNAL_TIMEFRAMES as readonly number[]).includes(x));
    const ratios = (Array.isArray(e.slRatios) ? e.slRatios : []).map(Number).filter((x) => Number.isFinite(x) && x >= 0.05 && x <= 5);
    const types = words(e.types).filter((t) => (SET_TYPES as readonly string[]).includes(t));
    return {
      label: String(e.label ?? `signals ${i + 1}`).slice(0, 60),
      enabled: e.enabled !== false,
      sources: sources.length ? sources : [...SIGNAL_SOURCES],
      ranges: ranges.length ? ranges : [...SIGNAL_RANGES],
      timeframes: tfs.length ? tfs : [...DEFAULT_SIGNAL_CONFIG.timeframes],
      stepMin: lo,
      stepMax: hi,
      slRatios: ratios.length ? ratios : [...DEFAULT_SIGNAL_CONFIG.slRatios],
      types: types.length ? types : [...DEFAULT_SIGNAL_CONFIG.types],
    };
  });
}

export function indicationConfigList(v: unknown): IndicationConfig[] {
  if (!Array.isArray(v)) return [];
  return v.slice(0, 64).map((raw, i) => {
    const e = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
    const tfRaw: unknown[] = Array.isArray(e.timeframes) ? e.timeframes : typeof e.timeframes === "string" ? e.timeframes.split(/[,;\s]+/) : [];
    const tfs = tfRaw
      .map((x) => (x === "c" || x === "x" ? x : Math.round(Number(x))))
      .filter((x) => x === "c" || x === "x" || (INDICATION_TIMEFRAMES as readonly number[]).includes(x as number)) as (number | "c" | "x")[];
    const lo = Math.max(TP_STEP_MIN, Math.min(TP_STEP_MAX, Math.round(Number(e.stepMin) || DEFAULT_INDICATION_CONFIG.stepMin)));
    const hi = Math.max(lo, Math.min(TP_STEP_MAX, Math.round(Number(e.stepMax) || DEFAULT_INDICATION_CONFIG.stepMax)));
    const types = strList(e.types).filter((t) => (SET_TYPES as readonly string[]).includes(t));
    const relRaw: unknown[] = Array.isArray(e.relations) ? e.relations : typeof e.relations === "string" ? e.relations.split(/[,;]/) : [""];
    const rel = relRaw.map((x) => String(x ?? "").trim());
    const ratios = (Array.isArray(e.slRatios) ? e.slRatios : []).map(Number).filter((x) => Number.isFinite(x) && x >= 0.25 && x <= 3.5);
    return {
      label: String(e.label ?? `entry ${i + 1}`).slice(0, 60),
      lastNLanes: lastNLanes(e.lastNLanes),
      symbols: strList(e.symbols),
      major: typeof e.major === "string" ? e.major : undefined,
      minor: typeof e.minor === "string" ? e.minor : undefined,
      coordination: typeof e.coordination === "string" ? e.coordination : undefined,
      enabled: e.enabled !== false,
      families: strList(e.families),
      names: strList(e.names),
      ranges: numList(e.ranges, 2, 60),
      timeframes: [...new Set(tfs)],
      relations: rel.length ? [...new Set(rel)] : [""],
      stepMin: lo,
      stepMax: hi,
      slRatios: ratios.length ? [...new Set(ratios)] : [...DEFAULT_INDICATION_CONFIG.slRatios],
      types: types.length ? types : [...DEFAULT_INDICATION_CONFIG.types],
      pause: e.pause === "auto" || e.pause === undefined ? "auto" : pauseMinutes(e.pause, 0),
      ...trailSpan(e.trailStepMin, e.trailStepMax),
    };
  });
}

/** An entry's trailing arm range: 0 = not set (every arm up to the entry's stepMax); else 2..50 steps, max >= min. */
function trailSpan(lo: unknown, hi: unknown): { trailStepMin: number; trailStepMax: number } {
  const l = Math.round(Number(lo) || 0);
  const h = Math.round(Number(hi) || 0);
  const tl = l >= 2 ? Math.min(TP_STEP_MAX, l) : 0;
  const th = h >= 2 ? Math.max(tl, Math.min(TP_STEP_MAX, h)) : 0;
  return { trailStepMin: tl, trailStepMax: th };
}

/** Trailing range switches: only explicit booleans of known names (missing = on). */
function trailRangesOf(raw: unknown): Partial<Record<TpRangeId, boolean>> {
  if (!raw || typeof raw !== "object") return {};
  const ids = TP_RANGES.map((r) => r.id) as string[];
  return Object.fromEntries(Object.entries(raw as Record<string, unknown>).filter(([k, v]) => ids.includes(k) && typeof v === "boolean")) as Partial<Record<TpRangeId, boolean>>;
}

/** Largest SL ratio per TP range: missing = the shipped limits (Long, Wide 1.0); {} = none; each 0.2 .. 3.5 (server: set_engine.sl_max_by_range_of). */
function slMaxByRangeOf(raw: unknown): Record<string, number> {
  if (raw === undefined || raw === null) return { long: 1, wide: 1 };
  if (typeof raw !== "object") return {};
  const ids = TP_RANGES.map((r) => r.id) as string[];
  return Object.fromEntries(
    Object.entries(raw as Record<string, unknown>)
      .filter(([k, v]) => ids.includes(k) && Number.isFinite(Number(v)) && Number(v) > 0)
      .map(([k, v]) => [k, Math.max(0.2, Math.min(3.5, Number(v)))]),
  );
}

export type LastNOverride = { baseLong?: number; baseShort?: number; mainLong?: number; mainShort?: number[]; negWindows?: number[]; realN?: number; realNegN?: number };
export const LAST_N_FIELDS: { key: keyof LastNOverride; label: string; lo: number; hi: number; list?: boolean }[] = [
  { key: "baseLong", label: "Base long", lo: 1, hi: 200 },
  { key: "baseShort", label: "Base short", lo: 0, hi: 200 },
  { key: "mainLong", label: "Main long", lo: 1, hi: 200 },
  { key: "mainShort", label: "Main short", lo: 1, hi: 200, list: true },
  { key: "negWindows", label: "Negative", lo: 1, hi: 200, list: true },
  { key: "realN", label: "Real", lo: 1, hi: 200 },
  { key: "realNegN", label: "Real Neg", lo: 1, hi: 200 },
];

export const BLOCK_KINDS = ["base", "exit", "trail"] as const;
/** blockKinds from a settings file: the known Set types in their fixed order; missing or unreadable = the shipped base + trailing (Exit-Set parents get no add). */
export function blockKindsOf(raw: unknown): string[] {
  if (!Array.isArray(raw)) return ["base", "trail"];
  const names = new Set(raw.map((x) => String(x).toLowerCase()));
  return BLOCK_KINDS.filter((k) => names.has(k));
}

/** lastNByRange from a settings file: only known ranges and keys, each bounded (server: set_engine.last_n_by_range_of). */
export function lastNByRangeOf(raw: unknown): Record<string, LastNOverride> {
  if (!raw || typeof raw !== "object") return {};
  const ids = TP_RANGES.map((r) => r.id) as string[];
  const out: Record<string, LastNOverride> = {};
  for (const [rng, o] of Object.entries(raw as Record<string, unknown>)) {
    if (!ids.includes(rng) || !o || typeof o !== "object") continue;
    const got: Record<string, number | number[]> = {};
    for (const f of LAST_N_FIELDS) {
      const v = (o as Record<string, unknown>)[f.key];
      if (v === undefined || v === null || v === "") continue;
      if (f.list) {
        const arr = (Array.isArray(v) ? v : String(v).split(/[,;]/)).map((x) => Math.round(Number(x))).filter((x) => Number.isFinite(x) && x >= 1 && x <= 200);
        if (arr.length) got[f.key] = Array.from(new Set(arr)).sort((a, b) => a - b);
      } else if (Number.isFinite(Number(v))) {
        got[f.key] = Math.max(f.lo, Math.min(f.hi, Math.round(Number(v))));
      }
    }
    if (Object.keys(got).length) out[rng] = got as LastNOverride;
  }
  return out;
}

/** How a lane's trailing Sets compete with its base and Exit Sets (server: set_engine.KIND_ORDERS). */
export const TRAIL_PICKS = ["after", "together", "first", "first-any"] as const;
export type TrailPick = (typeof TRAIL_PICKS)[number];

export type BlockCount = { step: number; ratio: number; on: boolean };
export const BLOCK_RELATIONS = ["symbol", "indication", "set", "step"] as const;
export type BlockRelation = (typeof BLOCK_RELATIONS)[number];
export const BLOCK_RATIO_MIN = 0.1;
export const BLOCK_RATIO_MAX = 1;
export const BLOCK_COUNTS_MAX = 12;

/** The block count table from an overlay value (server: block_rules.clean_cfg): each ratio 0.1 .. 1.0, each step 0.5 .. 20 PositionCosts, at most 12 counts; an unreadable entry is dropped. */
export function blockCountsOf(raw: unknown): BlockCount[] {
  const out: BlockCount[] = [];
  for (const item of Array.isArray(raw) ? raw : []) {
    const e = (item && typeof item === "object" ? item : {}) as Record<string, unknown>;
    const step = Number(e.step ?? 2);
    const ratio = Number(e.ratio ?? 0.5);
    if (!Number.isFinite(step) || !Number.isFinite(ratio)) continue;
    out.push({ step: Math.max(0.5, Math.min(20, step)), ratio: Math.round(Math.max(BLOCK_RATIO_MIN, Math.min(BLOCK_RATIO_MAX, ratio)) * 100) / 100, on: e.on !== false });
  }
  return out.length ? out.slice(0, BLOCK_COUNTS_MAX) : DEFAULT_OVERLAY.blockCounts.map((c) => ({ ...c }));
}

/** The enabled Block relations (symbol, indication, set, step): unknown names dropped, none left means the Set relation. */
export function blockRelationsOf(raw: unknown): BlockRelation[] {
  const names = (Array.isArray(raw) ? raw : []).map((x) => String(x).toLowerCase());
  const out = BLOCK_RELATIONS.filter((r) => names.includes(r));
  return out.length ? [...out] : ["symbol"];
}

/** The range switches from an overlay value: unknown names ignored, missing names at their default (server: position_cost.tp_ranges_on). */
export function tpRangesOf(raw: unknown): Record<TpRangeId, boolean> {
  const out: Record<TpRangeId, boolean> = { ...TP_RANGES_DEFAULT };
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const r of TP_RANGES) {
      const v = (raw as Record<string, unknown>)[r.id];
      if (typeof v === "boolean") out[r.id] = v;
    }
  }
  return out;
}

/** {name: multiple}, each bounded 0.1 .. 5, unreadable entries dropped (server: pulse_trader.volume_mults_of). */
export function volumeMultsOf(raw: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      const n = Number(v);
      if (Number.isFinite(n) && n > 0) out[k] = Math.max(0.1, Math.min(5, n));
    }
  }
  return out;
}

/** Only the listed trailing stop families: an unknown ratio is dropped, never snapped (server: SetBook.load). */
export function trailSlRatiosOf(raw: unknown): number[] {
  if (!Array.isArray(raw)) return [];
  const ok = new Set<number>();
  for (const x of raw) {
    const n = Number(x);
    const hit = TRAIL_SL_RATIOS.find((r) => Math.abs(r - n) < 1e-9);
    if (hit !== undefined) ok.add(hit);
  }
  return [...ok].sort((a, b) => a - b);
}

export function overlayFromCts(cts: CtsSettings, live?: Partial<PulseOverlay>): PulseOverlay {
  const coord = (cts.coordination_settings ?? cts.coordinationSettings ?? {}) as Record<string, unknown>;
  const trails = arr<string>(cts.strategyBaseTrailingVariants ?? coord.trailingVariants, [
    "0.3:0.1",
  ]);
  const first = String(trails[0] || "0.3:0.1").split(":");
  const arm = Number(first[0]) || 0.3;
  const give = Number(first[1]) || 0.1;
  const out: PulseOverlay = {
    ...DEFAULT_OVERLAY,
    strictSizeCap: bool(cts.strictSizeCap, false),
    maxModeledStopLossPct: Math.max(0, Math.min(100, num(cts.maxModeledStopLossPct, 0))),
    trailArmPct: arm,
    trailGivePct: give,
    controlOrders: bool(cts.control_orders, true),
    blockEnabled: bool(cts.variantBlockEnabled ?? cts.variant_block, true),
    blockMaxStack: num(cts.blockMaxStack ?? coord.blockMaxStack, 6),
    blockMinSamples: Math.max(1, Math.round(num(cts.blockMinSamples, 8))),
    blockCounts: blockCountsOf(cts.blockCounts),
    blockStackType: cts.blockStackType === "shared" ? "shared" : "additive",
    blockSharedTotal: Math.max(0.1, Math.min(3, num(cts.blockSharedTotal, 1))),
    blockMinObservations: Math.max(1, Math.min(200, Math.round(num(cts.blockMinObservations, 1)))),
    blockRelations: blockRelationsOf(cts.blockRelations),
    blockCarry: bool(cts.blockCarry, false),
    blockPauseSteps: Math.max(0, Math.min(20, Math.round(num(cts.blockPauseSteps, 1)))),
    blockSkipFirst: Math.max(0, Math.min(12, Math.round(num(cts.blockSkipFirst, 1)))),
    blockGuard: bool(cts.blockGuard, true),
    blockTargetShare: Math.max(0, Math.min(1, num(cts.blockTargetShare, 0.5))),
    blockKinds: blockKindsOf(cts.blockKinds),
    blockWindow: Math.max(1, Math.round(num(cts.blockWindow, 25))),
    mainEvalPosCount: Math.max(3, Math.round(num(cts.mainEvalPosCount, 20))),
    realEvalPosCount: Math.max(3, Math.round(num(cts.realEvalPosCount, 15))),
    prevPosMinCount: Math.max(1, Math.round(num(cts.prevPosMinCount ?? cts.prev_pos_min_count, 8))),
    prevPosWindow: Math.max(1, Math.round(num(cts.prevPosWindow ?? cts.prev_pos_window, 25))),
    exitMinSamples: Math.max(5, Math.round(num(cts.exitMinSamples, 8))),
    relateConfigs: relateConfigs(live?.relateConfigs ?? cts.relateConfigs),
    relationLanes: relationLanesOf(live?.relationLanes ?? cts.relationLanes),
    blockRounds: blockRoundsOf(live?.blockRounds ?? cts.blockRounds),
    blockVolumeRatio: num(cts.blockVolumeRatio ?? coord.blockVolumeRatio, 1),
    blockRiskPctOfBalance: num(cts.blockRiskPctOfBalance, 0),
    manualCloseRestMin: Math.max(0, Math.min(10080, Math.round(num(cts.manualCloseRestMin, 240)))),
    transferAdjust: bool(cts.transferAdjust, true),
    blockProfitFactorRatio: num(cts.blockProfitFactorRatio ?? coord.blockProfitFactorRatio, 0.8),
    blockPauseCountRatio: num(cts.blockPauseCountRatio ?? coord.blockPauseCountRatio, 1),
    blockActiveLive: bool(cts.blockActiveLiveEnabled ?? coord.blockActiveLiveEnabled, true),
    blockActiveReal: bool(cts.blockActiveRealEnabled ?? coord.blockActiveRealEnabled, true),
    dcaEnabled: bool(live?.dcaEnabled ?? cts.dcaEnabled ?? cts.variantDcaEnabled ?? cts.variant_dca, false),
    dcaMaxSteps: num(cts.dcaMaxSteps ?? coord.dcaMaxSteps, 0),
    dcaCooldownSeconds: num(cts.dcaCooldownSeconds ?? coord.dcaCooldownSeconds, 30),
    dcaBreakevenProfitPct: num(cts.dcaBreakevenProfitPct ?? coord.dcaBreakevenProfitPct, 0.2),
    dcaTakeProfitMode: String(cts.dcaTakeProfitMode ?? coord.dcaTakeProfitMode ?? "average"),
    dcaStepDistancesPct: arr<number>(cts.dcaStepDistancesPct ?? coord.dcaStepDistancesPct, [0.5, 1, 1.5, 2]),
    dcaStepVolumeMultipliers: arr<number>(cts.dcaStepVolumeMultipliers ?? coord.dcaStepVolumeMultipliers, [1.5, 2, 2.3, 2.5]),
    dcaAutoDeact: bool(cts.dcaAutoDeact, true),
    dcaAuto: bool(cts.dcaAuto, true),
    dcaOnSets: bool(cts.dcaOnSets, false),
    dcaOnAxis: bool(cts.dcaOnAxis, false),
    dcaMaxExposureX: num(cts.dcaMaxExposureX, 0),
    dcaMinPf: pfFloor(num(cts.dcaMinPf, GATE_MIN_PF_DEFAULT)),
    dcaPfWindow: num(cts.dcaPfWindow ?? cts.pfWindow, 15),
    dcaDeactN: num(cts.dcaDeactN, 25),
    volumeFactor: Math.max(0.05, Math.min(10, num(cts.volumeFactor, 0.05) || 0.05)),     // 0.05 = the minimum and the default; never 1 for a missing or zero value
    axisPrevEnabled: bool(cts.axisPrevEnabled ?? nestedAxis(coord, "prev", "enabled"), false),
    axisPrevMaxWindow: num(cts.axisPrevMaxWindow ?? nestedAxis(coord, "prev", "maxWindow"), 12),
    axisLastEnabled: bool(cts.axisLastEnabled ?? nestedAxis(coord, "last", "enabled"), false),
    axisLastMaxWindow: num(cts.axisLastMaxWindow ?? nestedAxis(coord, "last", "maxWindow"), 4),
    axisContEnabled: bool(cts.axisContEnabled ?? nestedAxis(coord, "cont", "enabled"), false),
    axisContMaxWindow: num(cts.axisContMaxWindow ?? nestedAxis(coord, "cont", "maxWindow"), 8),
    axisPauseEnabled: bool(cts.axisPauseEnabled ?? nestedAxis(coord, "pause", "enabled"), false),
    axisPauseMaxWindow: num(cts.axisPauseMaxWindow ?? nestedAxis(coord, "pause", "maxWindow"), 8),
    minPf: pfFloor(num((cts.strategies as { main?: { real?: { min_profit_factor?: number } } } | undefined)?.main?.real?.min_profit_factor ?? cts.realProfitFactor, GATE_MIN_PF_DEFAULT)),
    positionCostPct: num(cts.exchangePositionCost ?? cts.positionCost, 0.11),
    slippageAllowancePct: num(cts.slippageAllowancePct, 0.01),
    makerFeeRate: num(cts.makerFeeRate, 0.0005),
    takerFeeRate: num(cts.takerFeeRate, 0.0008),
    slippageFeeRatio: num(cts.slippageFeeRatio, 0.1),
    windowsLinked: bool(cts.windowsLinked, true),
    pfWindow: Math.max(8, num(cts.pfWindow, 15)),
    slMinPct: num(cts.slMinPct, 0.2),
    slMaxPct: num(cts.slMaxPct, 15.75),
    tpMinPct: num(cts.tpMinPct, 0.35),
    tpMaxPct: num(cts.tpMaxPct, 4.5),
    tpCostRatio: num(cts.tpCostRatio, 5),
    slToTpRatio: snapSlToTp(num(cts.slToTpRatio, 0.5)),
    slToTpAuto: bool(cts.slToTpAuto, true),
    slToTpRecalcN: Math.max(8, num(cts.slToTpRecalcN, 8)),
    slToTpRecalcEvery: num(cts.slToTpRecalcEvery, 8),
    trailAuto: bool(cts.trailAuto, true),
    trailRecalcN: Math.max(8, num(cts.trailRecalcN, 8)),
    trailRecalcEvery: num(cts.trailRecalcEvery, 8),
    trailArmMin: num(cts.trailArmMin, 0.3),
    trailArmMax: num(cts.trailArmMax, 1.5),
    trailGiveMin: num(cts.trailGiveMin, 0.1),
    trailGiveMax: num(cts.trailGiveMax, 0.5),
    trailGiveFactor: num(cts.trailGiveFactor, 0.333),
    trailRecalcGive: bool(cts.trailRecalcGive, true),
    tf1m: bool(cts.tf1m, true),
    tf5m: bool(cts.tf5m, true),
    tf15m: bool(cts.tf15m, true),
    tfCombined: bool(cts.tfCombined, true),
    tfMinAgree: num(cts.tfMinAgree, 2),
    stratIndications: bool(cts.stratIndications, true),
    stratBlock: bool(cts.stratBlock, true),
    stratTrailing: bool(cts.stratTrailing, true),
    stratNormal: bool(cts.stratNormal, true),
    relationalVolumeRatios: relationalVolumeSettings(cts.relationalVolumeRatios),
    strategyProcessingByRange: processingByRangeOf(cts.strategyProcessingByRange),
    stratGeneral: bool(cts.stratGeneral, true),
    stratShortRange: bool(cts.stratShortRange, false),
    stratSignals: bool(cts.stratSignals, false),
    signalConfigs: signalConfigList(cts.signalConfigs),
    stratTrend: bool(cts.stratTrend, false),
    stratBreakout: bool(cts.stratBreakout, false),
    stratReversion: bool(cts.stratReversion, false),
    stratFlow: bool(cts.stratFlow, false),
    stratMulti: bool(cts.stratMulti, false),
    shortRangeStepMin: num(cts.shortRangeStepMin, 2),
    shortRangeStepMax: num(cts.shortRangeStepMax, 6),
    shortRangeMinPf: pfFloor(num(cts.shortRangeMinPf, 1.1)),
    minSlDistancePct: Math.max(protectionPolicy.minSlDistancePct.min, Math.min(protectionPolicy.minSlDistancePct.max, num(cts.minSlDistancePct, protectionPolicy.minSlDistancePct.default))),
    microMinSlDistancePct: Math.max(0, Math.min(10, num(cts.microMinSlDistancePct, .1))),
    microMinTrailDistancePct: Math.max(0, Math.min(10, num(cts.microMinTrailDistancePct, .05))),
    trailMinSteps: num(cts.trailMinSteps, 2),
    trailMinLockPct: Math.max(0, Math.min(3, num(cts.trailMinLockPct, 0))),
    groupGate: bool(cts.groupGate, false),
    groupGateN: Math.max(1, Math.min(200, Math.round(num(cts.groupGateN, 10)))),
    groupGatePf: Math.max(0.5, Math.min(3, num(cts.groupGatePf, 1))),
    groupGateHours: Math.max(1, Math.min(72, num(cts.groupGateHours, 12))),
    groupGateMin: Math.max(1, Math.min(50, Math.round(num(cts.groupGateMin, 1)))),
    stratDca: bool(cts.stratDca ?? cts.dcaEnabled, false),
    indTypeState: bool(cts.indTypeState, true),
    indTypeDirection: bool(cts.indTypeDirection, true),
    indTypeMove: bool(cts.indTypeMove, true),
    indTypeActive: bool(cts.indTypeActive, true),
    indTypeCommon: bool(cts.indTypeCommon, true),
    indTypeSignals: bool(cts.indTypeSignals, true),
    noise: num(cts.activeNoiseFilter, 0.05),
    volWeight: num(cts.activeVolatilityWeight, 0.3),
    minStep: num((coord as { minStep?: number }).minStep, 6),
    maxStopLossRatio: num((coord as { maxStopLossRatio?: number }).maxStopLossRatio, 2.5),
    trailingMinStep: num((coord as { trailingMinStep?: number }).trailingMinStep, 6),
    posCountsVolumeRatio: num(cts.posCountsVolumeRatio ?? (coord as { posCountsVolumeRatio?: number }).posCountsVolumeRatio, 0.05),
    indRewardRisk: num(cts.indRewardRisk, 1.8),
    indExtraSources: bool(cts.indExtraSources, true),
    histEnabled: bool(cts.histEnabled, true),
    histLookbackBars: Math.max(120, Math.min(4320, Math.round(num(cts.histLookbackBars, 4320)))),
    histMinBars: num(cts.histMinBars, 120),
    histWarmup: num(cts.histWarmup, 30),
    histRefreshS: num(cts.histRefreshS, 90),
    setPfWindow: num(cts.setPfWindow ?? cts.pfWindow, 15),
    setDeactN: num(cts.setDeactN, 25),
    setMinPf: pfFloor(num(cts.setMinPf ?? cts.realProfitFactor, GATE_MIN_PF_DEFAULT)),
    setMaxDdtHours: num(cts.setMaxDdtHours, 24),
    setGateWindow: Math.max(1, Math.min(200, Math.round(num(cts.setGateWindow, 50)))),
    setGateMinTrades: Math.max(0, Math.min(200, Math.round(num(cts.setGateMinTrades, 0)))),
    stagesEnabled: bool(cts.stagesEnabled, true),
    lastNDisjoint: bool(cts.lastNDisjoint,false),
    setGateShortMode: cts.setGateShortMode==="negative"?"negative":"positive",
    mainShortMode: cts.mainShortMode==="negative"?"negative":"positive",
    stageFullWindow: bool(cts.stageFullWindow, false),
    lastNBasis: cts.lastNBasis === "count" ? "count" : "time",
    setGateShortN: Math.max(0, Math.min(200, Math.round(num(cts.setGateShortN, 25)))),
    minPfBySize: bool(cts.minPfBySize, true),
    minPfMicro: pfFloor(num(cts.minPfMicro, PF_TYPE_DEFAULTS.minPfMicro)),
    minPfMinimal: pfFloor(num(cts.minPfMinimal, PF_TYPE_DEFAULTS.minPfMinimal)),
    minPfShort: pfFloor(num(cts.minPfShort, PF_TYPE_DEFAULTS.minPfShort)),
    minPfMedium: pfFloor(num(cts.minPfMedium, PF_TYPE_DEFAULTS.minPfMedium)),
    minPfLong: pfFloor(num(cts.minPfLong, PF_TYPE_DEFAULTS.minPfLong)),
    minPfSignals: Math.max(PF_TYPE_DEFAULTS.minPfSignals, pfFloor(num(cts.minPfSignals, PF_TYPE_DEFAULTS.minPfSignals))),
    minPfMicroN: Math.max(1, Math.round(num(cts.minPfMicroN, 5))),
    minPfMinimalN: Math.max(1, Math.round(num(cts.minPfMinimalN, 10))),
    minPfShortN: Math.max(1, Math.round(num(cts.minPfShortN, 20))),
    minPfMediumN: Math.max(1, Math.round(num(cts.minPfMediumN, 35))),
    setGateShortMaxRatio: Math.max(0.1, Math.min(1, num(cts.setGateShortMaxRatio, 0.5))),
    setGateMinN: Math.max(0, Math.min(5, Math.round(num(cts.setGateMinN, 0)))),
    setGateMinMode: minMode(cts.setGateMinMode),
    mainGateEnabled: bool(cts.mainGateEnabled, true),
    mainGateN: Math.max(1, Math.min(200, Math.round(num(cts.mainGateN, 25)))),
    mainShortEnabled: bool(cts.mainShortEnabled ?? cts.mainAutoEnabled, true),
    mainShortWindows: windows(cts.mainShortWindows ?? cts.mainAutoWindows, [5, 10, 15]),
    mainShortMaxRatio: Math.max(0.1, Math.min(1, num(cts.mainShortMaxRatio ?? cts.mainAutoMaxRatio, 0.5))),
    mainShortMin: Math.max(1, Math.min(200, Math.round(num(cts.mainShortMin ?? cts.mainAutoMin, 1)))),
    mainShortMargin: Math.max(1, Math.min(2, num(cts.mainShortMargin ?? cts.mainAutoMargin, 1.02))),
    mainMinN: Math.max(0, Math.min(5, Math.round(num(cts.mainMinN, 0)))),
    mainMinMode: minMode(cts.mainMinMode),
    mainNegEnabled: bool(cts.mainNegEnabled, true),
    mainNegWindows: windows(cts.mainNegWindows, [5, 10]),
    realGateEnabled: bool(cts.realGateEnabled, true),
    realGateN: Math.max(1, Math.min(200, Math.round(num(cts.realGateN, 20)))),
    realStagePaper: bool(cts.realStagePaper, true),
    minPfLinked: bool(cts.minPfLinked, true),
    histLookbackAuto: bool(cts.histLookbackAuto, true),
    histLookbackMax: Math.max(120, Math.min(4320, Math.round(num(cts.histLookbackMax, 4320)))),
    histLookbackTargetN: Math.max(1, Math.min(200, Math.round(num(cts.histLookbackTargetN, 12)))),
    setUseHistoricGate: bool(cts.setUseHistoricGate, true),
    holdEnabled: bool(cts.holdEnabled, true),
    holdAuto: bool(cts.holdAuto, false),
    holdAutoMax: Math.max(10, Math.min(50, Math.round(num(cts.holdAutoMax, 50)))),
    holdAutoMin: Math.max(3, Math.min(50, Math.round(num(cts.holdAutoMin, 10)))),
    directionMode: cts.directionMode === "both" ? "both" : "split",
    minPfByStep: bool(cts.minPfByStep, true),
    minPfByRange: pfRangeFloorsOf(cts.minPfByRange ?? DEFAULT_OVERLAY.minPfByRange),
    rankByLane: bool(cts.rankByLane, true),
    setScratchEnabled: bool(cts.setScratchEnabled, false),
    tpRanges: tpRangesOf(cts.tpRanges),
    setSlRatios: setSlRatiosOf(cts.setSlRatios),
    trailStepMax: Math.max(2, Math.min(TRAIL_STEP_CEIL, Math.round(num(cts.trailStepMax, TRAIL_STEP_MAX_DEFAULT)))),
    trailSlRatios: trailSlRatiosOf(cts.trailSlRatios),
    volumeByPack: volumeMultsOf(cts.volumeByPack),
    volumeByKind: volumeMultsOf(cts.volumeByKind),
    volumeByRange: volumeMultsOf(cts.volumeByRange),
    volumeByPackKind: cts.volumeByPackKind && typeof cts.volumeByPackKind === "object" ? volumeMultsOf(cts.volumeByPackKind) : {},
    slMaxByRange: slMaxByRangeOf(cts.slMaxByRange),
    lastNByRange: lastNByRangeOf(cts.lastNByRange),
    exitSetsByPack: Object.fromEntries(
      Object.entries(cts.exitSetsByPack && typeof cts.exitSetsByPack === "object" ? cts.exitSetsByPack : {}).filter(([, v]) => typeof v === "boolean"),
    ) as Record<string, boolean>,
    trailRanges: { micro: true, minimal: true, short: true, normal: true, long: true, wide: true, ...trailRangesOf(cts.trailRanges) },
    trailPick: (TRAIL_PICKS as readonly string[]).includes(String(cts.trailPick)) ? (cts.trailPick as TrailPick) : "after",
    trailPickByPack: Object.fromEntries(
      Object.entries(cts.trailPickByPack && typeof cts.trailPickByPack === "object" ? cts.trailPickByPack : {}).filter(([, v]) =>
        (TRAIL_PICKS as readonly string[]).includes(String(v)),
      ),
    ) as Record<string, TrailPick>,
    minPfStepAuto: bool(cts.minPfStepAuto, true),
    minPfStepRatio: stepPfRatio(cts.minPfStepRatio),
    minPfStepAutoN: Math.max(3, Math.min(50, Math.round(num(cts.minPfStepAutoN, 10)))),
    minPfStepAutoMin: Math.max(1, Math.min(100000, Math.round(num(cts.minPfStepAutoMin, 1)))),
    minPfStepAutoScope: cts.minPfStepAutoScope === "step" ? "step" : "set",
    minPfSetAutoMin: Math.max(1, Math.min(100000, Math.round(num(cts.minPfSetAutoMin, 10)))),
    minPfStepAutoTol: Math.max(0, Math.min(50, Math.round(num(cts.minPfStepAutoTol, 5)))),
    minPfStepAutoLow: Math.max(1, Math.min(2, Math.round(num(cts.minPfStepAutoLow, 1.02) * 100) / 100)),
    minPfStepAutoHigh: Math.max(1, Math.min(2, Math.round(num(cts.minPfStepAutoHigh, 2) * 100) / 100)),
    pauseMode: PAUSE_MODES.includes(cts.pauseMode as PauseMode) ? (cts.pauseMode as PauseMode) : DEFAULT_OVERLAY.pauseMode,
    pauseFixedMin: pauseMinutes(cts.pauseFixedMin, DEFAULT_OVERLAY.pauseFixedMin),
    pauseCandidates: pauseList(cts.pauseCandidates),
    pauseAutoMax: Math.max(10, Math.min(500, Math.round(num(cts.pauseAutoMax, 100)))),
    pauseAutoMin: Math.max(8, Math.min(500, Math.round(num(cts.pauseAutoMin, 30)))),
    pausePack: pausePackMap(cts.pausePack),
    indicationConfigs: indicationConfigList(cts.indicationConfigs),
    stepHoldOn: Array.isArray(cts.stepHoldOn) ? cts.stepHoldOn.slice(0, 12).map(Boolean) : DEFAULT_OVERLAY.stepHoldOn,
    stepHoldMin: Array.isArray(cts.stepHoldMin) ? cts.stepHoldMin.slice(0, 12).map((x) => Math.round(Number(x) || 0)) : DEFAULT_OVERLAY.stepHoldMin,
    setMinSamples: num(cts.setMinSamples, 12),
    setMinStep: num(cts.setMinStep ?? cts.minStepRange, 2),
    setStepMax: num(cts.setStepMax, TP_STEP_DEFAULT_MAX),
    setStepAdapt: bool(cts.setStepAdapt, false),
    exitEnabled: bool(cts.exitEnabled, true),
    exitIgnoreTp: bool(cts.exitIgnoreTp, true),
    exitBestOf: bool(cts.exitBestOf, true),
    exitLockOn: bool(cts.exitLockOn, true),
    exitPeakOn: bool(cts.exitPeakOn, true),
    exitRevOn: bool(cts.exitRevOn, false),
    exitTimeOn: bool(cts.exitTimeOn, false),
    exitLockPct: num(cts.exitLockPct, 0.15),
    exitBeBuffer: num(cts.exitBeBuffer, 0.04),
    exitOptSlPct: num(cts.exitOptSlPct, 0.3),
    exitOptSlMin: num(cts.exitOptSlMin, 0.1),
    exitOptSlMax: num(cts.exitOptSlMax, 0.9),
    exitMinHoldS: num(cts.exitMinHoldS, 45),
    setSlMinRatio: num(cts.setSlMinRatio, 0.5),
    execGateN: Math.max(0, Math.min(200, Math.round(num(cts.execGateN, 0)))),
    slRatiosByRange: ((raw: unknown): Record<string, number[]> => {
      if (!raw || typeof raw !== "object") return { long: [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8], wide: [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8] };
      const out: Record<string, number[]> = {};
      for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
        const xs = Array.isArray(v) ? v.filter((x): x is number => typeof x === "number" && Number.isFinite(x) && x >= 0.05 && x <= 5) : [];
        if (xs.length) out[k] = [...new Set(xs.map((x) => Math.round(x * 100) / 100))].sort((a, b) => a - b);
      }
      return out;
    })(cts.slRatiosByRange),
    exitSetHoldBars: num(cts.exitSetHoldBars, 720),
    exitPfWindow: num(cts.exitPfWindow, 15),
    exitDeactN: num(cts.exitDeactN, 25),
    exitMinPf: pfFloor(num(cts.exitMinPf, GATE_MIN_PF_DEFAULT)),
    exitAutoDeact: bool(cts.exitAutoDeact, true),
    rearrange: bool(cts.rearrange, true),
    rearrangeGap: num(cts.rearrangeGap, 0.22),
    ...live,
  };
  out.histLookbackBars = Math.max(120, Math.min(4320, Math.round(num(out.histLookbackBars, 4320))));
  out.relationalVolumeRatios = relationalVolumeSettings(out.relationalVolumeRatios);
  out.strategyProcessingByRange = processingByRangeOf(out.strategyProcessingByRange);
  out.minPfByRange = pfRangeFloorsOf(out.minPfByRange);
  for (const key of Object.keys(PF_TYPE_DEFAULTS) as (keyof typeof PF_TYPE_DEFAULTS)[]) {
    out[key] = pfFloor(out[key] == null ? PF_TYPE_DEFAULTS[key] : num(out[key], PF_TYPE_DEFAULTS[key]));
  }
  out.minPfMicroN = Math.max(1, Math.round(num(out.minPfMicroN, 5)));
  out.minPfMinimalN = Math.max(out.minPfMicroN, Math.round(num(out.minPfMinimalN, 10)));
  out.minPfShortN = Math.max(out.minPfMinimalN, Math.round(num(out.minPfShortN, 20)));
  out.minPfMediumN = Math.max(out.minPfShortN, Math.round(num(out.minPfMediumN, 35)));
  out.histLookbackMax = Math.max(120, Math.min(4320, Math.round(num(out.histLookbackMax, 4320))));
  out.histLookbackTargetN = Math.max(1, Math.min(200, Math.round(num(out.histLookbackTargetN, 12))));
  out.slToTpRatio = snapSlToTp(num(out.slToTpRatio, 0.5));
  out.modules = {
    ...(DEFAULT_OVERLAY.modules ?? {}),
    ...(typeof live?.modules === "object" && live.modules ? live.modules : {}),
  };
  // A raw module disable must not be displayed or re-saved as an enabled strategy.
  out.minPfSignals = Math.max(PF_TYPE_DEFAULTS.minPfSignals, pfFloor(num(out.minPfSignals, PF_TYPE_DEFAULTS.minPfSignals)));
  out.stratTrailing = out.stratTrailing && out.modules["strategy.trailing"] !== false;
  if (Array.isArray(live?.symbols)) out.symbols = live.symbols as string[];
  if (live?.symbolsAll != null) out.symbolsAll = bool(live.symbolsAll, out.symbolsAll);
  out.symbolSort = coerceSymbolSort(out.symbolSort ?? live?.symbolSort);
  out.symbolsDynamic = bool(out.symbolsDynamic, true);
  out.symbolCap = Math.max(0, Math.round(num(out.symbolCap, 0)));
  if (out.trailRecalcGive && live?.trailGivePct == null) {
    out.trailGivePct = trailGiveFromArm(out.trailArmPct, out.trailGiveFactor, out.trailGiveMin, out.trailGiveMax);
  }
  return out;
}

function nestedAxis(coord: Record<string, unknown>, axis: string, field: string): unknown {
  const axes = coord.axes as Record<string, Record<string, unknown>> | undefined;
  return axes?.[axis]?.[field];
}

export function blockTable(ratio: number, pfRatio: number, defaultMinPf: number, baseQty = 1, stack = 0) {
  const rows = [];
  const nMax = stack > 0 ? Math.min(stack, 24) : 12;
  for (let n = 1; n <= nMax; n += 1) {
    const inc = n * ratio;
    rows.push({
      n,
      inc,
      add: baseQty * inc,
      tot: baseQty + baseQty * inc,
      minPf: 1 + Math.max(0, defaultMinPf - 1) * pfRatio * inc,
    });
  }
  return rows;
}

export type CtsBundle = {
  cts: CtsSettings | null;
  overlay: Partial<PulseOverlay> | null;
  conn?: string;
};

export async function fetchCtsBundle(conn = "overall"): Promise<CtsBundle> {
  if (conn === "overall") return { cts: null, overlay: null, conn: "overall" };
  try {
    const r = await fetch(`/config.json?conn=${encodeURIComponent(conn)}`, { cache: "no-store" });
    if (!r.ok) return { cts: null, overlay: loadLocalOverlay(conn), conn };
    const j = (await r.json()) as {
      cts?: CtsSettings;
      overlay?: Partial<PulseOverlay>;
      conn?: string;
    } & CtsSettings;
    const overlay =
      j?.overlay && typeof j.overlay === "object"
        ? (j.overlay as Partial<PulseOverlay>)
        : null;
    const cts =
      j?.cts && typeof j.cts === "object"
        ? j.cts
        : j && typeof j === "object" && ("blockMaxStack" in j || "strategies" in j) && !overlay
          ? (j as CtsSettings)
          : ((j.cts as CtsSettings) ?? null);
    return { cts, overlay, conn: j?.conn || conn };
  } catch {
    return { cts: null, overlay: loadLocalOverlay(conn), conn };
  }
}

export async function fetchCtsSettings(conn = "overall"): Promise<CtsSettings | null> {
  const b = await fetchCtsBundle(conn);
  return b.cts;
}

export function syncOverlayFlags(overlay: PulseOverlay): PulseOverlay {
  const next: PulseOverlay = {
    ...overlay,
    relateConfigs: relateConfigs(overlay.relateConfigs),
    relationLanes: relationLanesOf(overlay.relationLanes),
    blockRounds: blockRoundsOf(overlay.blockRounds),
    minPfSignals: Math.max(PF_TYPE_DEFAULTS.minPfSignals, pfFloor(num(overlay.minPfSignals, PF_TYPE_DEFAULTS.minPfSignals))),
    symbols: capSymbols(overlay.symbols || []),
  };
  if (overlay.symbolsAll || next.symbols.includes("*") || next.symbols.includes("ALL")) {
    next.symbols = ["*"];
    next.symbolsAll = true;
  } else {
    next.symbolsAll = false;
    if (!next.symbols.length) next.symbols = [...PULSE_SYMBOLS];
  }
  next.symbolSort = coerceSymbolSort(next.symbolSort);
  next.symbolsDynamic = next.symbolsDynamic !== false;
  next.symbolCap = Math.max(0, Math.round(Number(next.symbolCap) || 0));
  const steps = Math.max(0, Math.round(Number(next.dcaMaxSteps) || 0));
  next.dcaMaxSteps = steps;
  const dist = [...(next.dcaStepDistancesPct || [0.5, 1, 1.5, 2])];
  const mult = [...(next.dcaStepVolumeMultipliers || [1.5, 2, 2.3, 2.5])];
  const seed = steps > 0 ? steps : Math.max(dist.length, mult.length, 4);
  while (dist.length < seed) dist.push(Number((dist[dist.length - 1] + 0.5).toFixed(2)));
  while (mult.length < seed) mult.push(mult[mult.length - 1] ?? 1.5);
  next.dcaStepDistancesPct = steps > 0 ? dist.slice(0, steps) : dist;
  next.dcaStepVolumeMultipliers = steps > 0 ? mult.slice(0, steps) : mult;
  const m: Record<string, boolean> = { ...(next.modules ?? {}) };
  m["strategy.block"] = Boolean(next.blockEnabled && next.stratBlock);
  m["strategy.dca"] = next.dcaEnabled === true && next.stratDca === true;
  m["exec.controls"] = Boolean(next.controlOrders);
  m["strategy.rearrange"] = Boolean(next.rearrange);
  m["strategy.indications"] = Boolean(next.indEnabled && next.stratIndications);
  m["feed.tf1m"] = Boolean(next.tf1m);
  m["feed.tf5m"] = Boolean(next.tf5m);
  m["feed.tf15m"] = Boolean(next.tf15m);
  m["feed.tfCombined"] = Boolean(next.tfCombined);
  m["strategy.exits"] = Boolean(next.exitEnabled);
  m["core.historic"] = Boolean(next.histEnabled);
  m["strategy.sets"] = Boolean(next.histEnabled);
  m["strategy.coord"] = Boolean(
    next.axisPrevEnabled || next.axisLastEnabled || next.axisContEnabled || next.axisPauseEnabled,
  );
  m["strategy.trailing"] = Boolean(next.stratTrailing);
  m["strategy.trailRecalc"] = Boolean(next.trailAuto && next.stratTrailing);
  m["risk.slTpRatios"] = true;
  m["feed.universeRank"] = Boolean(next.symbolsDynamic);
  next.modules = m;
  return next;
}

export async function saveOverlay(
  overlay: PulseOverlay,
  conn = "",
): Promise<{ ok: boolean; detail: string; overlay?: Partial<PulseOverlay>; conn?: string }> {
  const next = syncOverlayFlags(overlay);
  const desk = conn === "overall" ? "" : conn;
  try {
    if (desk) localStorage.setItem(`x01-pulse-overlay-${desk}`, JSON.stringify(next));
  } catch {
    /* ignore */
  }
  if (!desk) return { ok: false, detail: "Pick Live or VST to save" };
  try {
    const r = await fetch(`/config.json?conn=${encodeURIComponent(desk)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overlay: next }),
    });
    const j = (await r.json().catch(() => ({}))) as {
      ok?: boolean;
      detail?: string;
      overlay?: Partial<PulseOverlay>;
      conn?: string;
    };
    if (r.ok && j.ok !== false) {
      const applied = syncOverlayFlags({ ...next, ...(j.overlay || {}) } as PulseOverlay);
      try {
        localStorage.setItem(`x01-pulse-overlay-${desk}`, JSON.stringify(applied));
      } catch {
        /* ignore */
      }
      return {
        ok: true,
        detail: `Saved ${j.conn || desk} · engine reload`,
        overlay: applied,
        conn: j.conn || desk,
      };
    }
    return { ok: false, detail: String(j.detail || `Save rejected (${r.status})`) };
  } catch (e) {
    return { ok: false, detail: `Save failed · ${String(e)}` };
  }
}

export function loadLocalOverlay(conn = ""): Partial<PulseOverlay> | null {
  try {
    if (!conn || conn === "overall") return null;
    const raw = localStorage.getItem(`x01-pulse-overlay-${conn}`);
    if (!raw) return null;
    return JSON.parse(raw) as Partial<PulseOverlay>;
  } catch {
    return null;
  }
}

/** Only exact independent ranges and literal booleans are persisted. */
export function processingByRangeOf(raw: unknown): Record<string, {normal?:boolean;trailing?:boolean}> {
  const result: Record<string, {normal?:boolean;trailing?:boolean}> = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return result;
  for (const key of [...TP_RANGES.map(r=>r.id), "signals", "exit"]) {
    const value = (raw as Record<string, unknown>)[key];
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const own: {normal?:boolean;trailing?:boolean} = {};
    for (const field of ["normal", "trailing"] as const) if (typeof (value as Record<string, unknown>)[field] === "boolean") own[field] = (value as Record<string, boolean>)[field];
    result[key] = own;
  }
  return result;
}
