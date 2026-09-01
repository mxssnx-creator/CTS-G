import { Link, useRouterState } from "@tanstack/react-router";
import { Blocks, LayoutDashboard, LineChart, Pause, Play, Square, SlidersHorizontal } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useConnection } from "@/components/connection-provider";
import { postControl, type ConnType } from "@/lib/connections";

export function DeskShell({
  children,
  live,
  mode,
  paused,
  statsType,
  statsId,
}: {
  children: ReactNode;
  live?: boolean;
  mode?: string;
  paused?: boolean;
  statsType?: string;
  statsId?: string;
}) {
  const path = useRouterState({ select: (s) => s.location.pathname });
  const { conn, setConn, catalog } = useConnection();
  const onDesk = path === "/";
  const onResults = path.startsWith("/results");
  const onSettings = path.startsWith("/settings");
  const onSystem = path.startsWith("/system");
  const title = onSettings
    ? "Settings & config"
    : onResults
      ? "Results"
      : onSystem
        ? "System"
        : "Pulse desk";
  const sub = onSettings
    ? "Per-connection CTS + overlay — Live and VST stay independent"
    : onResults
      ? "Closed tape, equity path, symbol and exit breakdown"
      : onSystem
        ? "Generic core · exchange / strategy / risk slots · extend without rewriting the loop"
        : "Independent desks in parallel · pick Overall, Live or VST";
  const types: { id: ConnType; label: string; hint: string }[] = [
    { id: "overall", label: "Overall", hint: "all" },
    { id: "live", label: "Live", hint: "USDT" },
    { id: "vst", label: "VST demo", hint: "VST" },
  ];

  return (
    <main
      className="desk-grid min-h-screen"
      data-testid="desk-root"
      data-conn={conn}
      data-stats-type={statsType || ""}
      data-stats-id={statsId || ""}
      suppressHydrationWarning
    >
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 px-4 py-5 sm:px-6 sm:py-7">
        <div className="grid grid-cols-3 gap-1 rounded-radius border border-border bg-surface p-1" data-testid="conn-switch">
          {types.map((t) => {
            const lane = catalog?.types.find((x) => x.type === t.id);
            const on = conn === t.id;
            const running = Boolean(lane?.running);
            const openN = lane?.openCount ?? 0;
            const xchN = lane?.exchangeOpenCount ?? -1;
            const simN = lane?.simOpenCount ?? -1;
            const xchMismatch = xchN >= 0 && xchN !== openN;
            return (
              <button
                key={t.id}
                type="button"
                data-testid={`conn-${t.id}`}
                aria-pressed={on}
                onClick={() => setConn(t.id)}
                className={`min-h-12 rounded-lg px-2 py-2 text-center ${
                  on ? "bg-bg2 text-fg" : "text-muted"
                }`}
              >
                <div className="flex items-center justify-center gap-2">
                  <span
                    className={`size-2 rounded-full ${running ? "bg-primary" : "bg-danger"}`}
                  />
                  <span className="text-sm font-medium">{t.label}</span>
                </div>
                <div
                  className={`mt-0.5 font-mono text-[10px] tracking-wide uppercase ${xchMismatch ? "text-danger" : ""}`}
                >
                  {t.hint}
                  {` · R ${openN}`}
                  {xchN >= 0 ? ` · L ${xchN}` : ""}
                  {simN > 0 ? ` · S ${simN}` : ""}
                </div>
              </button>
            );
          })}
        </div>

        <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border pb-4">
          <div>
            <p className="font-mono text-xs tracking-[0.22em] text-muted uppercase">
              {conn === "overall"
                ? "All desks · independent · parallel"
                : conn === "vst"
                  ? "BingX X02 · Prod-VST demo"
                  : "BingX X01 · live mainnet"}
            </p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
              {title}
            </h1>
            <p className="mt-1 text-sm text-muted">{sub}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <nav className="flex flex-wrap rounded-radius border border-border bg-surface p-1">
              <NavLink to="/" on={onDesk} icon={<LayoutDashboard className="size-4" />} label="Desk" />
              <NavLink to="/results" on={onResults} icon={<LineChart className="size-4" />} label="Results" />
              <NavLink to="/system" on={onSystem} icon={<Blocks className="size-4" />} label="System" />
              <NavLink to="/settings" on={onSettings} icon={<SlidersHorizontal className="size-4" />} label="Settings" />
            </nav>
            <EngineControls conn={conn} live={live} paused={paused} />
            <div className="flex items-center gap-3 rounded-radius border border-border bg-surface px-3 py-2">
              <span className={`live-dot size-2.5 rounded-full ${paused ? "bg-warn" : live ? "bg-primary" : "bg-danger"}`} />
              <div className="leading-tight">
                <div className="font-mono text-xs text-muted">{paused ? "PAUSED" : mode ?? "CONNECTING"}</div>
                <div className="text-sm font-medium">{paused ? "PAUSE" : live ? "LIVE" : "OFFLINE"}</div>
              </div>
            </div>
          </div>
        </header>
        {children}
      </div>
    </main>
  );
}

function EngineControls({ conn, live, paused }: { conn: ConnType; live?: boolean; paused?: boolean }) {
  // Per-action in-flight tracking: only the button whose request is running
  // is disabled (prevents double-submit); every other action stays clickable
  // so rapid switching always works. The sidecar serializes the actions.
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const run = async (action: "start" | "stop" | "pause" | "resume") => {
    if (busy[action]) return;
    if (conn === "overall") {
      const ok = window.confirm(
        `${action.toUpperCase()} both Live and VST desks?\nOpen positions stay on the exchange — no flatten.`,
      );
      if (!ok) return;
    } else if (action === "stop") {
      const who = conn === "vst" ? "VST demo" : "Live mainnet";
      const ok = window.confirm(`Stop ${who}? Open positions stay on BingX — no flatten.`);
      if (!ok) return;
    }
    setBusy((b) => ({ ...b, [action]: true }));
    setMsg(null);
    try {
      const r = await postControl(conn, action);
      setMsg(r.detail);
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy((b) => ({ ...b, [action]: false }));
      // Event-based coordination: the desk repolls stats immediately instead
      // of waiting out the 2s poll cadence.
      window.dispatchEvent(new CustomEvent("pulse:control"));
    }
  };
  const btn = "inline-flex min-h-11 items-center gap-1.5 rounded-lg px-3 text-sm disabled:opacity-40";
  const active = Boolean(live) && !paused;
  const startAction = paused ? "resume" : "start";
  const anyBusy = Object.values(busy).some(Boolean);
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex rounded-radius border border-border bg-surface p-1">
        <button type="button" className={`${btn} ${active ? "text-muted" : "text-primary"}`} disabled={Boolean(busy[startAction])} onClick={() => run(startAction)}>
          <Play className="size-4" /> {paused ? "Resume" : "Start"}
        </button>
        <button type="button" className={`${btn}`} disabled={Boolean(busy.pause)} onClick={() => run("pause")}>
          <Pause className="size-4" /> Pause
        </button>
        <button type="button" className={`${btn} text-danger`} disabled={Boolean(busy.stop)} onClick={() => run("stop")}>
          <Square className="size-4" /> Stop
        </button>
      </div>
      {msg || anyBusy ? <span className="max-w-72 text-right font-mono text-[10px] text-muted">{anyBusy ? "…" : msg}</span> : <span className="font-mono text-[10px] text-muted uppercase">{conn}</span>}
    </div>
  );
}

function NavLink({
  to,
  on,
  icon,
  label,
}: {
  to: "/" | "/results" | "/settings" | "/system";
  on: boolean;
  icon: ReactNode;
  label: string;
}) {
  return (
    <Link
      to={to}
      className={`inline-flex min-h-11 items-center gap-2 rounded-lg px-3 text-sm ${
        on ? "bg-bg2 text-fg" : "text-muted"
      }`}
    >
      {icon}
      {label}
    </Link>
  );
}
