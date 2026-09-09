import { type SystemStatus } from "@/lib/system-settings";
import { useSystemStatus } from "@/lib/use-system-status";

function number(value?: number, suffix = "", digits = 0) {
  return value == null || !Number.isFinite(value) ? "—" : `${value.toLocaleString(undefined, { maximumFractionDigits: digits })}${suffix}`;
}

function duration(value?: number) {
  if (value == null) return "—";
  const seconds = Math.max(0, Math.floor(value));
  return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds / 3600) % 24}h ${Math.floor(seconds / 60) % 60}m ${seconds % 60}s`;
}

export function SystemHealth({ status }: { status: SystemStatus | null }) {
  const lanes = status?.lanes ?? (status ? [status] : []);
  const currentLanes = lanes.filter((lane) => !isStale(lane));
  const redis = status?.sharedDatabase;
  const redisSummary = redis?.available
    ? `${number(redis.keys)} Redis keys · ${number(redis.operationsPerSec, "", 1)} ops/s`
    : "Redis metadata pending";
  const requestSummary = currentLanes.length
    ? `${currentLanes.map((lane) => `${lane.connection} ${number(lane.requestsPerSec, "", 1)}`).join(" · ")} REST req/s`
    : "REST req/s pending";
  return (
    <details aria-label="System resources" data-testid="system-health" className="min-w-0 rounded-radius border border-border bg-surface p-4">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 text-sm font-medium [&::-webkit-details-marker]:hidden">
        <span>System info</span>
        <span className="font-mono text-xs text-muted">
          {status ? `${currentLanes.length}/${lanes.length || 1} current` : "loading"} · {requestSummary} · {redisSummary}
        </span>
        <span className="ml-auto text-xs text-muted">expand</span>
      </summary>
      <div className="mt-3 space-y-3">
        {!status && <p className="text-sm text-muted">Loading system measurements…</p>}
        {lanes.map((lane) => {
        const at = lane.snapshotAt ?? lane.sampledAt ?? 0;
        const stale = isStale(lane);
        const counters = lane.counters;
        const runningS = lane.sessionRunningS == null ? undefined : lane.sessionRunningS + (!stale && !lane.session?.clean ? Math.max(0, Date.now() / 1000 - at) : 0);
        const fields: [string, string][] = [
          ["CPU", stale ? "—" : number(lane.cpuPct, "%", 1)],
          ["Memory", stale ? "—" : number(lane.memoryMb, " MiB", 1)],
          ["DB rows / keys", number(lane.dbKeys)],
          ["DB file size", number(lane.dbBytes == null ? undefined : lane.dbBytes / 1048576, " MiB", 2)],
          ["REST requests / sec", stale ? "—" : number(lane.requestsPerSec, "", 2)],
          ["Recoveries", number(counters?.recoveries ?? (lane.persistent ? 0 : undefined))],
          ["Crashes / unclean stops", number(counters?.crashes ?? (lane.persistent ? 0 : undefined))],
          ["Session running time", duration(runningS)],
        ];
        if (lane.storageMode) fields.push(
          ["SQLite", lane.storageMode === "memory" ? "RAM" : lane.storageMode === "checkpoint" ? "Saved checkpoint" : "Disk"],
          ["SQLite RAM", number(lane.memoryBytes == null ? undefined : lane.memoryBytes / 1048576, " MiB", 2)],
          ["Redo journal", number(lane.journalBytes == null ? undefined : lane.journalBytes / 1024, " KiB", 1)],
          ["Checkpoint age", duration(lane.checkpointAt ? Date.now() / 1000 - lane.checkpointAt : undefined)],
        );
          return <div key={lane.connection} className="min-w-0 rounded-lg border border-border bg-bg2 p-3">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-xs">
              <span className="font-medium">{lane.connection === "bingx-x02" ? "VST · Simulated exchange" : lane.connection === "bingx-x01" ? "Live exchange" : lane.connection}</span>
              <span className="text-muted">{lane.error ? "Measurement unavailable" : stale ? "Waiting for current engine sample" : "Current"} · {lane.persistent ? "Persistent" : "Not initialized"}</span>
            </div>
            <dl className={`grid grid-cols-2 gap-4 sm:grid-cols-4 ${lane.storageMode ? "xl:grid-cols-6" : "xl:grid-cols-8"}`}>
              {fields.map(([label, value]) => <div key={label} className="min-w-0"><dt className="text-xs text-muted">{label}</dt><dd className="mt-1 break-words font-mono text-sm">{value}</dd></div>)}
            </dl>
            {lane.dbRows && <p className="mt-3 break-words font-mono text-xs text-muted">DB rows · {Object.entries(lane.dbRows).map(([name, count]) => `${name} ${number(count)}`).join(" · ")}</p>}
            {(lane.error || lane.detail) && <p role="status" className="mt-3 text-xs text-muted">{lane.error || lane.detail}</p>}
            {lane.storageMode === "checkpoint" && <p className="mt-3 text-xs text-muted">Showing the saved checkpoint; the current engine database is unavailable.</p>}
            {lane.memoryRestartRequired && <p role="status" className="mt-3 text-xs text-muted">Saved SQLite mode takes effect at the next engine restart.</p>}
            {lane.checkpointError && <p role="status" className="mt-3 text-xs text-muted">Checkpoint pending ({lane.checkpointError}); durable journal retained for recovery.</p>}
            {lane.calculationCache && <p className="mt-3 break-words text-xs text-muted">Redis calculations · {number(lane.calculationCache.hits)} reused · {number(lane.calculationCache.misses)} computed · {number(lane.calculationCache.cachedSets)} cached Sets · {number((lane.calculationCache.accountedBytes ?? 0) / 1048576, " MiB", 1)} budgeted · {lane.calculationCache.entryLimit} → {Math.floor((lane.calculationCache.entryLimit ?? 350) * (lane.calculationCache.trimTargetPct ?? 80) / 100)} newest entries{lane.calculationCache.error ? ` · ${lane.calculationCache.error}; local calculation continues` : ""}</p>}
            {lane.calculationCache?.bypassReason && <p className="text-xs text-muted">{lane.calculationCache.bypassReason} · {number(lane.calculationCache.bypassed)} calculated locally</p>}
          </div>;
        })}
        {redis && <div className="min-w-0 rounded-lg border border-border bg-bg2 p-3">
          <p className="text-xs font-medium">Shared Redis database</p>
          <p className="mt-2 break-words font-mono text-xs text-muted">{redis.available
            ? `${number(redis.databaseCount)} DBs · ${number(redis.keys)} keys · ${number((redis.memoryBytes ?? 0) / 1048576, " MiB", 1)} · ${number(redis.operationsPerSec, "", 1)} ops/s · ceiling ${redis.maxMemoryBytes ? number(redis.maxMemoryBytes / 1048576, " MiB", 1) : "unset"} · ${redis.policy} · AOF ${redis.appendOnly ? redis.appendStatus : "off"} · snapshot ${redis.snapshotStatus}`
            : redis.detail || "Metadata unavailable"}</p>
          {redis.databases && <p className="mt-2 break-words font-mono text-xs text-muted">Redis DBs · {Object.entries(redis.databases).map(([name, info]) => `${name} ${number(info.keys)} keys / ${number(info.expiringKeys)} expiring`).join(" · ")}</p>}
        </div>}
        <p className="text-xs text-muted">CPU is process usage (100% = one core). REST req/s are actual exchange calls; Redis ops/s is the shared-server rate. Crash counts include unclean shutdowns; clean restarts preserve totals.</p>
      </div>
    </details>
  );
}

function isStale(lane: SystemStatus) {
  const at = lane.snapshotAt ?? lane.sampledAt ?? 0;
  return !at || Date.now() / 1000 - at > Math.max(30, (lane.limits?.systemMetricsIntervalS ?? 5) * 3);
}

export function SystemHealthFooter({ conn }: { conn: string }) {
  return <SystemHealth status={useSystemStatus(conn)} />;
}
