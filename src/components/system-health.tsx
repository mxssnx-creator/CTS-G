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
  return (
    <section aria-label="System resources" data-testid="system-health" className="min-w-0 space-y-3 rounded-radius border border-border bg-surface p-4">
      <h2 className="text-sm font-medium">System resources</h2>
      {!status && <p className="text-sm text-muted">Loading system measurements…</p>}
      {lanes.map((lane) => {
        const at = lane.snapshotAt ?? lane.sampledAt ?? 0;
        const stale = !at || Date.now() / 1000 - at > Math.max(30, (lane.limits?.systemMetricsIntervalS ?? 5) * 3);
        const counters = lane.counters;
        const runningS = lane.sessionRunningS == null ? undefined : lane.sessionRunningS + (!stale && !lane.session?.clean ? Math.max(0, Date.now() / 1000 - at) : 0);
        const fields: [string, string][] = [
          ["CPU", stale ? "—" : number(lane.cpuPct, "%", 1)],
          ["Memory", stale ? "—" : number(lane.memoryMb, " MiB", 1)],
          ["DB keys / rows", number(lane.dbKeys)],
          ["DB size", number(lane.dbBytes == null ? undefined : lane.dbBytes / 1048576, " MiB", 2)],
          ["Requests / sec", stale ? "—" : number(lane.requestsPerSec, "", 2)],
          ["Recoveries", number(counters?.recoveries ?? (lane.persistent ? 0 : undefined))],
          ["Crashes / unclean stops", number(counters?.crashes ?? (lane.persistent ? 0 : undefined))],
          ["Session running time", duration(runningS)],
        ];
        return <div key={lane.connection} className="min-w-0 rounded-lg border border-border bg-bg2 p-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="font-medium">{lane.connection === "bingx-x02" ? "VST · Simulated exchange" : lane.connection === "bingx-x01" ? "Live exchange" : lane.connection}</span>
            <span className="text-muted">{lane.error ? "Measurement unavailable" : stale ? "Waiting for current engine sample" : "Current"} · {lane.persistent ? "Persistent" : "Not initialized"}</span>
          </div>
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4 xl:grid-cols-8">
            {fields.map(([label, value]) => <div key={label} className="min-w-0"><dt className="text-xs text-muted">{label}</dt><dd className="mt-1 break-words font-mono text-sm">{value}</dd></div>)}
          </dl>
          {(lane.error || lane.detail) && <p role="status" className="mt-3 text-xs text-muted">{lane.error || lane.detail}</p>}
          {lane.calculationCache && <p className="mt-3 break-words text-xs text-muted">Redis calculations · {number(lane.calculationCache.hits)} reused · {number(lane.calculationCache.misses)} computed · {number(lane.calculationCache.cachedSets)} cached Sets · {number((lane.calculationCache.accountedBytes ?? 0) / 1048576, " MiB", 1)} budgeted · {lane.calculationCache.entryLimit} → {Math.floor((lane.calculationCache.entryLimit ?? 350) * (lane.calculationCache.trimTargetPct ?? 80) / 100)} newest entries{lane.calculationCache.error ? ` · ${lane.calculationCache.error}; local calculation continues` : ""}</p>}
          {lane.calculationCache?.bypassReason && <p className="text-xs text-muted">{lane.calculationCache.bypassReason} · {number(lane.calculationCache.bypassed)} calculated locally</p>}
        </div>;
      })}
      {status?.sharedDatabase && <div className="min-w-0 rounded-lg border border-border bg-bg2 p-3">
        <p className="text-xs font-medium">Shared Redis database</p>
        <p className="mt-2 break-words font-mono text-xs text-muted">{status.sharedDatabase.available
          ? `${number(status.sharedDatabase.keys)} keys · ${number((status.sharedDatabase.memoryBytes ?? 0) / 1048576, " MiB", 1)} · ${number(status.sharedDatabase.operationsPerSec)} ops/s · ceiling ${status.sharedDatabase.maxMemoryBytes ? number(status.sharedDatabase.maxMemoryBytes / 1048576, " MiB", 1) : "unset"} · ${status.sharedDatabase.policy} · AOF ${status.sharedDatabase.appendOnly ? status.sharedDatabase.appendStatus : "off"} · snapshot ${status.sharedDatabase.snapshotStatus}`
          : status.sharedDatabase.detail || "Metadata unavailable"}</p>
      </div>}
      <p className="text-xs text-muted">CPU is process usage (100% = one core). Requests are actual exchange REST calls. Crash counts include unclean shutdowns; clean restarts preserve all totals.</p>
    </section>
  );
}

export function SystemHealthFooter({ conn }: { conn: string }) {
  return <SystemHealth status={useSystemStatus(conn)} />;
}
