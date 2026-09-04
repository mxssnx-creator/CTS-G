import type { ActivityCounts, ActivityEvent, ActivitySummary, LiveStats } from "@/lib/live-stats";

const EVENT_TYPES = [
  "entry_intent",
  "exchange_request",
  "exchange_response",
  "fill",
  "position_open",
  "position_snapshot",
  "control_request",
  "control_response",
  "protection",
  "cancellation",
  "close",
  "rejected",
  "error",
  "reconciliation",
] as const;

function summaryFor(stats: LiveStats | null): ActivitySummary {
  return stats?.activity || stats?.coverage?.activity || {};
}

function eventRows(stats: LiveStats | null, activity: ActivitySummary): ActivityEvent[] {
  const rows = activity.tail || stats?.events || stats?.coverage?.events || [];
  return rows.filter((row): row is ActivityEvent => Boolean(row && typeof row === "object"));
}

function statusTone(status: string): string {
  const normalized = status.toLowerCase();
  if (["error", "rejected", "discrepant", "blocked"].includes(normalized)) return "text-danger";
  if (["pending", "selected"].includes(normalized)) return "text-warn";
  if (["confirmed", "filled", "recovered", "qualified"].includes(normalized)) return "text-primary";
  return "text-muted";
}

function countLabel(counts: Record<string, number> | undefined, key: string): number {
  return Number(counts?.[key] || 0);
}

function outcomeTotal(bucket: ActivityCounts | undefined): number {
  if (!bucket) return 0;
  return ["evaluated", "qualified", "selected", "entered", "exited", "blocked", "rejected", "paused"].reduce(
    (total, key) => total + Number(bucket[key as keyof ActivityCounts] || 0),
    0,
  );
}

export function ActivityPanel({ stats, compact = false }: { stats: LiveStats | null; compact?: boolean }) {
  const activity = summaryFor(stats);
  const events = eventRows(stats, activity);
  const byType = activity.byType || {};
  const parity = activity.parity || "pending";
  const parityTone = parity === "match" ? "text-primary" : parity === "discrepant" ? "text-danger" : "text-warn";

  if (compact) {
    return (
      <section className="rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs" data-testid="activity-strip">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-primary">activity ledger</span>
          <span className={parityTone}>parity {parity}</span>
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-muted">
          <span>events {activity.eventCount ?? 0}</span>
          <span>fills {activity.fillCount ?? 0}</span>
          <span>requests {activity.requestCount ?? 0}</span>
          <span>errors {activity.errorCount ?? 0}</span>
          <span>duplicates {activity.duplicateCount ?? 0}</span>
        </div>
      </section>
    );
  }

  return (
    <section className="rounded-radius border border-border bg-surface p-4" data-testid="activity-panel">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Committed activity ledger</h2>
          <p className="mt-1 text-xs text-muted">Bounded, idempotent exchange actions and internal transitions</p>
        </div>
        <span className={`font-mono text-xs ${parityTone}`}>exchange parity · {parity}</span>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
        <Metric label="Events" value={activity.eventCount ?? 0} />
        <Metric label="Requests" value={activity.requestCount ?? 0} />
        <Metric label="Responses" value={activity.responseCount ?? 0} />
        <Metric label="Fills" value={activity.fillCount ?? 0} />
        <Metric label="Controls" value={activity.protectionEventCount ?? 0} />
        <Metric label="Closes" value={activity.closeEventCount ?? 0} />
        <Metric label="Errors" value={activity.errorCount ?? 0} tone="danger" />
        <Metric label="Retries" value={activity.duplicateCount ?? 0} tone="warn" />
      </div>

      <div className="mt-3 flex flex-wrap gap-2 font-mono text-xs">
        {EVENT_TYPES.map((type) => (
          <span key={type} className="rounded-md border border-border px-2 py-1 text-muted">
            {type.replaceAll("_", " ")} {countLabel(byType, type)}
          </span>
        ))}
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-3">
        <OutcomeTable title="Indication outcomes" values={activity.byIndication} />
        <OutcomeTable title="Strategy outcomes" values={activity.byStrategy} />
        <OutcomeTable title="Coordination outcomes" values={activity.byAxis} />
      </div>

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-border pt-3 font-mono text-xs text-muted">
        <span>internal open {activity.internalOpen ?? 0}</span>
        <span>exchange open {activity.exchangeOpen == null || activity.exchangeOpen < 0 ? "—" : activity.exchangeOpen}</span>
        <span>closed {activity.internalClosed ?? 0}</span>
        <span>pending {activity.pendingCount ?? 0}</span>
        <span>recovered {activity.recoveredCount ?? 0}</span>
        <span>fees {Number(activity.fees ?? 0).toFixed(6)}</span>
      </div>

      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-xs">
          <caption className="mb-2 text-left font-mono text-[11px] text-muted">Latest committed events</caption>
          <thead className="font-mono text-muted">
            <tr>
              <th className="pb-2 font-medium">Time</th>
              <th className="pb-2 font-medium">Event</th>
              <th className="pb-2 font-medium">Status</th>
              <th className="pb-2 font-medium">Route</th>
              <th className="pb-2 font-medium">Exchange</th>
              <th className="pb-2 font-medium">Detail</th>
            </tr>
          </thead>
          <tbody>
            {events.length ? (
              events.slice(0, 16).map((event, index) => (
                <tr key={event.event_id || `${event.ts}-${index}`} className="border-t border-border font-mono">
                  <td className="py-1.5 text-muted">{formatTime(event.ts)}</td>
                  <td className="py-1.5 text-fg">{String(event.event_type || "event").replaceAll("_", " ")}</td>
                  <td className={`py-1.5 ${statusTone(String(event.status || ""))}`}>{event.status || "—"}</td>
                  <td className="py-1.5 text-muted">
                    {[event.symbol, event.side, event.indication_kind || event.strategy].filter(Boolean).join(" · ") || "—"}
                  </td>
                  <td className="py-1.5 text-muted">{event.code || event.order_id || "—"}</td>
                  <td className="max-w-[280px] truncate py-1.5 text-muted">{event.detail || "—"}</td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={6} className="py-6 text-center text-muted">
                  No committed activity yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone?: "danger" | "warn" }) {
  return (
    <div className="rounded-lg border border-border bg-bg2 px-2 py-2">
      <div className="font-mono text-[10px] text-muted uppercase">{label}</div>
      <div className={`mt-1 font-mono text-lg tabular-nums ${tone === "danger" ? "text-danger" : tone === "warn" ? "text-warn" : "text-fg"}`}>{value}</div>
    </div>
  );
}

function OutcomeTable({ title, values }: { title: string; values?: Record<string, ActivityCounts> }) {
  const rows = Object.entries(values || {}).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="rounded-lg border border-border bg-bg2 p-3">
      <h3 className="mb-2 text-xs font-medium text-muted uppercase">{title}</h3>
      <div className="space-y-1 font-mono text-[11px]">
        {rows.length ? (
          rows.slice(0, 8).map(([key, bucket]) => (
            <div key={key} className="flex items-center justify-between gap-2 text-muted">
              <span>{key}</span>
              <span className="text-fg">{outcomeTotal(bucket)} · in {bucket.entered ?? 0} · out {bucket.exited ?? 0}</span>
            </div>
          ))
        ) : (
          <div className="text-muted">No committed outcomes</div>
        )}
      </div>
    </div>
  );
}

function formatTime(ts?: number): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString();
}
