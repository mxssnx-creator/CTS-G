import { Link, useRouterState } from "@tanstack/react-router";
import { Blocks, ChartSpline, LayoutDashboard, LineChart, Pause, Play, Square, SlidersHorizontal, Timer } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useConnection } from "@/components/connection-provider";
import { postControl, type ConnType } from "@/lib/connections";
import { engineDotClass, engineStatusLabel } from "@/lib/status-tone";
import { knownCount, livePosOrders, realPosOrders } from "@/lib/live-stats";

export function DeskShell({
  children,
  live,
  mode,
  paused,
  halted,
  alive,
  statsType,
  statsId,
}: {
  children: ReactNode;
  live?: boolean;
  mode?: string;
  paused?: boolean;
  halted?: boolean;
  alive?: boolean;
  statsType?: string;
  statsId?: string;
}) {
  const path = useRouterState({ select: (s) => s.location.pathname });
  const { conn, setConn, catalog } = useConnection();
  const lane = catalog?.types.find((x) => x.type === conn);
  const engineLive = live ?? (lane ? Boolean(lane.running && !lane.halted && !lane.paused) : undefined);
  const enginePaused = paused ?? (lane ? Boolean(lane.paused) : undefined);
  const engineHalted = halted ?? (lane ? Boolean(lane.halted) : undefined);
  const engineAlive = alive ?? (lane ? lane.alive !== false : undefined);
  const onDesk = path === "/";
  const onResults = path.startsWith("/results");
  const onSettings = path.startsWith("/settings");
  const onSystem = path.startsWith("/system");
  const onSweep = path.startsWith("/step-sweep");
  const onSim70 = path.startsWith("/sim-70h");
  const title = onSettings
    ? "Settings & config"
    : onResults
      ? "Results"
      : onSystem
        ? "System"
        : onSweep
          ? "Historic test"
          : onSim70
            ? "70h complete test"
          : "Pulse desk";
  const sub = onSettings
    ? "Per-connection CTS + overlay — Test Historic is first on Overview, default ON"
    : onResults
      ? "Closed tape, equity path, symbol and exit breakdown"
      : onSystem
        ? "Generic core · exchange / strategy / risk slots · extend without rewriting the loop"
        : onSweep
          ? "Historic test · 4–64h · fill until positive count · steps 3–12"
          : onSim70
            ? "70-hour replay · 5 symbols · last-N 10–70 · all indications, strategies and types"
          : "Independent desks in parallel · pick Overall, Live or VST";
  const types: { id: ConnType; label: string; short: string }[] = [
    { id: "overall", label: "Overall", short: "Overall" },
    { id: "live", label: "Live", short: "Live" },
    { id: "vst", label: "VST demo", short: "VST" },
  ];

  return (
    <main
      className="desk-grid min-h-screen overflow-x-clip"
      data-testid="desk-root"
      data-conn={conn}
      data-stats-type={statsType || ""}
      data-stats-id={statsId || ""}
      suppressHydrationWarning
    >
      <div className="mx-auto flex w-full min-w-0 max-w-6xl flex-col gap-5 px-4 py-5 sm:px-6 sm:py-7">
        <div className="grid min-w-0 grid-cols-3 gap-1 rounded-radius border border-border bg-surface p-1" data-testid="conn-switch">
          {types.map((t) => {
            const lane = catalog?.types.find((x) => x.type === t.id);
            const on = conn === t.id;
            const running = Boolean(lane?.running);
            const realPair = realPosOrders(lane);
            const livePair = livePosOrders(lane);
            const livePos = knownCount(lane?.livePositionCount) ?? knownCount(lane?.exchangeOpenCount);
            const liveOrd = knownCount(lane?.liveOrderCount);
            const xchN = knownCount(lane?.exchangeOpenCount) ?? -1;
            const liveN = knownCount(lane?.livePositionCount) ?? -1;
            const simN = lane?.simOpenCount ?? -1;
            const xchMismatch = xchN >= 0 && liveN >= 0 && xchN !== liveN;
            const dot = engineDotClass({
              running,
              halted: Boolean(lane?.halted),
              paused: Boolean(lane?.paused),
              alive: lane?.alive !== false,
            });
            return (
              <button
                key={t.id}
                type="button"
                data-testid={`conn-${t.id}`}
                aria-pressed={on}
                aria-label={`${t.label} · Positions/Orders R ${realPair}${livePos != null || liveOrd != null ? ` · L ${livePair}` : ""}`}
                onClick={() => setConn(t.id)}
                className={`flex min-h-12 min-w-0 flex-col items-center justify-center overflow-hidden rounded-lg px-1 py-1.5 text-center sm:px-2 ${
                  on ? "bg-bg2 text-fg" : "text-muted"
                }`}
              >
                <div className="flex max-w-full items-center justify-center gap-1.5">
                  <span className={`size-2 shrink-0 rounded-full ${dot}`} />
                  <span className="truncate text-sm font-medium">
                    <span className="sm:hidden">{t.short}</span>
                    <span className="hidden sm:inline">{t.label}</span>
                  </span>
                </div>
                <div
                  className={`mt-0.5 flex max-w-full flex-wrap items-center justify-center gap-x-1.5 font-mono text-[10px] leading-tight ${xchMismatch ? "text-danger" : ""}`}
                  title="Positions = unique symbol+direction · Orders = complete working book · R Real · L Live"
                >
                  <span className="whitespace-nowrap">R {realPair}</span>
                  {livePos != null || liveOrd != null ? <span className="whitespace-nowrap">L {livePair}</span> : null}
                  {simN > 0 ? <span className="whitespace-nowrap">S {simN}</span> : null}
                </div>
              </button>
            );
          })}
        </div>

        <header className="flex min-w-0 flex-col gap-3 border-b border-border pb-4">
          <div className="flex min-w-0 flex-wrap items-end justify-between gap-3">
            <div className="min-w-0">
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
              <p className="mt-1 max-w-xl text-sm text-muted">{sub}</p>
            </div>
            <div className="flex min-w-0 items-center gap-3 rounded-radius border border-border bg-surface px-3 py-2">
              <span className={`size-2.5 shrink-0 rounded-full ${engineLive && !engineHalted && !enginePaused ? "live-dot" : ""} ${engineDotClass({ running: engineLive, halted: engineHalted, paused: enginePaused, alive: engineAlive })}`} />
              <div className="min-w-0 leading-tight">
                <div className="font-mono text-xs text-muted">{engineStatusLabel({ running: engineLive, halted: engineHalted, paused: enginePaused, mode }).sub}</div>
                <div className="truncate text-sm font-medium">{engineStatusLabel({ running: engineLive, halted: engineHalted, paused: enginePaused, mode }).text}</div>
              </div>
            </div>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <nav className="flex min-w-0 flex-wrap rounded-radius border border-border bg-surface p-1">
              <NavLink to="/" on={onDesk} icon={<LayoutDashboard className="size-4" />} label="Desk" />
              <NavLink to="/results" on={onResults} icon={<LineChart className="size-4" />} label="Results" />
              <NavLink to="/sim-70h" on={onSim70} icon={<Timer className="size-4" />} label="70h test" short="70h" />
              <NavLink to="/step-sweep" on={onSweep} icon={<ChartSpline className="size-4" />} label="Historic test" short="Historic" />
              <NavLink to="/system" on={onSystem} icon={<Blocks className="size-4" />} label="System" />
              <NavLink to="/settings" on={onSettings} icon={<SlidersHorizontal className="size-4" />} label="Settings" />
            </nav>
            <EngineControls conn={conn} live={engineLive} paused={enginePaused} />
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
      setMsg(r.detail || (r.ok ? `${action} accepted` : `${action} failed`));
      window.dispatchEvent(new CustomEvent("pulse:control", { detail: { conn, action, result: r } }));
    } catch (e) {
      setMsg(String(e));
      window.dispatchEvent(new CustomEvent("pulse:control", { detail: { conn, action, result: { ok: false } } }));
    } finally {
      setBusy((b) => ({ ...b, [action]: false }));
    }
  };
  const btn = "inline-flex min-h-11 items-center gap-1.5 rounded-lg px-2.5 text-sm sm:px-3 disabled:opacity-40";
  const active = Boolean(live) && !paused;
  const startAction = paused ? "resume" : "start";
  const anyBusy = Object.values(busy).some(Boolean);
  return (
    <div className="flex min-w-0 flex-col items-stretch gap-1 sm:items-end">
      <div className="flex flex-wrap rounded-radius border border-border bg-surface p-1">
        <button
          type="button"
          data-testid="engine-start"
          aria-label={paused ? "Resume engine" : "Start engine"}
          className={`${btn} ${active ? "text-muted" : "text-primary"}`}
          disabled={Boolean(busy[startAction])}
          onClick={() => run(startAction)}
        >
          <Play className="size-4" /> {paused ? "Resume" : "Start"}
        </button>
        <button
          type="button"
          data-testid="engine-pause"
          aria-label="Pause engine"
          className={`${btn}`}
          disabled={Boolean(busy.pause)}
          onClick={() => run("pause")}
        >
          <Pause className="size-4" /> Pause
        </button>
        <button
          type="button"
          data-testid="engine-stop"
          aria-label="Stop engine"
          className={`${btn} text-danger`}
          disabled={Boolean(busy.stop)}
          onClick={() => run("stop")}
        >
          <Square className="size-4" /> Stop
        </button>
      </div>
      {msg || anyBusy ? <span className="max-w-72 text-right font-mono text-[10px] text-muted [overflow-wrap:anywhere]">{anyBusy ? "…" : msg}</span> : <span className="font-mono text-[10px] text-muted uppercase">{conn}</span>}
    </div>
  );
}

function NavLink({
  to,
  on,
  icon,
  label,
  short,
}: {
  to: "/" | "/results" | "/settings" | "/step-sweep" | "/system" | "/sim-70h";
  on: boolean;
  icon: ReactNode;
  label: string;
  short?: string;
}) {
  return (
    <Link
      to={to}
      className={`inline-flex min-h-11 min-w-0 items-center gap-1.5 rounded-lg px-2.5 text-sm sm:gap-2 sm:px-3 ${
        on ? "bg-bg2 text-fg" : "text-muted"
      }`}
    >
      {icon}
      {short ? (
        <>
          <span className="sm:hidden">{short}</span>
          <span className="hidden sm:inline">{label}</span>
        </>
      ) : (
        label
      )}
    </Link>
  );
}
