export type HistCalcKind = {
  kind: string;
  n?: number;
  pf?: number;
  validated?: boolean;
  profitable?: boolean;
  maxDdS?: number;
  avgDdS?: number;
  ok?: boolean;
  side?: string;
  netAvg?: number;
  costSubtracted?: boolean;
  bySide?: Record<string, HistCalcKind>;
};

export type HistCalcRow = {
  id: string;
  kind: string;
  pack: string;
  direction?: string;
  slRatio: number;
  trailKey?: string;
  trailArm?: number;
  trailGive?: number;
  step: number;
  n: number;
  wr: number;
  last15Ratio: number;
  last15N: number;
  maxDdS: number;
  avgDdS?: number;
  expectancy?: number;
  netAvg?: number;
  active?: boolean;
  validated?: boolean;
  lowSl?: boolean;
  deactReason?: string;
  costSubtracted?: boolean;
  bySide?: Record<string, { n: number; pf: number; validated?: boolean; maxDdS?: number }>;
};

export type HistCalcSymbol = {
  symbol: string;
  n: number;
  pf: number;
  maxDdS: number;
  wr: number;
  validated?: boolean;
  netAvg?: number;
  costSubtracted?: boolean;
  bySide?: Record<string, { n: number; pf: number; validated?: boolean }>;
};

export type HistCalcDirection = {
  direction: string;
  n: number;
  pf: number;
  maxDdS: number;
  wr: number;
  validated?: boolean;
  netAvg?: number;
  costSubtracted?: boolean;
};

export type HistCalcStrategy = {
  strategy: string;
  n: number;
  pf: number;
  maxDdS?: number;
  wr?: number;
  validated?: boolean;
  netAvg?: number;
  costSubtracted?: boolean;
  bySide?: Record<string, { n: number; pf: number; validated?: boolean; netAvg?: number }>;
};

export type HistCalcOptions = {
  hours: number;
  minStep: number;
  stepMax: number;
  trailing: boolean;
  stratBlock: boolean;
  stratDca: boolean;
  stratIndications: boolean;
  stratGeneral: boolean;
  allConfigs: boolean;
  allSymbols: boolean;
  indTypeSignals: boolean;
  indTypeState: boolean;
  indTypeDirection: boolean;
  indTypeMove: boolean;
  indTypeActive: boolean;
  indTypeCommon: boolean;
};

export type HistCalcJob = {
  ok?: boolean;
  phase: string;
  pct: number;
  detail: string;
  hours?: number;
  lookback?: number;
  symbols?: string[];
  options?: HistCalcOptions;
  coverage?: {
    product?: number;
    dims?: Record<string, number>;
    families?: { base?: number; trail?: number };
    slTpCover?: boolean;
    trailSlTpCover?: boolean;
    independentConfigs?: boolean;
  };
  rows?: HistCalcRow[];
  rowCount?: number;
  validatedCount?: number;
  bySymbol?: HistCalcSymbol[];
  byDirection?: Record<string, HistCalcDirection>;
  byStrategy?: Record<string, HistCalcStrategy>;
  kinds?: Record<string, HistCalcKind>;
  winner?: HistCalcRow | null;
  apply?: Record<string, unknown>;
  presets?: Array<{ id: string; name: string; hint: string }>;
  error?: string;
  elapsedMs?: number;
  source?: string;
  independent?: boolean;
  ready?: boolean;
  async?: boolean;
  partial?: boolean;
  workers?: number;
  barsHeld?: number;
  independence?: {
    symbol?: boolean;
    direction?: boolean;
    indication?: boolean;
    strategy?: boolean;
    config?: boolean;
    costSubtracted?: boolean;
    async?: boolean;
    partial?: boolean;
  };
};

export const DEFAULT_CALC_OPTIONS: HistCalcOptions = {
  hours: 20,
  minStep: 2,
  stepMax: 4,
  trailing: true,
  stratBlock: true,
  stratDca: false,
  stratIndications: true,
  stratGeneral: true,
  allConfigs: true,
  allSymbols: true,
  indTypeSignals: true,
  indTypeState: true,
  indTypeDirection: true,
  indTypeMove: true,
  indTypeActive: true,
  indTypeCommon: true,
};

export async function fetchHistCalc(): Promise<HistCalcJob> {
  try {
    const r = await fetch("/hist-calc.json", { cache: "no-store" });
    if (!r.ok) return { phase: "idle", pct: 0, detail: `status ${r.status}` };
    return (await r.json()) as HistCalcJob;
  } catch (e) {
    return { phase: "error", pct: 0, detail: String(e), error: String(e) };
  }
}

export async function startHistCalc(
  body: Partial<HistCalcOptions> & { symbols?: string[] },
): Promise<HistCalcJob> {
  try {
    const r = await fetch("/hist-calc.json", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...DEFAULT_CALC_OPTIONS, ...body, allConfigs: true }),
    });
    const j = (await r.json().catch(() => ({}))) as HistCalcJob;
    if (!r.ok) {
      return { phase: "error", pct: 0, detail: j.detail || `rejected ${r.status}`, error: j.error };
    }
    return j;
  } catch (e) {
    return { phase: "error", pct: 0, detail: String(e), error: String(e) };
  }
}
