import { Pause, Play, Square } from "lucide-react";
import { useState, type ReactNode } from "react";
import {
  HIST_TEST_HOURS_DEFAULT,
  HIST_TEST_MIN_PF,
  histTestIsPaused,
  histTestIsRunning,
  histTestStartLabel,
  histTestStatusLine,
  type HistTestJob,
} from "@/lib/hist-test";

export function HistTestControls({
  job,
  enabled = true,
  hours = HIST_TEST_HOURS_DEFAULT,
  minPf = HIST_TEST_MIN_PF,
  onControl,
  children,
}: {
  job: HistTestJob | null;
  enabled?: boolean;
  hours?: number;
  minPf?: number;
  onControl: (action: "start" | "stop" | "pause" | "resume") => Promise<void>;
  children?: ReactNode;
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
            data-testid="hist-test-start"
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
