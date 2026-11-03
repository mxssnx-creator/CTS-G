import { useEffect, useState, type ReactNode } from "react";
import type { LiveStats, SystemInfo } from "@/lib/live-stats";

/**
 * The desk's bottom bar: session and running info, CPU, memory, request rates, the last start (clean or crash), the restore events and the
 * errors. Every chip opens a dialog with the details (server: Pulse.system_info).
 */
type DialogId = "session" | "cpu" | "mem" | "req" | "start" | "restore" | "errors";

export function SystemFooter({ stats }: { stats: LiveStats | null }) {
  const sys: SystemInfo = stats?.system ?? {};
  const [open, setOpen] = useState<DialogId | null>(null);
  const restore = sys.restore ?? [];
  const errs = sys.recentErrors ?? [];
  const lr = sys.lastRun ?? {};
  const crashed = lr.clean === false && lr.reason !== "first start";
  const memPct = sys.machineMb ? (100 * (sys.rssMb ?? 0)) / sys.machineMb : null;
  const chips: Array<{ id: DialogId; label: string; value: string; tone?: "warn" | "danger" | "ok" }> = [
    { id: "session", label: "Session", value: `${sys.conn ?? stats?.connection ?? "—"} · up ${dur(sys.uptimeS ?? stats?.uptimeS)}` },
    {
      id: "cpu",
      label: "CPU",
      value: sys.cpuPct == null ? "—" : `${sys.cpuPct.toFixed(0)}% of ${sys.cpuCount ?? "?"} cores`,
      tone: sys.cpuPct != null && sys.cpuPct > 90 ? "warn" : undefined,
    },
    {
      id: "mem",
      label: "Memory",
      value: `${(sys.rssMb ?? stats?.rssMb ?? 0).toFixed(0)} MB${sys.machineMb ? ` / ${(sys.machineMb / 1024).toFixed(1)} GB` : ""}`,
      tone: memPct != null && memPct > 40 ? "warn" : undefined,
    },
    { id: "req", label: "Requests", value: `REST ${num(sys.restPerS)}/s · WS ${num(sys.wsPerS)}/s` },
    {
      id: "start",
      label: "Last start",
      value: lr.reason === "first start" ? "first start" : crashed ? "after a crash / kill" : lr.clean ? "after a clean stop" : "—",
      tone: crashed ? "danger" : "ok",
    },
    { id: "restore", label: "Restore", value: `${restore.length} event${restore.length === 1 ? "" : "s"}`, tone: restore.length ? "warn" : undefined },
    {
      id: "errors",
      label: "Errors",
      value: `${errs.length} recent${sys.lastError ? " · last set" : ""}`,
      tone: errs.length ? "danger" : "ok",
    },
  ];
  return (
    <footer className="rounded-radius border border-border bg-surface px-3 py-2" data-testid="system-footer">
      <div className="flex flex-wrap gap-2">
        {chips.map((c) => (
          <button
            key={c.id}
            type="button"
            onClick={() => setOpen(c.id)}
            className={`rounded-xl border px-3 py-1.5 text-left font-mono text-xs transition-colors hover:bg-bg2 ${
              c.tone === "danger" ? "border-danger text-danger" : c.tone === "warn" ? "border-warn text-warn" : "border-border"
            }`}
            data-testid={`sys-${c.id}`}
          >
            <span className="block text-[10px] tracking-wide text-muted uppercase">{c.label}</span>
            <span className="tabular-nums">{c.value}</span>
          </button>
        ))}
      </div>
      {open ? (
        <Dialog title={chips.find((c) => c.id === open)?.label ?? ""} onClose={() => setOpen(null)}>
          {open === "session" ? (
            <Rows
              rows={[
                ["Connection", sys.conn ?? "—"],
                ["Endpoint", sys.base ?? "—"],
                ["Process", String(sys.pid ?? "—")],
                ["Started", when(sys.startedAt)],
                ["Uptime", dur(sys.uptimeS)],
                ["Equity", `${num(stats?.equity, 4)} ${stats?.unit ?? ""}`],
                ["Session PnL", num(stats?.sessionPnl, 4)],
                ["Open positions", String(stats?.openCount ?? 0)],
                ["Wins / losses", `${stats?.wins ?? 0} / ${stats?.losses ?? 0}`],
                ["Halted", stats?.halted ? `yes (${stats?.haltReason ?? ""})` : "no"],
              ]}
            />
          ) : null}
          {open === "cpu" ? (
            <Rows
              rows={[
                ["This process", sys.cpuPct == null ? "measuring" : `${sys.cpuPct.toFixed(1)}% (100% = one core)`],
                ["Cores", String(sys.cpuCount ?? "—")],
                ["Load level", String(stats?.engine?.load?.level ?? "—")],
                ["Hot / warm loop", `${num(stats?.engine?.hotMs, 0)} / ${num(stats?.engine?.warmMs, 0)} ms`],
                ["Replay phase", String((stats?.sets as { progress?: { phase?: string } } | undefined)?.progress?.phase ?? "—")],
              ]}
            />
          ) : null}
          {open === "mem" ? (
            <Rows
              rows={[
                ["Resident (RSS)", `${num(sys.rssMb, 1)} MB`],
                ["Machine", sys.machineMb ? `${num(sys.machineMb, 0)} MB` : "—"],
                ["Available", sys.availMb ? `${num(sys.availMb, 0)} MB` : "—"],
                ["Governor soft / hard", `${num(stats?.engine?.load?.softMb, 0)} / ${num(stats?.engine?.load?.hardMb, 0)} MB`],
                ["Replay running", stats?.engine?.load?.histRun === false ? "shed" : "yes"],
              ]}
            />
          ) : null}
          {open === "req" ? (
            <Rows
              rows={[
                ["REST requests", `${num(sys.restPerS)}/s · ${sys.restTotal ?? 0} total`],
                ["WebSocket messages", `${num(sys.wsPerS)}/s · ${sys.wsTotal ?? 0} total`],
                ["Database", sys.db ?? "none"],
                ["Rate trips", String(stats?.api?.rateTrips ?? 0)],
                ["HTTP errors", String(stats?.api?.httpErr ?? 0)],
              ]}
            />
          ) : null}
          {open === "start" ? (
            <Rows
              rows={[
                ["This start", when(sys.startedAt)],
                ["Previous run started", when(lr.previousStartedAt ?? undefined)],
                ["Previous run last seen", when(lr.previousEnd ?? undefined)],
                ["Ended cleanly", lr.clean ? "yes" : "no"],
                ["Reason", lr.reason ?? "—"],
                ["Positions in the book at start", String(lr.openAtStart ?? 0)],
              ]}
            />
          ) : null}
          {open === "restore" ? (
            restore.length ? (
              <List items={restore.slice().reverse().map((r) => `${when(r.t)} · ${r.what} · ${r.symbol} ${r.side} · ${r.detail}`)} />
            ) : (
              <p className="text-sm text-muted">No restore events this session: every saved position matched the venue.</p>
            )
          ) : null}
          {open === "errors" ? (
            <div className="grid gap-2">
              {sys.lastError ? <p className="font-mono text-xs text-danger">last: {sys.lastError}</p> : null}
              {errs.length ? (
                <List
                  items={errs
                    .slice()
                    .reverse()
                    .map((e) => `${when(e.t)} · ${e.kind ?? ""} ${e.method ?? ""} ${e.path ?? ""} ${e.code ?? ""} · ${e.msg ?? ""}`)}
                />
              ) : (
                <p className="text-sm text-muted">No recent API errors.</p>
              )}
            </div>
          ) : null}
        </Dialog>
      ) : null}
    </footer>
  );
}

function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-4 sm:items-center" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="max-h-[80vh] w-full max-w-2xl overflow-y-auto rounded-radius border border-border bg-surface p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-medium tracking-wide uppercase">{title}</h3>
          <button type="button" onClick={onClose} className="rounded-lg border border-border px-2 py-1 font-mono text-xs" aria-label="Close">
            close
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function Rows({ rows }: { rows: Array<[string, string]> }) {
  return (
    <dl className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] gap-x-4 gap-y-1.5 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="font-mono break-words tabular-nums">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function List({ items }: { items: string[] }) {
  return (
    <ul className="grid gap-1 font-mono text-xs">
      {items.map((t, i) => (
        <li key={i} className="rounded-lg border border-border bg-bg2 px-2 py-1 break-words">
          {t}
        </li>
      ))}
    </ul>
  );
}

function num(n: number | null | undefined, d = 1): string {
  return n == null || !Number.isFinite(n) ? "—" : n.toFixed(d);
}

function dur(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${Math.floor(s % 60)}s`;
}

function when(t: number | null | undefined): string {
  if (!t) return "—";
  return new Date(t * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC";
}
