import { IND_TYPE_KEYS, type IndicationKind } from "./indication-kinds.ts";

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
  /** Prefer the smallest stable ranges after PF/DD/sample gates. */
  preferMinimalRange: boolean;
  /** Evaluate the independent 50+ close coordination window. */
  additionalCoordination: boolean;
  /** @deprecated accepted when reading older persisted jobs. */
  preferMinimalPositive?: boolean;
  minimalPositiveCoordination?: boolean;
  coordOptimizationN: number;
} & IndTypeFlags;

/** One historic calc toggle per indication kind (`indTypeState`, ...). */
export type IndTypeFlags = { [K in IndicationKind as `indType${Capitalize<K>}`]: boolean };

export type ForcedConfigRow = {
  evidenceVersion?: number;
  id: string; symbol: string; indication: string; direction: string;
  tpPct: number; slPct: number; slRatio: number; rank?: number;
  n: number; trainN: number; holdoutN: number; pf: number | null; trainPf: number | null; holdoutPf: number | null;
  costRatio: number; netPct: number; maxDrawdownR: number; tradesPerHour: number;
  avgHoldS: number; eligible: boolean; status: string; source: string; settingsKey: string;
  evaluationWindows?: Record<string, EvaluationWindow>;
  liveN?: number; livePf?: number | null; liveStatus?: string; liveEnabled?: boolean;
  trainingUsedN?: number;
  measuredCosts?: boolean; openUnresolved?: number;
  variant?: string;
};

export type ForcedBestRow = {
  evidenceVersion?: number;
  id?: string;
  symbol: string;
  indication?: string;
  direction?: string;
  tpPct: number;
  slPct: number;
  slRatio?: number;
  trainPf?: number | null;
  pf?: number | null;
  holdoutPf?: number | null;
  tradesPerHour?: number;
  variant?: string;
  settingsKey?: string;
  trainN?: number;
  trainingUsedN?: number;
  n?: number;
  eligible?: boolean;
};

export type ForcedConfigSummary = {
  version?: number;
  pfDefinition?: string;
  needsReturnPfRecalculation?: boolean;
  detail?: string;
  rows?: ForcedConfigRow[]; symbols?: string[]; completed?: number; requested?: number;
  coveragePct?: number; selectedCount?: number; eligibleCount?: number;
  minPf?: number; updatedAt?: number; baselineOnly?: boolean; mainnetReady?: boolean;
  connection?: string; trialMode?: boolean;
  controlMinTrades?: number; controlMinPf?: number; trainingMinTrades?: number;
  variant?: string;
  variantSettings?: Record<string, number | boolean | string>;
  succeeded?: string[];
  missing?: string[];
  hours?: number;
  bestBySymbol?: Record<string, ForcedBestRow> | ForcedBestRow[];
  forcedBest?: Record<string, ForcedBestRow> | ForcedBestRow[];
  engineMinPf?: number;
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
  forcedOnly?: boolean;
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
  pfStats?: Record<string, HistCalcStrategy>;
  withWithout?: Record<string, { with?: HistCalcStrategy; without?: HistCalcStrategy }>;
  comboMatrix?: Array<{ indication: string; strategy: string; n?: number; pf?: number; wr?: number; evalN?: number; validated?: boolean }>;
  successfulConfigs?: Array<{ indication?: string; config?: string; strategy?: string; setId?: string; pf?: number; n?: number; wr?: number; validated?: boolean; slRatio?: number; step?: number; trailKey?: string }>;
  combo?: { engine?: string; journal?: string; cells?: number; successfulCount?: number; validatedCount?: number };
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
    combo?: boolean;
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
  ...(Object.fromEntries(IND_TYPE_KEYS.map((key) => [key, true])) as IndTypeFlags),
  preferMinimalRange: false,
  additionalCoordination: false,
  // Same default as the engine (hist_calc.default_options).
  coordOptimizationN: 50,
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

export function forcedBestBySymbol(data?: ForcedConfigSummary | null): ForcedBestRow[] {
  if (!isCurrentForcedEvidence(data)) return [];
  const floor = data?.minPf ?? 1.05;
  const hasEvidence = (row: ForcedBestRow) => Boolean(row) && row.evidenceVersion === FORCED_EVIDENCE_VERSION
    && Number(row.trainingUsedN ?? row.trainN ?? 0) > 0
    && typeof row.trainPf === "number" && Number.isFinite(row.trainPf)
    && row.trainPf > 1.02 && row.trainPf + 1e-9 >= floor && row.eligible !== false;
  const stored = data?.forcedBest || data?.bestBySymbol;
  if (Array.isArray(stored) && stored.length) {
    return stored.filter(hasEvidence).sort((a, b) => a.symbol.localeCompare(b.symbol));
  }
  if (stored && typeof stored === "object") {
    const fromMap = Object.entries(stored).map(([symbol, row]) => ({ ...row, symbol: row.symbol || symbol }));
    if (fromMap.length) return fromMap.filter(hasEvidence).sort((a, b) => a.symbol.localeCompare(b.symbol));
  }
  const ENGINE = data?.engineMinPf && data.engineMinPf > 0 ? data.engineMinPf : 1.1;
  const best = new Map<string, ForcedConfigRow>();
  for (const row of data?.rows ?? []) {
    if (!row.eligible || !hasEvidence(row)) continue;
    const cur = best.get(row.symbol);
    if (!cur) {
      best.set(row.symbol, row);
      continue;
    }
    const rowPf = row.trainPf as number;
    const curPf = cur.trainPf as number;
    const rowPass = rowPf + 1e-9 >= ENGINE;
    const curPass = curPf + 1e-9 >= ENGINE;
    if (rowPass !== curPass) {
      if (rowPass) best.set(row.symbol, row);
      continue;
    }
    const tph = row.tradesPerHour - cur.tradesPerHour;
    const sl = row.slPct - cur.slPct;
    if (tph > 0 || (tph === 0 && sl < 0) || (tph === 0 && sl === 0 && rowPf > curPf)) {
      best.set(row.symbol, row);
    }
  }
  return [...best.values()].sort((a, b) => a.symbol.localeCompare(b.symbol));
}

export const FORCED_EVIDENCE_VERSION = 4;
export const FORCED_RECALCULATION_MESSAGE = "Needs return-PF recalculation: legacy or unversioned baseline evidence is not eligible.";

/** A versioned row contract prevents classic-PF artifacts from becoming return-PF winners. */
export function isCurrentForcedEvidence(data?: ForcedConfigSummary | null): data is ForcedConfigSummary & { version: typeof FORCED_EVIDENCE_VERSION } {
  const rows = data?.rows ?? [];
  const storedRows = [data?.forcedBest, data?.bestBySymbol].flatMap(stored => stored == null ? [] : Array.isArray(stored) ? stored : Object.values(stored));
  return Boolean(data && data.version === FORCED_EVIDENCE_VERSION && !data.needsReturnPfRecalculation
    && Array.isArray(rows) && [...rows, ...storedRows].every(row => row && row.evidenceVersion === FORCED_EVIDENCE_VERSION));
}

export function guardForcedEvidence(data?: ForcedConfigSummary | null): ForcedConfigSummary | undefined {
  if (!data) return undefined;
  return isCurrentForcedEvidence(data) ? data : { version: data.version, needsReturnPfRecalculation: true, detail: FORCED_RECALCULATION_MESSAGE };
}

/** Zero is an observed result; null and a tape with no observations have no PF. */
export function formatForcedPf(value: number | null | undefined, samples?: number): string {
  return typeof value === "number" && Number.isFinite(value) && (samples == null || samples > 0) ? value.toFixed(3) : "—";
}

export function calcPollMs(job: HistCalcJob | null | undefined, hidden = false): number {
  if (hidden) return 8000;
  if (calcIsRunning(job?.phase)) return 1200;
  if (job?.stale || job?.ready || job?.phase === "ready" || (job?.nextRunAt && job.nextRunAt * 1000 > Date.now())) {
    return 8000;
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
    const job = (await r.json()) as HistCalcJob;
    job.forcedConfigs = guardForcedEvidence(job.forcedConfigs);
    const local = await fetchLocalForced(signal);
    if (local) {
      // A complete server result is authoritative; a separate static artifact
      // cannot replace its winners, successes, settings or cost contract.
      if (!isCurrentForcedEvidence(job.forcedConfigs)) job.forcedConfigs = local;
    }
    return job;
  } catch (e) {
    if (signal?.aborted) return { phase: "idle", pct: 0, detail: "aborted" };
    return { phase: "error", pct: 0, detail: String(e), error: String(e) };
  }
}

let localForcedCache: ForcedConfigSummary | null | undefined;
let localForcedCacheAt = 0;

export async function fetchLocalForced(signal?: AbortSignal): Promise<ForcedConfigSummary | null> {
  if (localForcedCache !== undefined && Date.now() - localForcedCacheAt < 30_000) return localForcedCache;
  try {
    const r = await fetch("/forced-configs.json", { cache: "no-store", signal });
    if (!r.ok) {
      localForcedCache = null;
      localForcedCacheAt = Date.now();
      return null;
    }
    const parsed = (await r.json()) as ForcedConfigSummary;
    localForcedCache = guardForcedEvidence(parsed) ?? null;
    localForcedCacheAt = Date.now();
    return localForcedCache;
  } catch {
    if (signal?.aborted) return null;
    localForcedCache = null;
    localForcedCacheAt = Date.now();
    return null;
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
