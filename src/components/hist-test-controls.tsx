import { Pause, Play, Square } from "lucide-react";
import { useState, type ReactNode } from "react";
import {
  HIST_TEST_HOURS_DEFAULT,
  HIST_TEST_MIN_PF,
  histTestIsEnabled,
  histTestIsPaused,
  histTestIsRunning,
  histTestOverviewLine,
  histTestStartLabel,
  histTestStatusLine,
  type HistTestJob,
  type HistTestLive,
} from "@/lib/hist-test";

export function HistTestControls({
  job,
  enabled = true,
  hours = HIST_TEST_HOURS_DEFAULT,
  minPf = HIST_TEST_MIN_PF,
  onControl,
  children,
  testId = "hist-test-start",
}: {
  job: HistTestJob | null;
  enabled?: boolean;
  hours?: number;
  minPf?: number;
  onControl: (action: "start" | "stop" | "pause" | "resume") => Promise<void>;
  children?: ReactNode;
  testId?: string;
}) {
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const paused = histTestIsPaused(job);
  const live = histTestIsRunning(job?.phase) && !paused;
  const startAction: "start" | "resume" = paused ? "resume" : "start";
  const anyBusy = Object.values(busy).some(Boolean);
  const run = async (action: "start" | "stop" | "pause" | "resume") => {
    if (busy[action]) return;
    if (action === "start" && !enabled) return;
    setBusy((b) => ({ ...b, [action]: true }));
    try {
      await onControl(action);
    } finally {
      setBusy((b) => ({ ...b, [action]: false }));
    }
  };
  const btn = "inline-flex min-h-11 items-center gap-1.5 rounded-lg px-3 text-sm disabled:opacity-40";
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex rounded-radius border border-border bg-surface p-1" data-testid="hist-test-controls">
          <button
            type="button"
            data-testid={testId}
            aria-label={paused ? "Resume historic test" : "Start historic test"}
            disabled={Boolean(busy[startAction]) || (startAction === "start" && !enabled)}
            onClick={() => void run(startAction)}
            className={`${btn} ${live ? "text-muted" : "text-primary"}`}
          >
            <Play className="size-4" /> {histTestStartLabel(job)}
          </button>
          <button
            type="button"
            data-testid="hist-test-pause"
            aria-label="Pause historic test"
            disabled={Boolean(busy.pause)}
            onClick={() => void run("pause")}
            className={btn}
          >
            <Pause className="size-4" /> Pause
          </button>
          <button
            type="button"
            data-testid="hist-test-stop"
            aria-label="Stop historic test"
            disabled={Boolean(busy.stop)}
            onClick={() => void run("stop")}
            className={`${btn} text-danger`}
          >
            <Square className="size-4" /> Stop
          </button>
        </div>
        {children}
        {anyBusy ? <span className="font-mono text-[10px] text-muted">…</span> : null}
      </div>
      <p className={`text-sm ${job?.error ? "text-danger" : "text-muted"}`} data-testid="hist-test-status">
        {histTestStatusLine(job, hours, minPf)}
      </p>
      {histTestIsRunning(job?.phase) || job?.ready ? (
        <div className="h-1.5 overflow-hidden rounded-full bg-border">
          <div
            className="h-full rounded-full bg-primary"
            style={{
              width: `${Math.max(0, Math.min(100, job?.ready && !histTestIsRunning(job?.phase) ? 100 : job?.pct || 0))}%`,
            }}
          />
        </div>
      ) : null}
    </div>
  );
}

export function HistTestStatus({
  histTest,
  enabled,
  compact = false,
}: {
  histTest?: HistTestLive | null;
  enabled?: boolean;
  compact?: boolean;
}) {
  const on = enabled ?? histTestIsEnabled(histTest);
  const blob = histTest || (on ? { enabled: true, phase: "ready" } : { enabled: false, phase: "off" });
  const line = on
    ? histTestOverviewLine({ ...blob, enabled: true })
    : histTestOverviewLine({ ...blob, enabled: false, phase: "off" });
  const sets = on ? (histTest?.runningSets || []).slice(0, compact ? 4 : 12) : [];
  return (
    <div className={compact ? "font-mono text-[11px] text-muted" : "space-y-1 font-mono text-xs"} data-testid="hist-test-live-status">
      <p className={on ? "text-primary" : "text-muted"}>{line}</p>
      {!compact && sets.length ? (
        <p className="text-muted" data-testid="hist-test-running-sets">
          running {sets.map((s) => s.id).filter(Boolean).join(" · ")}
        </p>
      ) : null}
    </div>
  );
}
