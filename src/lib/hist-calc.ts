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
  evaluationWindows?: Record<string, EvaluationWindow>;
  bySide?: Record<string, HistCalcKind>;
};

export type EvaluationWindow = {
  requestedN?: number;
  n?: number;
  available?: boolean;
  requiredSamples?: number;
  validated?: boolean;
  pf?: number;
  classicPf?: number;
  avgR?: number;
  netAvg?: number;
  netPct?: number;
  costPct?: number;
  costSamples?: number;
  costSubtracted?: boolean;
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
  evaluationWindows?: Record<string, EvaluationWindow>;
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
  evaluationWindows?: Record<string, EvaluationWindow>;
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
  evaluationWindows?: Record<string, EvaluationWindow>;
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
  evaluationWindows?: Record<string, EvaluationWindow>;
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
  symbolCap?: number;
  indTypeSignals: boolean;
  indTypeState: boolean;
  indTypeDirection: boolean;
  indTypeMove: boolean;
  indTypeActive: boolean;
  indTypeCommon: boolean;
  indTypeTrend: boolean;
  indTypeBreak: boolean;
  /** Prefer the smallest stable ranges after PF/DD/sample gates. */
  preferMinimalRange: boolean;
  /** Evaluate the independent 50+ close coordination window. */
  additionalCoordination: boolean;
  /** @deprecated accepted when reading older persisted jobs. */
  preferMinimalPositive?: boolean;
  minimalPositiveCoordination?: boolean;
  coordOptimizationN: number;
};

export type ForcedConfigRow = {
  id: string; symbol: string; indication: string; direction: string;
  tpPct: number; slPct: number; slRatio: number; rank?: number;
  n: number; trainN: number; holdoutN: number; pf: number; trainPf: number; holdoutPf: number;
  costRatio: number; netPct: number; maxDrawdownR: number; tradesPerHour: number;
  avgHoldS: number; eligible: boolean; status: string; source: string; settingsKey: string;
  evaluationWindows?: Record<string, EvaluationWindow>;
  liveN?: number; livePf?: number; liveStatus?: string; liveEnabled?: boolean;
  measuredCosts?: boolean; openUnresolved?: number;
};

export type ForcedConfigSummary = {
  rows?: ForcedConfigRow[]; symbols?: string[]; completed?: number; requested?: number;
  coveragePct?: number; selectedCount?: number; eligibleCount?: number;
  minPf?: number; updatedAt?: number; baselineOnly?: boolean; mainnetReady?: boolean;
  connection?: string; trialMode?: boolean;
  controlMinTrades?: number; controlMinPf?: number; trainingMinTrades?: number;
};

export type HistCalcTimings = {
  fetchMs?: number;
  fetchWaitMs?: number;
  fetchRequests?: number;
  signalMs?: number;
  signalWallMs?: number;
  replayMs?: number;
  replayWallMs?: number;
  mergeMs?: number;
  scoreMs?: number;
  reportMs?: number;
  totalMs?: number;
};

export type HistCalcTaskStatus = {
  requested?: number;
  submitted?: number;
  completed?: number;
  inFlight?: number;
  workers?: number;
  tileSize?: number;
  queueLimit?: number;
};

export type HistCalcJob = {
  forcedConfigs?: ForcedConfigSummary;
  ok?: boolean;
  phase: string;
  pct: number;
  detail: string;
  hours?: number;
  lookback?: number;
  evaluationBars?: number;
  warmupBars?: number;
  requestedBars?: number;
  requestedStart?: number;
  requestedEnd?: number;
  evaluationStart?: number;
  evaluationEnd?: number;
  fetchStart?: number;
  fetchEnd?: number;
  symbols?: string[];
  options?: HistCalcOptions;
  coverage?: {
    product?: number;
    setCount?: number;
    activeCount?: number;
    validatedCount?: number;
    histFills?: number;
    symbols?: { requested?: number; valid?: number; completed?: number; failed?: number; gapped?: number; invalid?: number; coveragePct?: number };
    bars?: { requested?: number; completed?: number; missing?: number; gapped?: number; coveragePct?: number };
    evaluationBars?: { requested?: number; completed?: number; coveragePct?: number };
    sets?: { requested?: number; completed?: number; coveragePct?: number };
    evaluations?: { requested?: number; completed?: number; coveragePct?: number };
    tasks?: { requested?: number; completed?: number; coveragePct?: number };
    gaps?: Array<{ symbol?: string; start?: number; end?: number; minutes?: number; error?: string }>;
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
  evaluationWindows?: {
    windows?: number[];
    directions?: Record<string, Record<string, EvaluationWindow>>;
    strategies?: Record<string, Record<string, EvaluationWindow>>;
    indications?: Record<string, Record<string, EvaluationWindow>>;
    symbols?: Record<string, Record<string, EvaluationWindow>>;
  };
  winner?: HistCalcRow | null;
  apply?: Record<string, unknown>;
  presets?: Array<{ id: string; name: string; hint: string }>;
  error?: string;
  elapsedMs?: number;
  timings?: HistCalcTimings;
  replayTasks?: HistCalcTaskStatus;
  replayTiles?: HistCalcTaskStatus;
  replayFailure?: { symbol?: string; kind?: string; tile?: number; error?: string };
  source?: string;
  connection?: string;
  runId?: string;
  generation?: number;
  mode?: "idle" | "initial" | "hourly" | "manual" | "gap" | string;
  selectedSymbols?: string[];
  validSymbols?: string[];
  invalidSymbols?: Array<{ symbol?: string; reason?: string }>;
  missingSymbols?: string[];
  gappedSymbols?: string[];
  watermark?: Record<string, number>;
  lastPublishedWatermark?: Record<string, number>;
  lastCompleteRun?: number;
  nextRunAt?: number;
  stale?: boolean;
  deferredReason?: string;
  coordinationComplete?: boolean;
  requestOptions?: HistCalcOptions;
  requestOverlay?: Record<string, unknown>;
  shared?: boolean;
  independent?: boolean;
  ready?: boolean;
  async?: boolean;
  partial?: boolean;
  running?: boolean;
  continuous?: boolean;
  refreshS?: number;
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
  hours: 48,
  minStep: 1,
  stepMax: 30,
  trailing: true,
  stratBlock: true,
  stratDca: false,
  stratIndications: true,
  stratGeneral: true,
  allConfigs: true,
  allSymbols: true,
  symbolCap: 50,
  indTypeSignals: true,
  indTypeState: true,
  indTypeDirection: true,
  indTypeMove: true,
  indTypeActive: true,
  indTypeCommon: true,
  indTypeTrend: true,
  indTypeBreak: true,
  preferMinimalRange: false,
  additionalCoordination: false,
  coordOptimizationN: 150,
};

export const CALC_RUNNING_PHASES = [
  "initial",
  "hourly",
  "backfill",
  "fetch",
  "replay",
  "score",
  "score-refresh",
  "gap",
  "incremental",
  "queued",
  "partial",
  "deferred",
  "paused",
] as const;

const RUNNING_PHASES = new Set<string>(CALC_RUNNING_PHASES);

export function calcIsRunning(phase?: string | null): boolean {
  return Boolean(phase && RUNNING_PHASES.has(phase));
}

export function hasCalcSnapshot(job: HistCalcJob | null | undefined): boolean {
  if (!job) return false;
  return Boolean(
    (job.phase && job.phase !== "idle") ||
      job.ready ||
      job.lastCompleteRun ||
      (job.rows?.length ?? 0) > 0 ||
      job.winner,
  );
}

export function calcPollMs(job: HistCalcJob | null | undefined, hidden = false): number {
  if (hidden) return 8000;
  if (calcIsRunning(job?.phase)) return 1200;
  if (job?.stale || job?.ready || job?.phase === "ready" || (job?.nextRunAt && job.nextRunAt * 1000 > Date.now())) {
    return 4000;
  }
  return 8000;
}

export function calcStatusLine(job: HistCalcJob | null | undefined, hours = 48): string {
  if (!job || !job.phase || job.phase === "idle") {
    return `Ready · ${hours}h tape · start a continuous replay`;
  }
  const pct = Math.round(job.pct || 0);
  const detail = String(job.detail || "").trim();
  const head = `${job.phase} ${pct}%`;
  if (calcIsRunning(job.phase)) {
    return detail ? `${head} · ${detail}` : head;
  }
  if (job.nextRunAt) {
    const when = new Date(job.nextRunAt * 1000).toLocaleTimeString();
    return detail ? `${head} · next refresh ${when} · ${detail}` : `${head} · next refresh ${when}`;
  }
  return detail ? `${head} · ${detail}` : head;
}

export function calcStartLabel(job: HistCalcJob | null | undefined): string {
  if (calcIsRunning(job?.phase)) return "Replaying…";
  if (hasCalcSnapshot(job)) return "Refresh now";
  return "Start continuous replay";
}

export async function fetchHistCalc(connection?: string, signal?: AbortSignal): Promise<HistCalcJob> {
  try {
    const query = connection ? `?conn=${encodeURIComponent(connection)}` : "";
    const r = await fetch(`/hist-calc.json${query}`, { cache: "no-store", signal });
    if (!r.ok) return { phase: "idle", pct: 0, detail: `status ${r.status}` };
    return (await r.json()) as HistCalcJob;
  } catch (e) {
    if (signal?.aborted) return { phase: "idle", pct: 0, detail: "aborted" };
    return { phase: "error", pct: 0, detail: String(e), error: String(e) };
  }
}

export async function startHistCalc(
  body: Partial<HistCalcOptions> & {
    symbols?: string[];
    selectedSymbols?: string[];
    forcedOnly?: boolean;
    connection?: string;
    overlay?: Record<string, unknown>;
    continuous?: boolean;
    mode?: string;
    refreshS?: number;
  },
): Promise<HistCalcJob> {
  try {
    const legacy = body as Partial<HistCalcOptions> & {
      preferMinimalPositive?: boolean;
      minimalPositiveCoordination?: boolean;
    };
    const continuous = body.continuous !== false && !body.forcedOnly;
    const migrated = {
      ...body,
      preferMinimalRange: body.preferMinimalRange ?? legacy.preferMinimalPositive,
      additionalCoordination: body.additionalCoordination ?? legacy.minimalPositiveCoordination,
      continuous,
      mode: body.mode || (continuous ? "hourly" : "manual"),
    };
    const query = body.connection ? `?conn=${encodeURIComponent(body.connection)}` : "";
    const r = await fetch(`/hist-calc.json${query}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...DEFAULT_CALC_OPTIONS, ...migrated, allConfigs: true }),
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

export async function stopHistCalc(connection?: string): Promise<HistCalcJob> {
  try {
    const query = connection ? `?conn=${encodeURIComponent(connection)}` : "";
    const r = await fetch(`/hist-calc.json${query}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "stop" }),
    });
    const j = (await r.json().catch(() => ({}))) as HistCalcJob;
    if (!r.ok) {
      return { phase: "error", pct: 0, detail: j.detail || `stop rejected ${r.status}`, error: j.error };
    }
    return j;
  } catch (e) {
    return { phase: "error", pct: 0, detail: String(e), error: String(e) };
  }
}
