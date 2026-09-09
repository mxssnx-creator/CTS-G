import limits from "../../server/pulse/system-limits.json" with { type: "json" };

export type SystemKey = keyof typeof limits;
export type SystemSettings = Record<SystemKey, number>;
export const SYSTEM_LIMITS = limits;

export function normalizeSystemSettings(raw: Partial<SystemSettings> = {}): SystemSettings {
  const result = {} as SystemSettings;
  for (const key of Object.keys(limits) as SystemKey[]) {
    const [fallback, low, high, integer] = limits[key] as [number, number, number, boolean];
    const supplied = raw[key];
    const n = supplied == null || typeof supplied === "boolean" ? fallback : Number(supplied);
    const value = Math.max(low, Math.min(high, Number.isFinite(n) ? n : fallback));
    result[key] = integer ? Math.trunc(value) : value;
  }
  if (result.rssSoftMb && result.rssHardMb) result.rssHardMb = Math.max(result.rssSoftMb, result.rssHardMb);
  return result;
}

export type SystemStatus = {
  calculationCache?: { hits?: number; misses?: number; bypassed?: number; bypassReason?: string; errors?: number; pruned?: number; accountedBytes?: number; cachedSets?: number; entryLimit?: number; trimTargetPct?: number; error?: string };
  sharedDatabase?: { available?: boolean; keys?: number; expiringKeys?: number; memoryBytes?: number; maxMemoryBytes?: number; operationsPerSec?: number; policy?: string; appendOnly?: boolean; snapshotStatus?: string; appendStatus?: string; detail?: string };
  connection: string;
  persistent?: boolean;
  cpuPct?: number;
  memoryMb?: number;
  dbBytes?: number;
  storageMode?: "memory" | "disk" | "checkpoint";
  memoryBytes?: number;
  checkpointAt?: number;
  checkpointError?: string;
  checkpointDurationMs?: number;
  journalBytes?: number;
  journalLimitBytes?: number;
  memoryRestartRequired?: boolean;
  dbKeys?: number;
  dbRows?: Record<string, number>;
  requestsPerSec?: number;
  sessionRunningS?: number;
  snapshotAt?: number;
  sampledAt?: number;
  counters?: Record<string, number>;
  totals?: Record<string, number>;
  session?: { startedAt?: number; endedAt?: number; clean?: boolean };
  limits?: SystemSettings;
  directory?: string;
  dbFile?: string;
  error?: string;
  detail?: string;
  lastBackupAt?: number;
  lanes?: SystemStatus[];
};

export async function fetchSystemStatus(conn: string, signal?: AbortSignal): Promise<SystemStatus> {
  try {
    const response = await fetch(`/system.json?conn=${encodeURIComponent(conn)}`, { cache: "no-store", signal });
    const data = await response.json() as SystemStatus;
    if (!response.ok) return { connection: conn, error: data.detail || "System statistics unavailable" };
    return data;
  } catch {
    return { connection: conn, error: "System statistics unavailable" };
  }
}
