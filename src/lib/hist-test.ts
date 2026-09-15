export type HistTestSymbol = {
  symbol: string;
  pf?: number;
  n?: number;
  evalN?: number;
  wr?: number;
  maxDdS?: number;
  positive?: boolean;
  validated?: boolean;
  vol1h?: number;
  vol24h?: number;
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
  pfStats?: Record<string, { pf?: number; n?: number; wr?: number; evalN?: number; validated?: boolean }>;
  withWithout?: Record<string, { with?: { pf?: number; n?: number; wr?: number; validated?: boolean }; without?: { pf?: number; n?: number; wr?: number; validated?: boolean } }>;
  comboMatrix?: Array<{ indication: string; strategy: string; n?: number; pf?: number; wr?: number; evalN?: number; validated?: boolean }>;
  successfulConfigs?: Array<{ indication?: string; config?: string; strategy?: string; setId?: string; pf?: number; n?: number; wr?: number; validated?: boolean; slRatio?: number; step?: number }>;
  combo?: { engine?: string; journal?: string; cells?: number; successfulCount?: number };
  refreshHours?: number;
  nextRunAt?: number;
  continuous?: boolean;
};

export const HIST_TEST_HOURS_MIN = 4;
export const HIST_TEST_HOURS_MAX = 64;
export const HIST_TEST_HOURS_DEFAULT = 20;
export const HIST_TEST_HOURS_STEP = 1;
export const HIST_TEST_MIN_PF = 1.1;
export const HIST_TEST_TARGET_DEFAULT = 20;
export const HIST_TEST_REFRESH_MIN = 1;
export const HIST_TEST_REFRESH_MAX = 8;
export const HIST_TEST_REFRESH_DEFAULT = 2;

export const HIST_TEST_RUNNING_PHASES = ["queued", "rank", "evaluate", "fetch", "replay", "score", "paused"] as const;

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
  const positives = (job.positive?.length ? job.positive : job.symbols) || [];
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
    return `Ready · ${hours}h tape · min PF ${minPf.toFixed(2)} · fill until positive count · refresh ${job?.refreshHours ?? HIST_TEST_REFRESH_DEFAULT}h`;
  }
  const ready = Boolean(job.ready) || job.phase === "ready";
  const pct = ready && !histTestIsRunning(job.phase) ? 100 : Math.round(Number(job.pct) || 0);
  const detail = String(job.detail || "").trim();
  if (histTestIsPaused(job)) {
    return detail ? `paused · ${detail}` : "paused";
  }
  const head = `${job.phase} ${pct}%`;
  if (histTestIsRunning(job.phase)) return detail ? `${head} · ${detail}` : head;
  if (job.error) return `${head} · ${job.error}`;
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
    symbolCap: Math.max(1, Math.round(Number(body.symbolCap) || HIST_TEST_TARGET_DEFAULT)),
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
