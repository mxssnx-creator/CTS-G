import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Layers,
  ShieldAlert,
  Wallet,
} from "lucide-react";
import { fetchLiveStats, pickView, type LiveStats } from "@/lib/live-stats";
import { derive } from "@/lib/derive-stats";
import { buildOverview, formatDuration } from "@/lib/analytics";
import { StatsOverview } from "@/components/stats-overview";
import { DeskShell } from "@/components/desk-shell";
import { useConnection } from "@/components/connection-provider";
import {
  BlockHeat,
  EquityArea,
  Meter,
  SlTpTape,
  TradeBars,
  WinRing,
} from "@/components/visual-stats";
import { CoverageBar } from "@/components/coverage-overview";
import { KindStrategyStrip } from "@/components/kind-strategy-stats";
import { ActivityPanel } from "@/components/activity-overview";
import type { ConnType } from "@/lib/connections";

export const Route = createFileRoute("/")({ component: DeskPage });

function DeskPage() {
  const { conn } = useConnection();
  const [raw, setRaw] = useState<LiveStats | null>(null);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setRaw(null);
    // Non-overlapping poll: the next pull is scheduled only after the
    // current one finished, so slow fetches can never stack up. Control
    // actions (start/stop/pause) trigger an immediate extra pull via the
    // pulse:control event instead of waiting out the cadence.
    const pull = async () => {
      const s = await fetchLiveStats(conn);
      if (!alive) return;
      setRaw(s);
      const hidden = typeof document !== "undefined" && document.hidden;
      timer = setTimeout(pull, hidden ? 8000 : 3500);
    };
    const kick = () => {
      if (!alive) return;
      if (timer) clearTimeout(timer);
      void pull();
    };
    window.addEventListener("pulse:control", kick);
    void pull();
    return () => {
      alive = false;
      window.removeEventListener("pulse:control", kick);
      if (timer) clearTimeout(timer);
    };
  }, [conn]);
  const stats = pickView(raw, conn);
  const d = useMemo(() => derive(stats), [stats]);
  const overview = useMemo(
    () =>
      buildOverview(
        (stats?.closed ?? []).map((c) => ({ pnl: c.pnl, t: c.t, pnl_pct: c.pnl_pct })),
      ),
    [stats],
  );
  const session = stats?.systemPnl ?? stats?.sessionPnl ?? 0;
  const grow = stats?.systemGrow ?? 0;
  const loss = stats?.systemLoss ?? 0;

  return (
    <DeskShell
      live={Boolean(stats?.running && !stats?.halted && !stats?.paused)}
      mode={stats?.paused ? "PAUSED" : stats?.mode}
      paused={Boolean(stats?.paused || stats?.haltReason === "paused")}
      statsType={stats?.connType}
      statsId={stats?.connection}
    >
      {!stats ? (
        <p className="rounded-radius border border-border bg-surface px-4 py-10 text-center text-sm text-muted" data-testid="desk-loading">
          Loading {conn === "vst" ? "VST demo" : conn === "live" ? "Live mainnet" : "all desks"}…
        </p>
      ) : null}
      {conn === "overall" && (stats?.lanes?.length ?? 0) > 0 ? <LaneBoard stats={stats!} /> : null}

      <section className="grid gap-3 lg:grid-cols-3">
        <div className="rounded-radius border border-border bg-surface p-5 lg:col-span-2">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="font-mono text-xs tracking-wide text-muted uppercase">Equity</p>
              <p className="mt-1 font-mono text-3xl tabular-nums">
                {conn === "overall"
                  ? `Live $${fmt(stats?.equityLive, 4)}`
                  : stats?.unit === "VST" || stats?.connType === "vst"
                    ? `${fmt(stats?.equity, 4)} VST`
                    : `$${fmt(stats?.equity, 4)}`}
              </p>
              <p className="mt-1 font-mono text-[11px] tracking-wide text-muted uppercase" data-testid="desk-identity">
                {stats?.connType || conn} · {stats?.unit || (conn === "vst" ? "VST" : conn === "live" ? "USDT" : "MIXED")}
                {stats?.connection ? ` · ${stats.connection}` : ""}
              </p>
              {conn === "overall" ? (
                <p className="mt-1 font-mono text-sm text-muted">
                  VST {fmt(stats?.equityVst, 4)} · live {fmt(stats?.sessionPnlLive, 4)} (g {fmt(stats?.systemGrowLive, 4)} / l {fmt(stats?.systemLossLive, 4)}) · vst {fmt(stats?.sessionPnlVst, 4)} (g {fmt(stats?.systemGrowVst, 4)} / l {fmt(stats?.systemLossVst, 4)})
                </p>
              ) : null}
              <p className={`mt-1 font-mono text-sm ${pnlClass(session)}`}>
                system {session >= 0 ? "+" : ""}
                {fmt(session, 4)} · grow {fmt(grow, 4)} / loss {fmt(loss, 4)} · {fmt(stats?.pnlPct, 2)}%
              </p>
            </div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <span className="text-muted">Available</span>
              <span className="font-mono text-right tabular-nums">${fmt(stats?.available, 3)}</span>
              <span className="text-muted">Used margin</span>
              <span className="font-mono text-right tabular-nums">${fmt(stats?.usedMargin, 3)}</span>
              <span className="text-muted">Unrealized</span>
              <span className={`font-mono text-right tabular-nums ${pnlClass(stats?.unrealized ?? 0)}`}>
                {fmt(stats?.unrealized, 4)}
              </span>
              <span className="text-muted">Uptime</span>
              <span className="font-mono text-right">{stats ? ago(stats.uptimeS) : "—"}</span>
            </div>
          </div>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <Meter label="Margin used" value={d.marginPct} danger={d.marginPct > 85} />
            <Meter label="Drawdown" value={d.ddPct} max={18} danger={d.ddPct > 8} />
          </div>
          <CoordStrip stats={stats} />
          <div className="mt-3">
            <CoverageBar live={stats} />
          </div>
          <ControlsStrip stats={stats} />
          <PacksStrip stats={stats} />
          <SetsStrip stats={stats} />
          <ExitStrip stats={stats} />
          <VariantsStrip stats={stats} />
          <EngineStrip stats={stats} />
          <WorkStrip stats={stats} />
          <IndicationStrip stats={stats} />
          <KindStrategyStrip stats={stats} />
          <div className="mt-3">
            <ActivityPanel stats={stats} compact />
          </div>
        </div>
        <div className="rounded-radius border border-border bg-surface p-5">
          <p className="mb-3 font-mono text-xs tracking-wide text-muted uppercase">Hit rate</p>
          <WinRing wins={stats?.wins ?? 0} losses={stats?.losses ?? 0} />
          <div className="mt-4 grid grid-cols-2 gap-2 font-mono text-xs text-muted">
            <span>PF {d.pf >= 99 ? "∞" : d.pf.toFixed(2)}</span>
            <span className="text-right">hold {d.avgHold.toFixed(0)}s</span>
            <span>
              E {d.expectancy >= 0 ? "+" : ""}
              {d.expectancy.toFixed(4)}
            </span>
            <span className="text-right">{stats?.activityPerMin ?? 0}/min</span>
          </div>
        </div>
      </section>

      <StatsOverview data={overview} compact live={stats} />

      <section className="grid gap-3 lg:grid-cols-2">
        <Panel title="Realized equity path" icon={<Activity className="size-4" />}>
          <EquityArea data={d.equityCurve} />
        </Panel>
        <Panel title="Closed trade PnL" icon={<Wallet className="size-4" />}>
          <TradeBars data={d.tradeBars} />
        </Panel>
      </section>

      <section className="rounded-radius border border-border bg-surface p-4">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Open book</h2>
          <span className="font-mono text-xs text-muted">
            {stats?.openCount ?? 0}/{stats?.maxOpen ? stats.maxOpen : "∞"} · {stats?.regime}
          </span>
        </div>
        {(stats?.open ?? []).length === 0 ? (
          <p className="py-8 text-center text-sm text-muted">{stats ? "Scanning — no live slot" : "Loading book"}</p>
        ) : (
          <>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {stats!.open.slice(0, 18).map((p) => (
              <article key={`${p.connType || conn}-${p.symbol}-${p.side}-${p.setId || p.clientId || ""}`} className="rounded-xl border border-border bg-bg2 p-4">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="text-lg font-medium">{p.symbol.replace("-USDT", "")}</div>
                    <div className="flex items-center gap-1">
                      <SideChip side={p.side} />
                      {p.connType ? (
                        <span className="rounded-full bg-bg2 px-1.5 py-0.5 font-mono text-[10px] text-muted uppercase">
                          {p.connType}
                        </span>
                      ) : null}
                    </div>
                  </div>
                  <div className={`font-mono text-lg tabular-nums ${pnlClass(p.uPnlPct)}`}>
                    {p.uPnlPct >= 0 ? "+" : ""}
                    {fmt(p.uPnlPct, 3)}%
                  </div>
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 font-mono text-xs text-muted">
                  <dt>Qty</dt>
                  <dd className="text-right text-fg">{fmt(p.qty, 4)}</dd>
                  <dt>Entry</dt>
                  <dd className="text-right text-fg">{fmtPx(p.entry)}</dd>
                  <dt>Mark</dt>
                  <dd className="text-right text-fg">{fmtPx(p.px)}</dd>
                  <dt>Age</dt>
                  <dd className="text-right text-fg">{ago(p.ageS)}</dd>
                  <dt>Controls</dt>
                  <dd className={`text-right ${p.controls ? "text-primary" : "text-danger"}`}>
                    {p.controls ? "SL+TP" : "none"}
                  </dd>
                  <dt>Security</dt>
                  <dd className={`text-right ${p.secSlOid && p.secTpOid ? "text-primary" : "text-danger"}`}>
                    {p.secSlOid && p.secTpOid ? "SEC" : "gap"}
                  </dd>
                  <dt>SL:TP</dt>
                  <dd className="text-right text-fg">{p.slRatio != null ? p.slRatio.toFixed(1) : "—"}</dd>
                  <dt>Trail</dt>
                  <dd className="text-right text-fg">{p.trailKey || "—"}</dd>
                </dl>
                <SlTpTape p={p} />
              </article>
            ))}
          </div>
          {stats!.open.length > 18 ? (
            <div className="mt-3 grid grid-cols-2 gap-1 font-mono text-[11px] text-muted sm:grid-cols-3 lg:grid-cols-4">
              {stats!.open.slice(18).map((p) => (
                <div key={`${p.connType || ""}-${p.symbol}-${p.side}-${p.clientId || ""}`} className="flex items-center justify-between rounded-md border border-border px-2 py-1">
                  <span>{p.symbol.replace("-USDT", "")} {p.side === "LONG" ? "L" : "S"}</span>
                  <span className={pnlClass(p.uPnlPct)}>{p.uPnlPct >= 0 ? "+" : ""}{fmt(p.uPnlPct, 2)}%</span>
                </div>
              ))}
            </div>
          ) : null}
          </>
        )}
      </section>

      <section className="grid gap-3 lg:grid-cols-2">
        <Panel title="Block 1–12" icon={<Layers className="size-4" />}>
          {stats ? <BlockHeat stats={stats} /> : <p className="text-sm text-muted">Waiting</p>}
        </Panel>
        <Panel title="Risk tape" icon={<ShieldAlert className="size-4" />}>
          {(stats?.open ?? []).length ? (
            <div className="space-y-3">
              {stats!.open.slice(0, 12).map((p) => (
                <SlTpTape key={`${p.connType || ""}-${p.symbol}-${p.side}`} p={p} />
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted">No protection tape until a slot is live</p>
          )}
        </Panel>
      </section>

      <section className="rounded-radius border border-border bg-surface p-4">
        <h2 className="mb-3 text-sm font-medium tracking-wide text-muted uppercase">
          Universe · {stats?.symbols?.length ?? 0}/{stats?.symbolMax ? stats.symbolMax : "unlimited"}
        </h2>
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">
          {(() => {
            const all = stats?.symbols ?? [];
            const openSet = new Set((stats?.open ?? []).map((p) => p.symbol));
            const ranked = [...all].sort((a, b) => Number(openSet.has(b)) - Number(openSet.has(a)));
            const shown = ranked.slice(0, 64);
            return (
              <>
                {shown.map((s) => {
                  const open = stats?.open?.find((p) => p.symbol === s);
                  const px = stats?.prices?.[s];
                  return (
                    <div
                      key={s}
                      className={`rounded-lg border px-2 py-2 ${
                        open ? "border-primary bg-primary-dim/20" : "border-border bg-bg2"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-1">
                        <span className="font-mono text-xs">{s.replace("-USDT", "")}</span>
                        {open ? <SideChip side={open.side} compact /> : null}
                      </div>
                      <div className="mt-1 font-mono text-[11px] text-muted tabular-nums">{fmtPx(px)}</div>
                    </div>
                  );
                })}
                {all.length > shown.length ? (
                  <div className="rounded-lg border border-border bg-bg2 px-2 py-2 font-mono text-[11px] text-muted">
                    +{all.length - shown.length} more
                  </div>
                ) : null}
              </>
            );
          })()}
        </div>
      </section>
    </DeskShell>
  );
}

const PROGRESS_PHASE_LABEL: Record<string, string> = {
  idle: "idle",
  starting: "starting",
  fetch: "fetching history",
  replay: "calculating sets",
  score: "scoring sets",
  partial: "partial history coverage",
  ready: "ready",
  deferred: "history deferred by load",
  error: "calc error",
};

function LaneProgress({ l }: { l: NonNullable<LiveStats["lanes"]>[number] }) {
  const pct = Math.max(0, Math.min(100, l.progressPct ?? 0));
  const starting = l.running && !l.progressReady && (!l.progressPhase || l.progressPhase === "idle" || pct <= 0);
  const phase = starting ? "starting" : String(l.progressPhase || "idle");
  const label = PROGRESS_PHASE_LABEL[phase] ?? phase;
  const updating = ["fetch", "replay", "score", "partial"].includes(phase);
  const busy = updating || !l.progressReady;
  const gate = l.progressReady ? (phase === "ready" ? "" : " · gate ready") : " · gate closed";
  const details: Array<[string, string]> = [];
  if (l.progressSymbol) details.push(["symbol", l.progressSymbol]);
  if (l.progressSymbolsTotal) details.push(["symbols", `${l.progressSymbolsDone ?? 0}/${l.progressSymbolsTotal}`]);
  if (l.progressSetsTotal) details.push(["sets", `${l.progressSetsDone ?? 0}/${l.progressSetsTotal}`]);
  if (l.progressBarsDone) details.push(["bars", `${l.progressBarsDone}${l.progressBarsTotal ? `/${l.progressBarsTotal}` : ""}`]);
  if (l.klinesReady != null && l.symbolCount) details.push(["klines", `${l.klinesReady}/${l.symbolCount}`]);
  if (l.progressElapsedMs) details.push(["elapsed", `${(l.progressElapsedMs / 1000).toFixed(1)}s`]);
  if (l.progressCycle) details.push(["cycle", String(l.progressCycle)]);
  return (
    <div className="mt-3" data-testid={`lane-progress-${l.type}`}>
      <div className="flex items-baseline justify-between gap-2 font-mono text-[10px] text-muted">
        <span className={l.progressError ? "text-danger" : ""}>{l.progressError ? "calc error" : label}{updating ? " · updating" : ""}{gate}</span>
        <span className="text-sm font-medium text-fg tabular-nums">{pct.toFixed(0)}%</span>
      </div>
      <div className="mt-1 h-2 overflow-hidden rounded-full bg-border">
        <div className={`h-full rounded-full bg-primary ${busy ? "animate-pulse" : ""}`} style={{ width: `${pct}%` }} />
      </div>
      {details.length ? (
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-muted">
          {details.map(([k, v]) => (
            <span key={k}><span className="text-fg/70">{k}</span> {v}</span>
          ))}
        </div>
      ) : null}
      {l.progressDetail ? <p className="mt-1 font-mono text-[10px] text-muted">{l.progressDetail}</p> : null}
      {l.progressError ? <p className="mt-1 font-mono text-[10px] text-danger">{l.progressError}</p> : null}
    </div>
  );
}

function LaneBoard({ stats }: { stats: LiveStats }) {
  const { setConn } = useConnection();
  return (
    <section className="grid gap-3 sm:grid-cols-2" data-testid="lane-board">
      {(stats.lanes ?? []).map((l) => {
        return (
          <article
            key={l.id}
            role="button"
            tabIndex={0}
            data-testid={`lane-${l.type}`}
            onClick={() => setConn(l.type as ConnType)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") setConn(l.type as ConnType);
            }}
            className="cursor-pointer rounded-radius border border-border bg-surface p-4 text-left"
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="font-mono text-xs tracking-wide text-muted uppercase">{l.label}</p>
                <p className="mt-1 text-lg font-medium">{l.exchange}</p>
              </div>
              <span className={`rounded-full px-2 py-0.5 font-mono text-xs ${l.paused ? "bg-bg2 text-muted" : l.running && !l.halted ? "bg-primary-dim text-primary" : "bg-danger/15 text-danger"}`}>
                {l.paused ? "pause" : l.halted ? "halt" : l.running ? "live" : "off"}
              </span>
            </div>
            <p className="mt-3 font-mono text-2xl tabular-nums">
              {l.unit === "VST" ? "" : "$"}
              {fmt(l.equity, 2)}
              {l.unit === "VST" ? " VST" : ""}
            </p>
            <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 font-mono text-xs text-muted">
              <dt>Real</dt>
              <dd className="text-right text-fg" title="Engine book — valid system entries">
                {l.openCount}
              </dd>
              <dt>Live</dt>
              <dd
                className={`text-right ${
                  typeof l.exchangeOpenCount === "number" &&
                  l.exchangeOpenCount >= 0 &&
                  l.exchangeOpenCount !== l.openCount
                    ? "text-danger"
                    : "text-fg"
                }`}
                title="Indeed live open on the exchange (reconcile)"
              >
                {typeof l.exchangeOpenCount === "number" && l.exchangeOpenCount >= 0
                  ? l.exchangeOpenCount
                  : "—"}
              </dd>
              <dt>Sim</dt>
              <dd
                className={`text-right ${(l.simOpenCount ?? 0) > 0 ? "text-warn" : "text-fg"}`}
                title="Simulated — Real positions not on the exchange; system-internal calcs (count · unrealized PnL)"
              >
                {typeof l.simOpenCount === "number" && l.simOpenCount >= 0
                  ? `${l.simOpenCount}${typeof l.simUPnl === "number" && l.simOpenCount > 0 ? ` · ${l.simUPnl >= 0 ? "+" : ""}${fmt(l.simUPnl, 2)}` : ""}`
                  : "—"}
              </dd>
              <dt>W / L</dt>
              <dd className="text-right text-fg">
                {l.wins} / {l.losses}
              </dd>
              <dt>Grow / Loss</dt>
              <dd className={`text-right ${pnlClass((l.systemPnl ?? l.sessionPnl) || 0)}`}>
                {fmt(l.systemGrow, 3)} / {fmt(l.systemLoss, 3)}
              </dd>
              <dt>System PnL</dt>
              <dd className={`text-right ${pnlClass((l.systemPnl ?? l.sessionPnl) || 0)}`}>
                {fmt(l.systemPnl ?? l.sessionPnl, 3)}
              </dd>
              <dt>PF</dt>
              <dd className="text-right text-fg">{l.pf >= 99 ? "∞" : l.pf.toFixed(2)}</dd>
              <dt>Scan</dt>
              <dd className="text-right text-fg">{fmt(l.hotMs ?? l.scanMs, 0)}ms</dd>
              <dt>SL+TP</dt>
              <dd className={`text-right ${(l.controlsMissing ?? 0) > 0 ? "text-danger" : "text-fg"}`}>
                {l.controlsOk ?? 0}/{l.openCount}
              </dd>
              <dt>Symbols</dt>
              <dd className="text-right text-fg">{l.symbolCount ?? "—"}</dd>
            </dl>
            <LaneProgress l={l} />
            {l.haltReason ? <p className="mt-2 text-xs text-danger">{l.haltReason}</p> : null}
          </article>
        );
      })}
    </section>
  );
}

function CoordStrip({ stats }: { stats: LiveStats | null }) {
  const c = stats?.coord;
  const axes = c?.axes ?? {};
  const gate = c?.gate;
  const allow = gate?.allow !== false;
  const pc = stats?.pfCost;
  const minPf = pc?.minPf ?? c?.minPf ?? 1.1;
  const cost = pc?.costPct ?? 0.15;
  return (
    <div className="mt-4 rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={allow ? "text-primary" : "text-danger"}>{allow ? "coord open" : "coord pause"}</span>
        <span className="text-muted">
          last15 {fmt(pc?.ratio ?? gate?.metrics?.last15Ratio, 2)} · R {fmt(pc?.avgR ?? gate?.metrics?.last15R, 2)} · min {fmt(minPf, 2)} · cost {fmt(cost, 2)}%
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-2 text-muted">
        {(["prev", "last", "cont", "pause"] as const).map((k) => {
          const a = axes[k];
          return (
            <span key={k} className={a?.enabled ? "text-fg" : "text-faint"}>
              {k} {a?.enabled ? a.max_window : "off"}
            </span>
          );
        })}
        <span>{c?.rearrange ? "rearr on" : "rearr off"}</span>
        <span className={pc?.pass === false ? "text-danger" : "text-primary"}>
          {pc?.pass === false ? "block new risk" : "PF pass"}
        </span>
      </div>
      {gate?.reasons?.length ? <p className="mt-1 text-danger">{gate.reasons.join(" · ")}</p> : null}
    </div>
  );
}

function ControlsStrip({ stats }: { stats: LiveStats | null }) {
  const c = stats?.coverage?.controls;
  const open = stats?.open ?? [];
  const miss = c?.missing ?? open.filter((p) => !p.controls).length;
  const sec = c?.security ?? open.filter((p) => p.secSlOid && p.secTpOid).length;
  const ok = c?.ok ?? open.filter((p) => p.controls).length;
  const n = c?.open ?? open.length;
  const gaps = open.filter((p) => !p.controls || !(p.secSlOid && p.secTpOid)).slice(0, 8);
  return (
    <div className="mt-3 rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs" data-testid="controls-strip">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={miss ? "text-danger" : "text-primary"}>
          controls · {ok}/{n} SL+TP · {sec} security
        </span>
        <span className="text-muted">missing {miss}</span>
      </div>
      {gaps.length ? (
        <p className="mt-1 text-danger">
          gap {gaps.map((p) => `${p.symbol.replace("-USDT", "")} ${p.side === "LONG" ? "L" : "S"}`).join(" · ")}
        </p>
      ) : (
        <p className="mt-1 text-muted">order SL+TP and symbol+direction security both sides</p>
      )}
    </div>
  );
}

function PacksStrip({ stats }: { stats: LiveStats | null }) {
  const p = (stats?.pulse ?? {}) as {
    stratIndications?: boolean;
    stratGeneral?: boolean;
    stratBlock?: boolean;
    stratTrailing?: boolean;
    dcaEnabled?: boolean;
    slMinPct?: number;
    slMaxPct?: number;
    tpMinPct?: number;
    tpMaxPct?: number;
    positionCostPct?: number;
    tpCostRatio?: number;
    slToTpRatio?: number;
  };
  const packs: [string, boolean][] = [
    ["indications", p.stratIndications !== false],
    ["general", p.stratGeneral !== false],
    ["block", p.stratBlock !== false],
    ["dca", Boolean(p.dcaEnabled) && stats?.dca?.enabled !== false],
    ["trailing", p.stratTrailing !== false],
  ];
  const tp = (p.positionCostPct ?? 0.15) * (p.tpCostRatio ?? 5);
  const sl = tp * (p.slToTpRatio ?? 0.6);
  return (
    <div className="mt-3 rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-primary">parallel packs</span>
        <span className="text-muted">
          SL:TP {fmt(p.slToTpRatio, 1)} · grid SL {fmt(sl, 2)}% TP {fmt(tp, 2)}%
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-2">
        {packs.map(([name, on]) => (
          <span key={name} className={on ? "text-fg" : "text-faint"}>
            {name} {on ? "on" : "off"}
          </span>
        ))}
      </div>
    </div>
  );
}

function SetsStrip({ stats }: { stats: LiveStats | null }) {
  const s = stats?.sets;
  const p = s?.progress;
  const rows = (s?.rows ?? []).slice(0, 8);
  const pct = Math.max(0, Math.min(100, p?.pct ?? 0));
  const phase = String(p?.phase ?? "idle");
  const updating = ["fetch", "replay", "score", "partial"].includes(phase);
  const gate = p?.ready ? (phase === "ready" ? "" : " · gate ready") : " · gate closed";
  const active = s?.activeCount ?? 0;
  const lanes = s?.lanes ?? [];
  return (
    <div className="mt-3 rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs" data-testid="sets-strip">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={p?.ready ? "text-primary" : "text-warn"}>
          sets · {p?.phase ?? "idle"} · valid {s?.validatedCount ?? 0}/{s?.setCount ?? 0} · active {active}/{s?.setCount ?? 0}
          {updating ? " · updating" : ""}{gate}
          {stats?.detailType ? ` · from ${stats.detailType}` : ""}
        </span>
        <span className="text-muted">
          last15 PF · max DDt · last{s?.deactN ?? 25} R · 1m×{s?.lookback ?? 480}
        </span>
      </div>
      {lanes.length > 1 ? (
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {lanes.map((ln) => {
            const lp = Math.max(0, Math.min(100, ln.progress?.pct ?? 0));
            const lanePhase = String(ln.progress?.phase ?? "idle");
            const laneUpdating = ["fetch", "replay", "score", "partial"].includes(lanePhase);
            const laneGate = ln.progress?.ready ? (lanePhase === "ready" ? "" : " · gate ready") : " · gate closed";
            return (
              <div key={ln.id || ln.type}>
                <div className="flex justify-between text-muted">
                  <span className={ln.running && !ln.halted ? "text-primary" : "text-faint"}>
                    {ln.type} valid {ln.validatedCount ?? 0}/{ln.setCount ?? 0} · active {ln.activeCount ?? 0}/{ln.setCount ?? 0}
                  </span>
                  <span>{ln.progress?.phase ?? "idle"}{laneUpdating ? " · updating" : ""}{laneGate} {fmt(lp, 0)}%</span>
                </div>
                <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-border">
                  <div className="h-full rounded-full bg-primary" style={{ width: `${lp}%` }} />
                </div>
                <p className="mt-0.5 text-[10px] text-muted">{ln.progress?.detail || ""}</p>
              </div>
            );
          })}
        </div>
      ) : (
        <>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
            <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${pct}%` }} />
          </div>
          <p className="mt-1 text-muted">
            {p?.detail || "prehistoric 1m replay"} {p?.symbol ? `· ${p.symbol.replace("-USDT", "")}` : ""} · {fmt(p?.lastRunMs, 0)}ms
            {p?.symbolsTotal ? ` · history ${p.symbolsDone ?? 0}/${p.symbolsTotal}` : ""}
            {updating && p?.ready ? " · prior gate remains active" : ""}
          </p>
        </>
      )}
      {rows.length ? (
        <div className="mt-2 grid gap-1 sm:grid-cols-2">
          {rows.map((r) => (
            <div key={r.id} className="flex items-center justify-between gap-2">
              <span className={r.active ? "text-fg" : "text-faint"}>
                {r.pack?.slice(0, 3) || r.id.slice(0, 8)} sl{Number(r.slRatio || 0).toFixed(1)} st{r.step ?? "—"}
              </span>
              <span className={r.active ? "text-primary" : "text-danger"}>
                {r.last15Ratio.toFixed(2)} · {formatDuration(r.maxDdS * 1000)} · R{r.last25AvgR.toFixed(1)}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      {p?.error ? <p className="mt-1 text-danger">{p.error}</p> : null}
    </div>
  );
}

function ExitStrip({ stats }: { stats: LiveStats | null }) {
  const ex = stats?.exits;
  const lanes = ex?.lanes ?? [];
  return (
    <div className="mt-3 rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs" data-testid="exit-strip">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={ex?.enabled === false ? "text-faint" : "text-primary"}>
          exits · {ex?.ignoreTp === false ? "TP on" : "SL takes profit"} · pick {ex?.lastPick ?? "—"}
        </span>
        <span className="text-muted">opt SL {fmt(ex?.optSlPct, 2)}% from peak</span>
      </div>
      <div className="mt-1 flex flex-wrap gap-2">
        {(["lock", "peak", "rev", "time", "hard"] as const).map((k) => {
          const ln = lanes.find((l) => l.key === k);
          const on = k === "hard" ? true : k === "lock" ? ex?.lockOn !== false : k === "peak" ? ex?.peakOn !== false : k === "rev" ? ex?.revOn !== false : ex?.timeOn !== false;
          return (
            <span key={k} className={ln?.active === false || !on ? "text-faint" : ln?.selected ? "text-primary" : "text-fg"}>
              {k} {ln && ln.n > 0 ? ln.last15Ratio.toFixed(2) : on ? "on" : "off"}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function WorkStrip({ stats }: { stats: LiveStats | null }) {
  const fails = (stats?.tests ?? []).filter((t) => !t.pass);
  const last = (stats?.signals ?? []).slice(0, 3);
  return (
    <div className="mt-3 rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs" data-testid="work-strip">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={fails.length ? "text-danger" : "text-primary"}>
          work · cycle {stats?.cycle ?? "—"} · {fmt(stats?.activityPerMin, 1)}/min
          {stats?.detailType ? ` · ${stats.detailType}` : ""}
        </span>
        <span className="text-muted">
          err {stats?.errors ?? 0} · rss {fmt((stats as LiveStats & { rssMb?: number })?.rssMb, 0)}MB
        </span>
      </div>
      {stats?.haltReason ? <p className="mt-1 text-danger">{stats.haltReason}</p> : null}
      {stats?.lastError ? <p className="mt-1 text-danger">{stats.lastError}</p> : null}
      {fails.length ? (
        <p className="mt-1 text-danger">
          fail {fails.map((t) => `${t.name}${t.detail ? ` (${t.detail})` : ""}`).join(" · ")}
        </p>
      ) : (
        <p className="mt-1 text-muted">in-process tests holding</p>
      )}
      {last.length ? (
        <div className="mt-1 flex flex-wrap gap-2 text-muted">
          {last.map((s, i) => (
            <span key={i}>
              {String(s.symbol ?? "").replace("-USDT", "")} {String(s.side ?? s.reason ?? "")}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function EngineStrip({ stats }: { stats: LiveStats | null }) {
  const e = stats?.engine;
  const api = stats?.api;
  if (!e && !api) return null;
  const qa = `${e?.qaPass ?? 0}P / ${e?.qaFail ?? 0}F`;
  const load = e?.load;
  const lv = load?.level ?? "normal";
  const lvCls =
    lv === "critical" || lv === "overload" ? "text-danger" : lv === "busy" ? "text-warn" : "text-primary";
  return (
    <div className="mt-3 rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={(e?.hotMs ?? 99) <= (e?.cycleMs ?? 200) + 40 && !e?.cycleOverrun ? "text-primary" : "text-danger"}>async engine</span>
        <span className="text-muted">{qa}</span>
      </div>
      <div className="mt-1 flex flex-wrap gap-3 text-muted">
        <span>hot {fmt(e?.hotMs, 0)}ms</span>
        <span>cycle {fmt(e?.cycleMs ?? (e?.scanS ?? 0.2) * 1000, 0)}ms</span>
        <span>wait {fmt(e?.cycleWaitMs, 0)}ms</span>
        <span>{e?.cycleOverrun ? "overrun" : "on beat"}</span>
        <span>warm {fmt(e?.warmMs, 0)}ms</span>
        <span>p50 {fmt(e?.asyncP50 ?? api?.asyncP50, 0)}ms</span>
        <span>ws {api?.wsOk ? "ok" : "gap"} {fmt(api?.wsAgeMs, 0)}ms</span>
        <span>id {e?.trackPrefix ?? "G"}</span>
        <span>foreign {e?.ignoredForeign ?? 0}</span>
        <span>vol ×{fmt(stats?.volumeFactor ?? (stats as LiveStats & { pulse?: { volumeFactor?: number } })?.pulse?.volumeFactor, 2)}</span>
        <span>notional {fmt(stats?.targetNotional, 2)}</span>
        <span>1m {fmt(e?.tfReady?.["1m"], 0)}</span>
        <span>5m {fmt(e?.tfReady?.["5m"], 0)}</span>
        <span>15m {fmt(e?.tfReady?.["15m"], 0)}</span>
      </div>
      <div className="mt-1 flex flex-wrap gap-3" data-testid="load-strip">
        <span className={lvCls}>
          load {lv} · chunk {load?.scanChunk ?? e?.scanChunk ?? "—"} · rss {fmt(load?.rssMb ?? (stats as LiveStats & { rssMb?: number })?.rssMb, 0)}MB
        </span>
        <span className="text-muted">
          hist {load?.histChunk ?? "—"} · trim {load?.trimmed ?? 0} · gc {load?.gcN ?? 0}
          {load?.shed?.length ? ` · shed ${load.shed.join(",")}` : ""}
        </span>
      </div>
    </div>
  );
}

function VariantsStrip({ stats }: { stats: LiveStats | null }) {
  const v = stats?.variants;
  const pulse = (stats?.pulse ?? {}) as { slToTpRatio?: number; trailArmPct?: number; trailGivePct?: number; tf1m?: boolean; tf5m?: boolean; tf15m?: boolean; tfCombined?: boolean };
  const sl = v?.slRatio ?? pulse.slToTpRatio ?? 0.6;
  const trail = v?.trailKey ?? `${fmt(pulse.trailArmPct, 1)}:${fmt(pulse.trailGivePct, 1)}`;
  const scores = v?.slScores ?? [];
  return (
    <div className="mt-3 rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-primary">SL:TP · trail</span>
        <span className="text-muted">
          {v?.slAuto ? "sl auto" : "sl lock"} {v?.slPick ?? "—"} · {v?.trailAuto ? "tr auto" : "tr lock"} {v?.trailPick ?? "—"}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {[0.3, 0.6, 0.9, 1.2, 1.5].map((r) => {
          const sc = scores.find((s) => Math.abs(Number(s.key) - r) < 1e-9);
          const on = Math.abs(sl - r) < 1e-9;
          return (
            <span
              key={r}
              className={`rounded-md border px-2 py-1 ${on ? "border-primary text-fg" : "border-border text-muted"}`}
            >
              {r.toFixed(1)}
              {sc && sc.n > 0 ? ` n${sc.n}` : ""}
            </span>
          );
        })}
        <span className="rounded-md border border-border px-2 py-1 text-fg">tr {trail}</span>
      </div>
      <div className="mt-1 flex flex-wrap gap-2 text-muted">
        <span className={pulse.tf1m === false ? "text-faint" : "text-fg"}>1m</span>
        <span className={pulse.tf5m === false ? "text-faint" : "text-fg"}>5m</span>
        <span className={pulse.tf15m === false ? "text-faint" : "text-fg"}>15m</span>
        <span className={pulse.tfCombined === false ? "text-faint" : "text-primary"}>combined</span>
      </div>
    </div>
  );
}

function IndicationStrip({ stats }: { stats: LiveStats | null }) {
  const ind = stats?.indications;
  const rows = ind?.primary ?? [];
  const kinds = ind?.kindStats || {};
  const samples = ind?.samples ?? [];
  return (
    <div className="mt-3 rounded-xl border border-border bg-bg2 px-3 py-2">
      <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-xs">
        <span className={ind?.enabled ? "text-primary" : "text-faint"}>
          {ind?.enabled ? "indications" : "indications off"}
        </span>
        <span className="text-muted">
          min {ind?.minSources ?? 3} · agr {fmt(ind?.minAgreement, 2)}
          {ind?.tfCombined !== false ? " · tf combined" : ""}
          {ind?.extraSources ? " · extra venues" : ""}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-2 font-mono text-[11px]">
        {(["state", "signals", "active", "direction", "move", "common"] as const).map((k) => {
          const row = kinds[k];
          const on = ind?.types?.[k] !== false;
          return (
            <span key={k} className={on ? "text-fg" : "text-faint"}>
              {k} {on ? row?.hits ?? 0 : "off"}
              {row?.long || row?.short ? ` L${row.long ?? 0}/S${row.short ?? 0}` : ""}
            </span>
          );
        })}
      </div>
      {rows.length === 0 && samples.length === 0 ? (
        <p className="mt-2 text-xs text-muted">No consensus yet — waiting on independent 1/5/15 lanes</p>
      ) : (
        <div className="mt-2 grid gap-1 sm:grid-cols-2">
          {samples.slice(0, 6).map((s) => (
            <div key={s.symbol} className="font-mono text-xs">
              <span className="text-fg">{s.symbol.replace("-USDT", "")}</span>
              <span className="ml-2 text-muted">
                {Object.entries(s.kinds || {})
                  .map(([k, v]) => `${k[0]}${v.dir === "short" ? "−" : "+"}`)
                  .join(" ")}
              </span>
            </div>
          ))}
          {samples.length === 0
            ? rows.slice(0, 8).map((r) => {
                const tag =
                  r.mode === "tf_combined"
                    ? "tf"
                    : r.mode === "multi_source_consensus"
                      ? "cons"
                      : r.timeframe || "src";
                return (
                  <div key={r.symbol} className="flex items-center justify-between gap-2 font-mono text-xs">
                    <span className="text-fg">
                      {r.symbol.replace("-USDT", "")}{" "}
                      <span className={r.direction === "long" ? "text-primary" : "text-danger"}>
                        {r.direction}
                      </span>
                    </span>
                    <span className="text-muted tabular-nums">
                      {tag} · {fmt(r.agreement, 2)} · {fmt(r.confidence, 2)}
                    </span>
                  </div>
                );
              })
            : null}
        </div>
      )}
    </div>
  );
}

function Panel({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-radius border border-border bg-surface p-4">
      <div className="mb-3 flex items-center justify-between text-muted">
        <h2 className="text-sm font-medium tracking-wide uppercase">{title}</h2>
        <span className="text-faint">{icon}</span>
      </div>
      {children}
    </section>
  );
}

function SideChip({ side, compact }: { side: string; compact?: boolean }) {
  const long = side === "LONG";
  const Icon = long ? ArrowUpRight : ArrowDownRight;
  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded-full px-2 py-0.5 font-mono text-xs font-medium ${
        long ? "bg-primary-dim/40 text-primary" : "bg-danger/15 text-danger"
      }`}
    >
      <Icon className="size-3" />
      {compact ? (long ? "L" : "S") : side}
    </span>
  );
}

function fmt(n: number | null | undefined, d = 2) {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(d);
}

function fmtPx(n: number | null | undefined) {
  if (n == null || !n) return "—";
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(4);
  return n.toFixed(6);
}

function pnlClass(n: number) {
  if (n > 0) return "text-primary";
  if (n < 0) return "text-danger";
  return "text-muted";
}

function ago(s: number) {
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}
