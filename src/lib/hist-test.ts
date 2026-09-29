export type HistTestSymbol = {
  symbol: string;
  pf?: number;
  n?: number;
  evalN?: number;
  wr?: number;
  maxDdS?: number;
  avgDdS?: number;
  pfDdRatio?: number;
  positive?: boolean;
  validated?: boolean;
  vol1h?: number;
  vol24h?: number;
};

export type HistTestRunningSet = {
  id?: string;
  indication?: string;
  strategy?: string;
  symbol?: string;
  pf?: number;
  n?: number;
  step?: number;
  validated?: boolean;
};

export type HistTestJob = {
  ok?: boolean;
  phase: string;
  pct: number;
  detail: string;
  ready?: boolean;
  running?: boolean;
  paused?: boolean;
  error?: string;
  hours?: number;
  minPf?: number;
  positivePf?: number;
  targetCount?: number;
  filled?: number;
  evaluated?: number;
  symbols?: string[];
  positive?: string[];
  rejected?: HistTestSymbol[] | string[];
  skipped?: Array<{ symbol?: string; reason?: string }>;
  ranked?: HistTestSymbol[];
  bySymbol?: HistTestSymbol[];
  bestStep?: { step?: number; pf?: number; pfDdRatio?: number; validated?: boolean };
  fill?: { filled?: number; target?: number; short?: number; evaluated?: number; rejectedCount?: number };
  elapsedMs?: number;
  resumePhase?: string;
  pfStats?: Record<string, { pf?: number; n?: number; wr?: number; evalN?: number; validated?: boolean; maxDdS?: number; avgDdS?: number }>;
  withWithout?: Record<string, { with?: { pf?: number; n?: number; wr?: number; validated?: boolean; maxDdS?: number }; without?: { pf?: number; n?: number; wr?: number; validated?: boolean; maxDdS?: number } }>;
  comboMatrix?: Array<{ indication: string; strategy: string; n?: number; pf?: number; wr?: number; evalN?: number; validated?: boolean; maxDdS?: number }>;
  successfulConfigs?: Array<{ indication?: string; config?: string; strategy?: string; setId?: string; pf?: number; n?: number; wr?: number; validated?: boolean; slRatio?: number; step?: number; maxDdS?: number }>;
  combo?: { engine?: string; journal?: string; cells?: number; successfulCount?: number };
  refreshHours?: number;
  nextRunAt?: number;
  continuous?: boolean;
  validatedIds?: string[];
  recalcOnly?: boolean;
  runningSets?: HistTestRunningSet[];
  internSymbols?: string[];
  enabled?: boolean;
  ownsCatalog?: boolean;
  catalogSkipped?: boolean;
  internSetCount?: number;
  validatedCount?: number;
  processingCount?: number;
  processedSetCount?: number;
  byIndication?: Record<string, { pf?: number; n?: number; maxDdS?: number; wr?: number; validated?: boolean }>;
  byStrategy?: Record<string, { pf?: number; n?: number; maxDdS?: number; wr?: number; validated?: boolean }>;
  kinds?: Record<string, { pf?: number; n?: number; maxDdS?: number; wr?: number; validated?: boolean }>;
};

export type HistTestLive = {
  enabled?: boolean;
  ownsCatalog?: boolean;
  catalogSkipped?: boolean;
  phase?: string;
  pct?: number;
  detail?: string;
  internSetCount?: number;
  validatedCount?: number;
  processingCount?: number;
  processedSetCount?: number;
  runningSets?: HistTestRunningSet[];
  symbols?: string[];
  internSymbols?: string[];
  selectedCoordinations?: HistTestRunningSet[];
  withWithout?: HistTestJob["withWithout"];
  comboMatrix?: HistTestJob["comboMatrix"];
  successfulConfigs?: HistTestJob["successfulConfigs"];
  pfStats?: HistTestJob["pfStats"];
  byIndication?: HistTestJob["byIndication"];
  byStrategy?: HistTestJob["byStrategy"];
  ready?: boolean;
  running?: boolean;
  paused?: boolean;
  hours?: number;
  filled?: number;
  targetCount?: number;
  stale?: boolean;
};

export function histTestIsEnabled(ht?: HistTestLive | null): boolean {
  if (!ht) return false;
  if (ht.enabled === false || ht.ownsCatalog === false || ht.phase === "off") return false;
  if (ht.enabled === true || ht.ownsCatalog === true) return true;
  return Boolean(ht.phase && ht.phase !== "idle" && ht.phase !== "off");
}

export function histTestOverviewLine(ht?: HistTestLive | null): string {
  if (!histTestIsEnabled(ht)) return "Test Historic · OFF · full catalog in play";
  const intern = ht?.internSetCount ?? 0;
  const n = ht?.validatedCount ?? 0;
  const ids = (ht?.runningSets || []).map((r) => r.id).filter(Boolean).slice(0, 6);
  const coords = (ht?.selectedCoordinations || ht?.successfulConfigs || []).length;
  const proc = ht?.processingCount ?? 0;
  const book = Array.isArray(ht?.internSymbols) && ht.internSymbols.length
    ? ht.internSymbols
    : (Array.isArray(ht?.symbols) ? ht.symbols : []);
  const syms = book.slice(0, 8);
  const parts = ["Test Historic · ON"];
  if (intern) parts.push(`${intern} intern`);
  parts.push(`${n} validated`);
  if (proc) parts.push(`${proc} processing`);
  if (coords) parts.push(`${coords} coordinations`);
  if (ids.length) parts.push(`sets ${ids.join(" ")}`);
  if (syms.length) parts.push(syms.join(" "));
  if (ht?.detail && !ids.length && !syms.length) parts.push(String(ht.detail));
  return parts.join(" · ");
}

export const HIST_TEST_HOURS_MIN = 4;
export const HIST_TEST_HOURS_MAX = 64;
export const HIST_TEST_HOURS_DEFAULT = 20;
export const HIST_TEST_HOURS_STEP = 1;
export const HIST_TEST_MIN_PF = 1.1;
export const HIST_TEST_TARGET_DEFAULT = 50;
/** Test Historic evaluates the 50 intern majors only; a larger fill target can never be met. */
export const HIST_TEST_TARGET_MAX = 50;
export const HIST_TEST_VALIDATE_CAP = 250;
export const HIST_TEST_REFRESH_MIN = 1;
export const HIST_TEST_REFRESH_MAX = 8;
export const HIST_TEST_REFRESH_DEFAULT = 2;

export const HIST_TEST_RUNNING_PHASES = ["queued", "rank", "evaluate", "fetch", "replay", "score", "score-refresh", "paused"] as const;

export function clampHistTestHours(value: unknown, fallback = HIST_TEST_HOURS_DEFAULT): number {
  const n = Math.round(Number(value));
  const base = Number.isFinite(n) ? n : fallback;
  return Math.max(HIST_TEST_HOURS_MIN, Math.min(HIST_TEST_HOURS_MAX, base));
}

export function clampHistTestRefreshHours(value: unknown, fallback = HIST_TEST_REFRESH_DEFAULT): number {
  const n = Math.round(Number(value));
  const base = Number.isFinite(n) ? n : fallback;
  return Math.max(HIST_TEST_REFRESH_MIN, Math.min(HIST_TEST_REFRESH_MAX, base));
}

export function clampHistTestTarget(value: unknown, fallback = HIST_TEST_TARGET_DEFAULT): number {
  const n = Math.round(Number(value));
  const base = Number.isFinite(n) && n > 0 ? n : fallback;
  return Math.max(1, Math.min(HIST_TEST_TARGET_MAX, base));
}

export function histTestLookbackBars(hours: unknown): number {
  return clampHistTestHours(hours) * 60;
}

export function histTestIsRunning(phase?: string | null): boolean {
  return Boolean(phase && (HIST_TEST_RUNNING_PHASES as readonly string[]).includes(phase));
}

export function histTestIsPaused(job?: HistTestJob | null): boolean {
  if (!job) return false;
  return Boolean(job.paused) || job.phase === "paused";
}

export function normalizeHistTestJob(job: HistTestJob): HistTestJob {
  const phase = String(job.phase || "idle");
  // An explicit empty positive list means nothing cleared the floor; symbols is the intern book.
  const positives = (Array.isArray(job.positive) ? job.positive : job.symbols) || [];
  const ready = Boolean(job.ready) || phase === "ready";
  let pct = Number(job.pct);
  if (!Number.isFinite(pct)) pct = 0;
  if (ready && phase === "ready" && !job.error && pct < 99) pct = 100;
  let filled = job.filled;
  if (filled == null || (filled === 0 && positives.length > 0)) filled = positives.length;
  let evaluated = job.evaluated ?? job.fill?.evaluated;
  if (evaluated == null) {
    const match = String(job.detail || "").match(/evaluated\s+(\d+)/i);
    if (match) evaluated = Number(match[1]);
  }
  return { ...job, phase, pct, filled, evaluated, ready: ready && phase === "ready" ? true : job.ready };
}

export function histTestStartLabel(job?: HistTestJob | null): string {
  if (histTestIsPaused(job)) return "Resume";
  if (job?.ready || job?.phase === "ready") return "Refresh now";
  return "Start";
}

export function histTestPollMs(job?: HistTestJob | null, hidden = false): number {
  if (hidden) return 8000;
  if (histTestIsRunning(job?.phase) || histTestIsPaused(job)) return 1200;
  return 8000;
}

export function histTestStatusLine(job: HistTestJob | null | undefined, hours = HIST_TEST_HOURS_DEFAULT, minPf = HIST_TEST_MIN_PF): string {
  if (!job || !job.phase || job.phase === "idle") {
    return `Ready · ${hours}h tape · min PF ${minPf.toFixed(2)} · fill until positive count · refresh ${job?.refreshHours ?? HIST_TEST_REFRESH_DEFAULT}h · recalc validated configs only`;
  }
  const ready = Boolean(job.ready) || job.phase === "ready";
  const pct = ready && !histTestIsRunning(job.phase) ? 100 : Math.round(Number(job.pct) || 0);
  const detail = String(job.detail || "").trim();
  if (histTestIsPaused(job)) {
    return detail ? `paused · ${detail}` : "paused";
  }
  const head = `${job.phase} ${pct}%`;
  if (histTestIsRunning(job.phase)) return detail ? `${head} · ${detail}` : head;
  const err = String(job.error || "");
  if (err && !err.startsWith("audit:")) return `${head} · ${err}`;
  if (job.nextRunAt) {
    const when = new Date(job.nextRunAt * 1000).toLocaleTimeString();
    return detail ? `${head} · next refresh ${when} · ${detail}` : `${head} · next refresh ${when}`;
  }
  return detail ? `${head} · ${detail}` : head;
}

async function postHistTest(body: Record<string, unknown>): Promise<HistTestJob> {
  try {
    const r = await fetch("/hist-test.json", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = (await r.json().catch(() => ({}))) as HistTestJob;
    if (!r.ok) return { phase: "error", pct: 0, detail: j.detail || `rejected ${r.status}`, error: j.error };
    return j;
  } catch (e) {
    return { phase: "error", pct: 0, detail: String(e), error: String(e) };
  }
}

export async function fetchHistTest(signal?: AbortSignal): Promise<HistTestJob> {
  try {
    const r = await fetch("/hist-test.json", { cache: "no-store", signal });
    if (!r.ok) return { phase: "idle", pct: 0, detail: `status ${r.status}` };
    return normalizeHistTestJob((await r.json()) as HistTestJob);
  } catch (e) {
    if (signal?.aborted) return { phase: "idle", pct: 0, detail: "aborted" };
    return { phase: "error", pct: 0, detail: String(e), error: String(e) };
  }
}

export async function startHistTest(body: {
  hours: number;
  minPf: number;
  symbolCap: number;
  overlay?: Record<string, unknown>;
  action?: "start" | "resume";
  refreshHours?: number;
}): Promise<HistTestJob> {
  const refreshHours = clampHistTestRefreshHours(body.refreshHours ?? body.overlay?.histTestRefreshHours, HIST_TEST_REFRESH_DEFAULT);
  return postHistTest({
    action: body.action || "start",
    hours: clampHistTestHours(body.hours),
    minPf: body.minPf,
    histTestMinPf: body.minPf,
    histTestRefreshHours: refreshHours,
    refreshHours,
    symbolCap: clampHistTestTarget(body.symbolCap),
    overlay: { ...(body.overlay || {}), histTestRefreshHours: refreshHours },
  });
}

export async function pauseHistTest(): Promise<HistTestJob> {
  return postHistTest({ action: "pause" });
}

export async function stopHistTest(): Promise<HistTestJob> {
  return postHistTest({ action: "stop" });
}

export async function resumeHistTest(body: {
  hours: number;
  minPf: number;
  symbolCap: number;
  overlay?: Record<string, unknown>;
}): Promise<HistTestJob> {
  return startHistTest({ ...body, action: "resume" });
}

export type HistTestAssignedConfig = {
  id: string;
  indication?: string;
  strategy?: string;
  pf?: number;
  n?: number;
};

export type HistTestAssignment = {
  /** Validated symbols the operator selection should follow, in job order. */
  symbols: string[];
  /** Validated configs (Set ids) the engine allow-list runs, best PF first. */
  configs: HistTestAssignedConfig[];
  /** Stable key of symbols + configs; changes only when the validated result does. */
  signature: string;
};

const USDT_SYMBOL = /^[A-Z0-9]{1,20}-USDT$/;

/**
 * What a finished Test Historic run assigns: its validated symbols (filtered by
 * `isEligible`, capped at `cap`, 0 = uncapped) and validated configs. Null until
 * the run is ready, or while it holds nothing to assign. A non-fatal `audit:`
 * note on the job does not block assignment.
 */
export function histTestAssignment(
  job: HistTestJob | null | undefined,
  isEligible: (symbol: string) => boolean = () => true,
  cap = 0,
): HistTestAssignment | null {
  if (!job) return null;
  const err = String(job.error || "");
  if (err && !err.startsWith("audit:")) return null;
  const ready = Boolean(job.ready) || job.phase === "ready";
  if (!ready || histTestIsRunning(job.phase)) return null;

  const rawSymbols = job.internSymbols?.length ? job.internSymbols : job.positive?.length ? job.positive : job.symbols || [];
  const symbols: string[] = [];
  const seenSymbols = new Set<string>();
  for (const raw of rawSymbols) {
    const symbol = String(raw || "").trim().toUpperCase();
    if (!USDT_SYMBOL.test(symbol) || seenSymbols.has(symbol) || !isEligible(symbol)) continue;
    seenSymbols.add(symbol);
    symbols.push(symbol);
  }
  const limit = Math.max(0, Math.round(Number(cap) || 0));
  const picked = limit > 0 ? symbols.slice(0, limit) : symbols;

  const configs: HistTestAssignedConfig[] = [];
  const seenConfigs = new Set<string>();
  const addConfig = (row: HistTestAssignedConfig) => {
    if (!row.id || seenConfigs.has(row.id)) return;
    seenConfigs.add(row.id);
    configs.push(row);
  };
  for (const row of job.successfulConfigs || []) {
    if (!row || row.validated === false) continue;
    addConfig({
      id: String(row.setId || row.config || "").trim(),
      indication: row.indication,
      strategy: row.strategy,
      pf: row.pf,
      n: row.n,
    });
  }
  for (const id of job.validatedIds || []) addConfig({ id: String(id || "").trim() });
  configs.sort((a, b) => (b.pf ?? -1) - (a.pf ?? -1) || (b.n ?? 0) - (a.n ?? 0) || a.id.localeCompare(b.id));

  if (!picked.length && !configs.length) return null;
  const signature = `${[...picked].sort().join(",")}|${configs.map((c) => c.id).sort().join(",")}`;
  return { symbols: picked, configs, signature };
}

type SymbolSelection = { symbols: string[]; symbolsAll?: boolean; symbolCap?: number };

/** True when the selection already holds exactly these symbols, in any order. */
export function histTestSelectionMatches(selection: readonly string[], symbols: readonly string[]): boolean {
  if (selection.length !== symbols.length) return false;
  const have = new Set(selection.map((s) => String(s).toUpperCase()));
  return symbols.every((s) => have.has(String(s).toUpperCase()));
}

/**
 * Put the validated symbols into the selection. Returns the same object when
 * the selection already matches, so callers can skip a no-op save. The cap
 * only grows to fit; 0 (unlimited) stays 0.
 */
export function applyHistTestSymbols<T extends SymbolSelection>(overlay: T, symbols: readonly string[]): T {
  if (!symbols.length || histTestSelectionMatches(overlay.symbols, symbols)) return overlay;
  const cap = Math.max(0, Math.round(Number(overlay.symbolCap) || 0));
  return {
    ...overlay,
    symbols: [...symbols],
    symbolsAll: false,
    symbolCap: cap === 0 ? 0 : Math.max(cap, symbols.length),
  };
}
