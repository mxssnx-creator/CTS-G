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
  const out = [...new Set(src.map((x) => Math.round(Number(x))).filter((x) => Number.isFinite(x)).map((x) => Math.max(2, Math.min(200, x))))];
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
export const SL_TP_RATIOS = [0.3, 0.6, 0.9, 1.2, 1.5] as const;
/** Set grid: SL = TP x ratio, 0.5..3.5 step 0.25 (13 ratios, each one a Set). Matches position_cost.SL_RATIOS. */
export const SL_SET_RATIOS: readonly number[] = Array.from({ length: 13 }, (_, i) => Number((0.5 + i * 0.25).toFixed(2)));
export const TRAIL_VARIANTS = ["0.3:0.1", "0.6:0.2", "0.9:0.3", "1.2:0.4", "1.5:0.5"] as const;

export function snapSlToTp(v: number, lo = 0.3, hi = 1.5, step = 0.3): number {
  const x = Math.min(hi, Math.max(lo, Number.isFinite(v) ? v : 0.6));
  return Math.round((lo + Math.round((x - lo) / step) * step) * 10) / 10;
}

export function trailGiveFromArm(arm: number, factor = 1 / 3, gmin = 0.1, gmax = 0.5): number {
  const g = arm * factor;
  return Math.round(Math.min(gmax, Math.max(gmin, g)) * 100) / 100;
}

export type PulseOverlay = {
  targetNotional: number;
  volumeFactor: number;
  /** Percent of the balance per position before the volume factor; 0 = the fixed targetNotional. */
  sizePctOfBalance: number;
  /** Hard ceiling for one position's notional in USDT, whatever the balance. */
  sizeMaxNotional: number;
  /** A fill farther than this (percent) from the price the entry was decided at is closed at once and the symbol rests. */
  maxEntrySlipPct: number;
  /** Drawdown from the start equity (percent) that halts entries; 0 = off. */
  ddHaltPct: number;
  leverage: number;
  useMaxLeverage: boolean;
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
  blockVolumeRatio: number;
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
  stratGeneral: boolean;
  /** Short Range strategy: the Low-Range signals, TP steps shortRangeStepMin..Max (default 2-6). */
  stratShortRange: boolean;
  stratTrend: boolean;
  stratBreakout: boolean;
  stratReversion: boolean;
  stratFlow: boolean;
  stratMulti: boolean;
  shortRangeStepMin: number;
  shortRangeStepMax: number;
  /** Short Range has its own Base PF floor, whatever the global setMinPf is (default 1.1). */
  shortRangeMinPf: number;
  /** Overall minimum stop = PositionCost + slMinSteps steps (one step = one PositionCost). */
  slMinSteps: number;
  /** Minimum trailing distance = PositionCost + trailMinSteps steps. */
  trailMinSteps: number;
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
  /** Base: the second last N; both Base windows must pass (0 = off). */
  setGateShortN: number;
  mainGateEnabled: boolean;
  mainGateN: number;
  /** Main Auto: each Set picks its own window from these on its Main Set. */
  mainAutoEnabled: boolean;
  mainAutoWindows: number[];
  mainAutoMin: number;
  mainAutoMargin: number;
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
  setUseHistoricGate: boolean;
  holdEnabled: boolean;
  holdAuto: boolean;
  holdAutoMax: number;
  holdAutoMin: number;
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
  exitPfWindow: number;
  exitDeactN: number;
  exitMinPf: number;
  exitAutoDeact: boolean;
  modules?: Record<string, boolean>;
};

export const DEFAULT_OVERLAY: PulseOverlay = {
  targetNotional: 2.15,
  volumeFactor: 0.05,
  sizePctOfBalance: 0,
  sizeMaxNotional: 25,
  maxEntrySlipPct: 0.75,
  ddHaltPct: 18,
  leverage: 150,
  useMaxLeverage: true,
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
  blockVolumeRatio: 1,
  blockProfitFactorRatio: 0.8,
  blockPauseCountRatio: 1,
  blockActiveLive: true,
  blockActiveReal: true,
  dcaEnabled: true,
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
  dcaMaxExposureX: 2,
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
  axisPrevEnabled: false,
  axisPrevMaxWindow: 12,
  axisLastEnabled: false,
  axisLastMaxWindow: 4,
  axisContEnabled: false,
  axisContMaxWindow: 8,
  axisPauseEnabled: false,
  axisPauseMaxWindow: 8,
  minPf: GATE_MIN_PF_DEFAULT,
  positionCostPct: 0.12,
  windowsLinked: true,
  pfWindow: 8,
  slMinPct: 0.2,
  slMaxPct: 15.75,
  tpMinPct: 0.35,
  tpMaxPct: 4.5,
  tpCostRatio: 5,
  slToTpRatio: 0.6,
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
  stratGeneral: true,
  stratShortRange: false,
  stratTrend: false,
  stratBreakout: false,
  stratReversion: false,
  stratFlow: false,
  stratMulti: false,
  shortRangeStepMin: 2,
  shortRangeStepMax: 6,
  shortRangeMinPf: 1.25,
  slMinSteps: 1,
  trailMinSteps: 2,
  stratDca: true,
  indTypeState: true,
  indTypeDirection: true,
  indTypeMove: true,
  indTypeActive: true,
  indTypeCommon: true,
  indTypeSignals: true,
  noise: 0.05,
  volWeight: 0.3,
  minStep: 10,
  maxStopLossRatio: 2.5,
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
  setMaxDdtHours: 12,
  setGateWindow: 50,
  setGateMinTrades: 30,
  stagesEnabled: true,
  setGateShortN: 15,
  mainGateEnabled: true,
  mainGateN: 25,
  mainAutoEnabled: true,
  mainAutoWindows: [5, 10, 15],
  mainAutoMin: 8,
  mainAutoMargin: 1.02,
  mainNegEnabled: true,
  mainNegWindows: [5, 10],
  realGateEnabled: true,
  realGateN: 15,
  realStagePaper: true,
  minPfLinked: true,
  histLookbackAuto: true,
  histLookbackMax: 2880,
  setUseHistoricGate: true,
  holdEnabled: true,
  holdAuto: true,
  holdAutoMax: 50,
  holdAutoMin: 10,
  stepHoldOn: [false, false, false, false, false, false, false, false, false, false, false, false],
  stepHoldMin: [15, 15, 15, 15, 15, 120, 120, 120, 120, 120, 120, 120],
  setMinSamples: 12,
  setMinStep: 2,
  setStepMax: 30,
  setStepAdapt: true,
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

export type CtsSettings = {
  coordination_settings?: Record<string, unknown>;
  coordinationSettings?: Record<string, unknown>;
  strategyBaseTrailingVariants?: string[];
  control_orders?: boolean | number | string;
  variantBlockEnabled?: boolean;
  variant_block?: boolean;
  blockMaxStack?: number;
  blockMinSamples?: number;
  blockWindow?: number;
  exitMinSamples?: number;
  blockVolumeRatio?: number;
  blockProfitFactorRatio?: number;
  blockPauseCountRatio?: number;
  blockActiveLiveEnabled?: boolean;
  blockActiveRealEnabled?: boolean;
  dcaEnabled?: boolean;
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
  stratGeneral?: boolean;
  stratShortRange?: boolean;
  stratTrend?: boolean;
  stratBreakout?: boolean;
  stratReversion?: boolean;
  stratFlow?: boolean;
  stratMulti?: boolean;
  shortRangeStepMin?: number;
  shortRangeStepMax?: number;
  shortRangeMinPf?: number;
  slMinSteps?: number;
  trailMinSteps?: number;
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
  setGateShortN?: number;
  mainAutoEnabled?: boolean;
  mainAutoWindows?: number[];
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
  setUseHistoricGate?: boolean;
  holdEnabled?: boolean;
  holdAuto?: boolean;
  holdAutoMax?: number;
  holdAutoMin?: number;
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
    trailArmPct: arm,
    trailGivePct: give,
    controlOrders: bool(cts.control_orders, true),
    blockEnabled: bool(cts.variantBlockEnabled ?? cts.variant_block, true),
    blockMaxStack: num(cts.blockMaxStack ?? coord.blockMaxStack, 6),
    blockMinSamples: Math.max(1, Math.round(num(cts.blockMinSamples, 8))),
    blockWindow: Math.max(1, Math.round(num(cts.blockWindow, 25))),
    mainEvalPosCount: Math.max(3, Math.round(num(cts.mainEvalPosCount, 20))),
    realEvalPosCount: Math.max(3, Math.round(num(cts.realEvalPosCount, 15))),
    prevPosMinCount: Math.max(1, Math.round(num(cts.prevPosMinCount ?? cts.prev_pos_min_count, 8))),
    prevPosWindow: Math.max(1, Math.round(num(cts.prevPosWindow ?? cts.prev_pos_window, 25))),
    exitMinSamples: Math.max(5, Math.round(num(cts.exitMinSamples, 8))),
    blockVolumeRatio: num(cts.blockVolumeRatio ?? coord.blockVolumeRatio, 1),
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
    dcaMaxExposureX: num(cts.dcaMaxExposureX, 2),
    dcaMinPf: num(cts.dcaMinPf, GATE_MIN_PF_DEFAULT),
    dcaPfWindow: num(cts.dcaPfWindow ?? cts.pfWindow, 15),
    dcaDeactN: num(cts.dcaDeactN, 25),
    volumeFactor: num(cts.volumeFactor, 0.05),
    axisPrevEnabled: bool(cts.axisPrevEnabled ?? nestedAxis(coord, "prev", "enabled"), false),
    axisPrevMaxWindow: num(cts.axisPrevMaxWindow ?? nestedAxis(coord, "prev", "maxWindow"), 12),
    axisLastEnabled: bool(cts.axisLastEnabled ?? nestedAxis(coord, "last", "enabled"), false),
    axisLastMaxWindow: num(cts.axisLastMaxWindow ?? nestedAxis(coord, "last", "maxWindow"), 4),
    axisContEnabled: bool(cts.axisContEnabled ?? nestedAxis(coord, "cont", "enabled"), false),
    axisContMaxWindow: num(cts.axisContMaxWindow ?? nestedAxis(coord, "cont", "maxWindow"), 8),
    axisPauseEnabled: bool(cts.axisPauseEnabled ?? nestedAxis(coord, "pause", "enabled"), false),
    axisPauseMaxWindow: num(cts.axisPauseMaxWindow ?? nestedAxis(coord, "pause", "maxWindow"), 8),
    minPf: num((cts.strategies as { main?: { real?: { min_profit_factor?: number } } } | undefined)?.main?.real?.min_profit_factor ?? cts.realProfitFactor, GATE_MIN_PF_DEFAULT),
    positionCostPct: num(cts.exchangePositionCost ?? cts.positionCost, 0.12),
    windowsLinked: bool(cts.windowsLinked, true),
    pfWindow: Math.max(8, num(cts.pfWindow, 15)),
    slMinPct: num(cts.slMinPct, 0.2),
    slMaxPct: num(cts.slMaxPct, 15.75),
    tpMinPct: num(cts.tpMinPct, 0.35),
    tpMaxPct: num(cts.tpMaxPct, 4.5),
    tpCostRatio: num(cts.tpCostRatio, 5),
    slToTpRatio: snapSlToTp(num(cts.slToTpRatio, 0.6)),
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
    stratGeneral: bool(cts.stratGeneral, true),
    stratShortRange: bool(cts.stratShortRange, false),
    stratTrend: bool(cts.stratTrend, false),
    stratBreakout: bool(cts.stratBreakout, false),
    stratReversion: bool(cts.stratReversion, false),
    stratFlow: bool(cts.stratFlow, false),
    stratMulti: bool(cts.stratMulti, false),
    shortRangeStepMin: num(cts.shortRangeStepMin, 2),
    shortRangeStepMax: num(cts.shortRangeStepMax, 6),
    shortRangeMinPf: num(cts.shortRangeMinPf, 1.25),
    slMinSteps: num(cts.slMinSteps, 1),
    trailMinSteps: num(cts.trailMinSteps, 2),
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
    histLookbackBars: num(cts.histLookbackBars, 1920),
    histMinBars: num(cts.histMinBars, 120),
    histWarmup: num(cts.histWarmup, 30),
    histRefreshS: num(cts.histRefreshS, 90),
    setPfWindow: num(cts.setPfWindow ?? cts.pfWindow, 15),
    setDeactN: num(cts.setDeactN, 25),
    setMinPf: num(cts.setMinPf ?? cts.realProfitFactor, GATE_MIN_PF_DEFAULT),
    setMaxDdtHours: num(cts.setMaxDdtHours, 12),
    setGateWindow: Math.max(10, Math.min(200, Math.round(num(cts.setGateWindow, 50)))),
    setGateMinTrades: Math.max(8, Math.min(200, Math.round(num(cts.setGateMinTrades, 30)))),
    stagesEnabled: bool(cts.stagesEnabled, true),
    setGateShortN: Math.max(0, Math.min(200, Math.round(num(cts.setGateShortN, 15)))),
    mainGateEnabled: bool(cts.mainGateEnabled, true),
    mainGateN: Math.max(8, Math.min(200, Math.round(num(cts.mainGateN, 25)))),
    mainAutoEnabled: bool(cts.mainAutoEnabled, true),
    mainAutoWindows: windows(cts.mainAutoWindows, [5, 10, 15]),
    mainAutoMin: Math.max(3, Math.min(200, Math.round(num(cts.mainAutoMin, 8)))),
    mainAutoMargin: Math.max(1, Math.min(2, num(cts.mainAutoMargin, 1.02))),
    mainNegEnabled: bool(cts.mainNegEnabled, true),
    mainNegWindows: windows(cts.mainNegWindows, [5, 10]),
    realGateEnabled: bool(cts.realGateEnabled, true),
    realGateN: Math.max(3, Math.min(200, Math.round(num(cts.realGateN, 15)))),
    realStagePaper: bool(cts.realStagePaper, true),
    minPfLinked: bool(cts.minPfLinked, true),
    histLookbackAuto: bool(cts.histLookbackAuto, true),
    histLookbackMax: Math.max(120, Math.min(2880, Math.round(num(cts.histLookbackMax, 2880)))),
    setUseHistoricGate: bool(cts.setUseHistoricGate, true),
    holdEnabled: bool(cts.holdEnabled, true),
    holdAuto: bool(cts.holdAuto, true),
    holdAutoMax: Math.max(10, Math.min(50, Math.round(num(cts.holdAutoMax, 50)))),
    holdAutoMin: Math.max(3, Math.min(50, Math.round(num(cts.holdAutoMin, 10)))),
    stepHoldOn: Array.isArray(cts.stepHoldOn) ? cts.stepHoldOn.slice(0, 12).map(Boolean) : DEFAULT_OVERLAY.stepHoldOn,
    stepHoldMin: Array.isArray(cts.stepHoldMin) ? cts.stepHoldMin.slice(0, 12).map((x) => Math.round(Number(x) || 0)) : DEFAULT_OVERLAY.stepHoldMin,
    setMinSamples: num(cts.setMinSamples, 12),
    setMinStep: num(cts.setMinStep ?? cts.minStepRange, 2),
    setStepMax: num(cts.setStepMax, 30),
    setStepAdapt: bool(cts.setStepAdapt, true),
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
    exitPfWindow: num(cts.exitPfWindow, 15),
    exitDeactN: num(cts.exitDeactN, 25),
    exitMinPf: num(cts.exitMinPf, GATE_MIN_PF_DEFAULT),
    exitAutoDeact: bool(cts.exitAutoDeact, true),
    rearrange: bool(cts.rearrange, true),
    rearrangeGap: num(cts.rearrangeGap, 0.22),
    ...live,
  };
  out.slToTpRatio = snapSlToTp(num(out.slToTpRatio, 0.6));
  out.modules = {
    ...(DEFAULT_OVERLAY.modules ?? {}),
    ...(typeof live?.modules === "object" && live.modules ? live.modules : {}),
  };
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
