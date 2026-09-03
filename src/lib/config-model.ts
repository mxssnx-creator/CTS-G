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
export const SL_TP_MIN = 0.2;
export const SL_TP_MAX = 2.6;
export const SL_TP_STEP = 0.2;

export function slTpGrid(lo = SL_TP_MIN, hi = SL_TP_MAX, step = SL_TP_STEP): number[] {
  const a = Number.isFinite(lo) ? lo : SL_TP_MIN;
  const b = Number.isFinite(hi) ? hi : SL_TP_MAX;
  const s = Number.isFinite(step) && step > 0 ? step : SL_TP_STEP;
  const min = Math.max(SL_TP_MIN, Math.min(a, b));
  const max = Math.min(SL_TP_MAX, Math.max(a, b));
  const out: number[] = [];
  for (let x = min; x <= max + 1e-9 && out.length < 64; x = Math.round((x + s) * 10) / 10) {
    out.push(Math.round(x * 10) / 10);
  }
  return out.length ? out : [0.6];
}

export const SL_TP_RATIOS = slTpGrid() as readonly number[];
export const TRAIL_VARIANTS = ["0.3:0.1", "0.6:0.2", "0.9:0.3", "1.2:0.4", "1.5:0.5"] as const;
export const TRAIL_ARMS = [0.3, 0.6, 0.9, 1.2, 1.5] as const;
export const TRAIL_GIVES = [0.1, 0.2, 0.3, 0.4, 0.5] as const;

export function trailGrid(): { key: string; arm: number; give: number }[] {
  const out: { key: string; arm: number; give: number }[] = [];
  for (const arm of TRAIL_ARMS) {
    for (const give of TRAIL_GIVES) {
      out.push({ key: `${arm.toFixed(1)}:${give.toFixed(1)}`, arm, give });
    }
  }
  return out;
}

export function snapSlToTp(v: number, lo = SL_TP_MIN, hi = SL_TP_MAX, step = SL_TP_STEP): number {
  const grid = slTpGrid(lo, hi, step);
  const x = Number.isFinite(v) ? v : 0.6;
  let best = grid[0] ?? 0.6;
  let dist = Math.abs(x - best);
  for (const g of grid) {
    const d = Math.abs(x - g);
    if (d < dist) {
      best = g;
      dist = d;
    }
  }
  return best;
}

export function trailGiveFromArm(arm: number, factor = 1 / 3, gmin = 0.1, gmax = 0.5): number {
  const g = arm * factor;
  return Math.round(Math.min(gmax, Math.max(gmin, g)) * 100) / 100;
}

export type PulseOverlay = {
  targetNotional: number;
  volumeFactor: number;
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
  maxDdTimeS: number;
  scratchS: number;
  scratchMinPct: number;
  scanS: number;
  cooldownS: number;
  staggerS: number;
  controlOrders: boolean;
  blockEnabled: boolean;
  blockMaxStack: number;
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
  baseMinPf: number;
  mainMinPf: number;
  realMinPf: number;
  positionCostPct: number;
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
  slToTpMin: number;
  slToTpMax: number;
  slToTpStep: number;
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
  stratDca: boolean;
  indTypeState: boolean;
  indTypeDirection: boolean;
  indTypeMove: boolean;
  indTypeActive: boolean;
  indTypeCommon: boolean;
  indTypeSignals: boolean;
  indTypeTrend: boolean;
  indTypeBreak: boolean;
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
  setMaxDdTimeS: number;
  setAutoDeact: boolean;
  setUseHistoricGate: boolean;
  setStrictGate: boolean;
  setMinSamples: number;
  setReactivate: boolean;
  setMaxActive: number;
  setMinStep: number;
  setStepMax: number;
  setStepAdapt: boolean;
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
  volumeFactor: 1,
  leverage: 150,
  useMaxLeverage: true,
  maxOpen: 0,
  maxPerGroup: 0,
  symbolsAll: true,
  symbolsDynamic: true,
  symbolSort: "vol1h",
  symbolCap: 0,
  slPct: 0.48,
  tpPct: 0.75,
  trailArmPct: 0.3,
  trailGivePct: 0.1,
  timeStopS: 21600,
  maxDdTimeS: 0,
  scratchS: 600,
  scratchMinPct: 0.16,
  scanS: 0.2,
  cooldownS: 9,
  staggerS: 0.6,
  controlOrders: true,
  blockEnabled: true,
  blockMaxStack: 3,
  blockVolumeRatio: 1,
  blockProfitFactorRatio: 1.25,
  blockPauseCountRatio: 1,
  blockActiveLive: true,
  blockActiveReal: true,
  dcaEnabled: false,
  dcaMaxSteps: 4,
  dcaCooldownSeconds: 30,
  dcaBreakevenProfitPct: 0.2,
  dcaTakeProfitMode: "average",
  dcaStepDistancesPct: [0.5, 1, 1.5, 2],
  dcaStepVolumeMultipliers: [1.5, 2, 2.3, 2.5],
  dcaAutoDeact: true,
  dcaMinPf: 1.25,
  dcaPfWindow: 15,
  dcaDeactN: 25,
  symbols: ["*"],
  axisPrevEnabled: true,
  axisPrevMaxWindow: 12,
  axisLastEnabled: true,
  axisLastMaxWindow: 4,
  axisContEnabled: true,
  axisContMaxWindow: 8,
  axisPauseEnabled: true,
  axisPauseMaxWindow: 8,
  minPf: 1.15,
  baseMinPf: 1.25,
  mainMinPf: 1.25,
  realMinPf: 1.25,
  positionCostPct: 0.15,
  pfWindow: 15,
  slMinPct: 0.2,
  slMaxPct: 1.2,
  tpMinPct: 0.35,
  tpMaxPct: 2.4,
  tpCostRatio: 5,
  slToTpRatio: 0.6,
  slToTpAuto: true,
  slToTpRecalcN: 6,
  slToTpRecalcEvery: 8,
  slToTpMin: 0.2,
  slToTpMax: 2.6,
  slToTpStep: 0.2,
  trailAuto: true,
  trailRecalcN: 6,
  trailRecalcEvery: 8,
  trailArmMin: 0.3,
  trailArmMax: 1.5,
  trailGiveMin: 0.1,
  trailGiveMax: 0.5,
  trailGiveFactor: 0.333,
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
  stratDca: false,
  indTypeState: true,
  indTypeDirection: true,
  indTypeMove: true,
  indTypeActive: true,
  indTypeCommon: true,
  indTypeSignals: true,
  indTypeTrend: true,
  indTypeBreak: true,
  noise: 0.05,
  volWeight: 0.3,
  minStep: 3,
  maxStopLossRatio: 2.5,
  trailingMinStep: 3,
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
  histLookbackBars: 480,
  histMinBars: 120,
  histWarmup: 30,
  histRefreshS: 90,
  setPfWindow: 15,
  setDeactN: 25,
  setMinPf: 1.15,
  setMaxDdTimeS: 1800,
  setAutoDeact: true,
  setUseHistoricGate: true,
  setStrictGate: true,
  setMinSamples: 8,
  setReactivate: true,
  setMaxActive: 0,
  setMinStep: 3,
  setStepMax: 22,
  setStepAdapt: true,
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
  exitPfWindow: 15,
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
  slToTpMin?: number;
  slToTpMax?: number;
  slToTpStep?: number;
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
  stratDca?: boolean;
  indTypeState?: boolean;
  indTypeDirection?: boolean;
  indTypeMove?: boolean;
  indTypeActive?: boolean;
  indTypeCommon?: boolean;
  indTypeSignals?: boolean;
  indTypeTrend?: boolean;
  indTypeBreak?: boolean;
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
  setMaxDdTimeS?: number;
  setAutoDeact?: boolean;
  setUseHistoricGate?: boolean;
  setStrictGate?: boolean;
  setMinSamples?: number;
  setReactivate?: boolean;
  setMaxActive?: number;
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
    blockMaxStack: num(cts.blockMaxStack ?? coord.blockMaxStack, 3),
    blockVolumeRatio: num(cts.blockVolumeRatio ?? coord.blockVolumeRatio, 1),
    blockProfitFactorRatio: num(cts.blockProfitFactorRatio ?? coord.blockProfitFactorRatio, 1.1),
    blockPauseCountRatio: num(cts.blockPauseCountRatio ?? coord.blockPauseCountRatio, 1),
    blockActiveLive: bool(cts.blockActiveLiveEnabled ?? coord.blockActiveLiveEnabled, true),
    blockActiveReal: bool(cts.blockActiveRealEnabled ?? coord.blockActiveRealEnabled, true),
    dcaEnabled: bool(live?.dcaEnabled ?? cts.dcaEnabled ?? cts.variantDcaEnabled ?? cts.variant_dca, false),
    dcaMaxSteps: num(cts.dcaMaxSteps ?? coord.dcaMaxSteps, 4),
    dcaCooldownSeconds: num(cts.dcaCooldownSeconds ?? coord.dcaCooldownSeconds, 30),
    dcaBreakevenProfitPct: num(cts.dcaBreakevenProfitPct ?? coord.dcaBreakevenProfitPct, 0.2),
    dcaTakeProfitMode: String(cts.dcaTakeProfitMode ?? coord.dcaTakeProfitMode ?? "average"),
    dcaStepDistancesPct: arr<number>(cts.dcaStepDistancesPct ?? coord.dcaStepDistancesPct, [0.5, 1, 1.5, 2]),
    dcaStepVolumeMultipliers: arr<number>(cts.dcaStepVolumeMultipliers ?? coord.dcaStepVolumeMultipliers, [1.5, 2, 2.3, 2.5]),
    dcaAutoDeact: bool(cts.dcaAutoDeact, true),
    dcaMinPf: num(cts.dcaMinPf, 1.25),
    dcaPfWindow: num(cts.dcaPfWindow ?? cts.pfWindow, 15),
    dcaDeactN: num(cts.dcaDeactN, 25),
    volumeFactor: num(cts.volumeFactor, 1),
    axisPrevEnabled: bool(cts.axisPrevEnabled ?? nestedAxis(coord, "prev", "enabled"), true),
    axisPrevMaxWindow: num(cts.axisPrevMaxWindow ?? nestedAxis(coord, "prev", "maxWindow"), 12),
    axisLastEnabled: bool(cts.axisLastEnabled ?? nestedAxis(coord, "last", "enabled"), true),
    axisLastMaxWindow: num(cts.axisLastMaxWindow ?? nestedAxis(coord, "last", "maxWindow"), 4),
    axisContEnabled: bool(cts.axisContEnabled ?? nestedAxis(coord, "cont", "enabled"), true),
    axisContMaxWindow: num(cts.axisContMaxWindow ?? nestedAxis(coord, "cont", "maxWindow"), 8),
    axisPauseEnabled: bool(cts.axisPauseEnabled ?? nestedAxis(coord, "pause", "enabled"), true),
    axisPauseMaxWindow: num(cts.axisPauseMaxWindow ?? nestedAxis(coord, "pause", "maxWindow"), 8),
    minPf: num((cts.strategies as { main?: { real?: { min_profit_factor?: number } } } | undefined)?.main?.real?.min_profit_factor ?? cts.realProfitFactor, 1.25),
    baseMinPf: num((cts.strategies as { main?: { base?: { min_profit_factor?: number } } } | undefined)?.main?.base?.min_profit_factor, 1.25),
    mainMinPf: num((cts.strategies as { main?: { main?: { min_profit_factor?: number } } } | undefined)?.main?.main?.min_profit_factor, 1.25),
    realMinPf: num((cts.strategies as { main?: { real?: { min_profit_factor?: number } } } | undefined)?.main?.real?.min_profit_factor ?? cts.realProfitFactor, 1.25),
    positionCostPct: num(cts.exchangePositionCost ?? cts.positionCost, 0.15),
    pfWindow: num(cts.pfWindow, 15),
    slMinPct: num(cts.slMinPct, 0.2),
    slMaxPct: num(cts.slMaxPct, 1.2),
    tpMinPct: num(cts.tpMinPct, 0.35),
    tpMaxPct: num(cts.tpMaxPct, 2.4),
    tpCostRatio: num(cts.tpCostRatio, 5),
    slToTpRatio: snapSlToTp(num(cts.slToTpRatio, 0.6)),
    slToTpAuto: bool(cts.slToTpAuto, true),
    slToTpRecalcN: num(cts.slToTpRecalcN, 6),
    slToTpRecalcEvery: num(cts.slToTpRecalcEvery, 8),
    slToTpMin: num(cts.slToTpMin, 0.2),
    slToTpMax: num(cts.slToTpMax, 2.6),
    slToTpStep: num(cts.slToTpStep, 0.2),
    trailAuto: bool(cts.trailAuto, true),
    trailRecalcN: num(cts.trailRecalcN, 6),
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
    stratDca: bool(cts.stratDca ?? cts.dcaEnabled, false),
    indTypeState: bool(cts.indTypeState, true),
    indTypeDirection: bool(cts.indTypeDirection, true),
    indTypeMove: bool(cts.indTypeMove, true),
    indTypeActive: bool(cts.indTypeActive, true),
    indTypeCommon: bool(cts.indTypeCommon, true),
    indTypeSignals: bool(cts.indTypeSignals, true),
    indTypeTrend: bool(cts.indTypeTrend, true),
    indTypeBreak: bool(cts.indTypeBreak, true),
    noise: num(cts.activeNoiseFilter, 0.05),
    volWeight: num(cts.activeVolatilityWeight, 0.3),
    minStep: num((coord as { minStep?: number }).minStep, 3),
    maxStopLossRatio: num((coord as { maxStopLossRatio?: number }).maxStopLossRatio, 2.5),
    trailingMinStep: num((coord as { trailingMinStep?: number }).trailingMinStep, 3),
    posCountsVolumeRatio: num(cts.posCountsVolumeRatio ?? (coord as { posCountsVolumeRatio?: number }).posCountsVolumeRatio, 0.05),
    indRewardRisk: num(cts.indRewardRisk, 1.8),
    indExtraSources: bool(cts.indExtraSources, true),
    histEnabled: bool(cts.histEnabled, true),
    histLookbackBars: num(cts.histLookbackBars, 480),
    histMinBars: num(cts.histMinBars, 120),
    histWarmup: num(cts.histWarmup, 30),
    histRefreshS: num(cts.histRefreshS, 90),
    setPfWindow: num(cts.setPfWindow ?? cts.pfWindow, 15),
    setDeactN: num(cts.setDeactN, 25),
    setMinPf: num(cts.setMinPf ?? cts.realProfitFactor, 1.15),
    setMaxDdTimeS: num(cts.setMaxDdTimeS, 1800),
    setAutoDeact: bool(cts.setAutoDeact, true),
    setUseHistoricGate: bool(cts.setUseHistoricGate, true),
    setStrictGate: bool(cts.setStrictGate, true),
    setMinSamples: num(cts.setMinSamples, 8),
    setReactivate: bool(cts.setReactivate, true),
    setMaxActive: num(cts.setMaxActive, 0),
    setMinStep: num(cts.setMinStep ?? cts.minStepRange, 3),
    setStepMax: num(cts.setStepMax, 22),
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
    exitMinPf: num(cts.exitMinPf, 1.25),
    exitAutoDeact: bool(cts.exitAutoDeact, true),
    rearrange: bool(cts.rearrange, true),
    rearrangeGap: num(cts.rearrangeGap, 0.22),
    ...live,
  };
  out.slToTpMin = 0.2;
  out.slToTpMax = 2.6;
  out.slToTpStep = 0.2;
  out.slToTpRatio = snapSlToTp(num(out.slToTpRatio, 0.6), 0.2, 2.6, 0.2);
  out.trailArmMin = 0.3;
  out.trailArmMax = 1.5;
  out.trailGiveMin = 0.1;
  out.trailGiveMax = 0.5;
  out.setMinStep = Math.max(3, Math.min(22, Math.round(num(out.setMinStep, 3))));
  out.setStepMax = Math.max(out.setMinStep, Math.min(22, Math.round(num(out.setStepMax, 22))));
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
  m["strategy.dca"] = Boolean(next.dcaEnabled) && next.stratDca !== false;
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
