import { createFileRoute } from "@tanstack/react-router";
import { lazy, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import { DeskShell } from "@/components/desk-shell";
import { useConnection } from "@/components/connection-provider";
import { fetchLiveStats, pickView, deskPollMs, statsUnchanged, posOrdersCounts, type LiveClosed, type LiveStats } from "@/lib/live-stats";
import { startPolling } from "@/lib/polling";
import { SystemHealthFooter } from "@/components/system-health";
import { derive } from "@/lib/derive-stats";
import { buildOverview, formatDuration } from "@/lib/analytics";
import { StatsOverview } from "@/components/stats-overview";
import { CoveragePanel } from "@/components/coverage-overview";
import { IndicationKindsPanel, StrategyStatsPanel } from "@/components/kind-strategy-stats";
import { ActivityPanel } from "@/components/activity-overview";
import { EquityArea, SymbolBars, TradeBars } from "@/components/visual-stats";
import type { EvaluationWindow } from "@/lib/hist-calc";
import { ForcedConfigsPanel } from "@/components/forced-configs";
import { ComboEvalPanel } from "@/components/combo-eval-panel";
import { SetGroups } from "@/components/set-groups";
import { enabledAxes, setMetric, setRowKey, type SetOverviewRow } from "@/lib/set-overview";
import { SetIdentity } from "@/components/set-identity";
import { pnlClass, pfClass, activeClass, sideChipClass, haltClass, isBenignError } from "@/lib/status-tone";

type ResultTab = "overview" | "coverage" | "indications" | "strategies" | "sets" | "controls" | "errors" | "tests" | "report";

const DimensionStats = lazy(() => import("@/components/dimension-stats"));

const RESULT_TABS: Array<{ id: ResultTab; label: string; hint: string }> = [
  { id: "overview", label: "Overview", hint: "equity, tape and headline metrics" },
  { id: "coverage", label: "Coverage", hint: "symbols, sets and processing coverage" },
  { id: "indications", label: "Indications", hint: "all indication kinds and sides" },
  { id: "strategies", label: "Strategies", hint: "Block, DCA, exits and strategy lanes" },
  { id: "sets", label: "Sets", hint: "independent configurations and validation" },
  { id: "tests", label: "Tests", hint: "forced baseline configurations and VST evidence" },
  { id: "controls", label: "Controls", hint: "exchange actions and protection parity" },
  { id: "errors", label: "Errors", hint: "recorded failures and rejected actions" },
  { id: "report", label: "HTML report", hint: "standalone stats report viewer" },
];

export const Route = createFileRoute("/results")({ component: ResultsPage });

function ResultsPage() {
  const { conn } = useConnection();
  const [raw, setRaw] = useState<LiveStats | null>(null);
  const [statsTab, setStatsTab] = useState<ResultTab>("overview");
  const rawRef = useRef<LiveStats | null>(null);
  useEffect(() => {
    setRaw(null);
    rawRef.current = null;
    setStatsTab("overview");
    const poll = startPolling(async (signal) => {
      const s = await fetchLiveStats(conn, signal);
      if (signal.aborted) return;
      if (s && !statsUnchanged(rawRef.current, s)) {
        rawRef.current = s;
        setRaw(s);
      }
    }, () => deskPollMs(rawRef.current, document.hidden));
    return poll.stop;
  }, [conn]);
  const stats = pickView(raw, conn);
  const d = useMemo(() => derive(stats), [stats]);
  const overview = useMemo(
    () =>
      buildOverview(
        (stats?.closed ?? []).map((c) => ({ pnl: c.pnl, t: c.t, symbol: c.symbol, pnl_pct: c.pnl_pct })),
        Date.now(),
        stats?.pfCost?.costPct ?? stats?.positionCost?.effectivePct ?? 0.10,
      ),
    [stats],
  );
  const closed = stats?.closed ?? [];
  const closedN = Math.max(
    Number(stats?.closedN || 0),
    Number(stats?.wins || 0) + Number(stats?.losses || 0),
    closed.length,
  );
  const gp = Number(
    stats?.systemGrow ??
      (stats?.systemGrowLive != null || stats?.systemGrowVst != null
        ? Number(stats?.systemGrowLive || 0) + Number(stats?.systemGrowVst || 0)
        : d.gp),
  );
  const gl = Number(
    stats?.systemLoss ??
      (stats?.systemLossLive != null || stats?.systemLossVst != null
        ? Number(stats?.systemLossLive || 0) + Number(stats?.systemLossVst || 0)
        : d.gl),
  );

  return (
    <DeskShell
      live={stats ? Boolean(stats.running && !stats.halted && !stats.paused) : undefined}
      mode={stats?.paused ? "PAUSED" : stats?.halted ? "HALTED" : stats?.mode}
      paused={stats ? Boolean(stats.paused || stats.haltReason === "paused") : undefined}
      halted={stats ? Boolean(stats.halted) : undefined}
      alive={stats ? stats.alive !== false : undefined}
      statsType={stats?.connType}
      statsId={stats?.connection}
    >
      <p className="min-w-0 font-mono text-[11px] tracking-wide text-muted uppercase [overflow-wrap:anywhere]" data-testid="results-identity">
        {stats?.connType || conn} · {stats?.connection || conn} · {stats?.unit || ""} · Pos R {posOrdersCounts(stats).realPositions ?? "—"} L {posOrdersCounts(stats).livePositions ?? "—"} · Ord R {posOrdersCounts(stats).realOrders ?? "—"} L {posOrdersCounts(stats).liveOrders ?? "—"} · {closedN} closed
      </p>
      <StatsOverview data={overview} live={stats} />

      <div className="flex flex-wrap gap-2" data-testid="results-export-actions">
        <a
          href={`/results-export.html?conn=${encodeURIComponent(conn)}`}
          target="_blank"
          rel="noreferrer"
          className="inline-flex min-h-11 items-center rounded-lg bg-primary px-4 text-sm font-medium text-bg"
        >
          Open HTML report
        </a>
        <a
          href={`/results-export.html?conn=${encodeURIComponent(conn)}`}
          download={`pulse-results-${conn}.html`}
          className="inline-flex min-h-11 items-center rounded-lg border border-border px-4 text-sm"
        >
          Download HTML
        </a>
        <a
          href={`/results-export.json?conn=${encodeURIComponent(conn)}`}
          download={`pulse-results-${conn}.json`}
          className="inline-flex min-h-11 items-center rounded-lg border border-border px-4 text-sm"
        >
          Download JSON
        </a>
        <a
          href={`/results-export.md?conn=${encodeURIComponent(conn)}`}
          download={`pulse-results-${conn}.md`}
          className="inline-flex min-h-11 items-center rounded-lg border border-border px-4 text-sm"
        >
          Download Markdown
        </a>
      </div>

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Hero k="Trades" v={String(closedN)} s={`${d.longs}L / ${d.shorts}S · tape ${closed.length}`} />
        <Hero k="Gross profit" v={`+${gp.toFixed(4)}`} s={`avg win ${d.avgWin.toFixed(4)}`} good />
        <Hero k="Gross loss" v={gl ? `-${Math.abs(gl).toFixed(4)}` : "0"} s={`avg loss ${d.avgLoss.toFixed(4)}`} />
        <Hero
          k="PF after cost"
          v={(stats?.pfCost?.ratio ?? stats?.profitFactor ?? 1).toFixed(2)}
          s={`1.00=neutral · 1.10=+1×cost · min ${stats?.pfCost?.minPf ?? 1.1} · ${stats?.pfCost?.pass ? "pass" : "gate"}`}
        />
      </section>

      <EvaluationWindowsStrip stats={stats} />
      <ResultsTabs value={statsTab} onChange={setStatsTab} stats={stats} />
      {statsTab === "tests" ? <div id="results-panel-tests" role="tabpanel"><ForcedConfigsPanel live={stats?.forcedConfigs} /></div> : null}

      {statsTab === "coverage" ? <div id="results-panel-coverage" role="tabpanel"><CoveragePanel live={stats} /></div> : null}
      {statsTab === "indications" ? <div id="results-panel-indications" className="min-w-0 grid gap-3" role="tabpanel">
        <Suspense fallback={<p className="p-4 text-sm text-muted">Loading indication diagrams…</p>}><DimensionStats stats={stats} focus="indications" /></Suspense>
        <IndicationKindsPanel stats={stats} />
      </div> : null}
      {statsTab === "strategies" ? (
        <div id="results-panel-strategies" className="min-w-0 grid gap-3" role="tabpanel">
          <Suspense fallback={<p className="p-4 text-sm text-muted">Loading strategy diagrams…</p>}><DimensionStats stats={stats} focus="strategies" /></Suspense>
          <StrategyStatsPanel stats={stats} />
          <ComboEvalPanel job={stats} />
          <ExitResults stats={stats} />
          <BlockResults stats={stats} />
          <DcaResults stats={stats} />
        </div>
      ) : null}
      {statsTab === "sets" ? (
        <div id="results-panel-sets" className="min-w-0 grid gap-3" role="tabpanel">
          <InternResults stats={stats} />
          <SetResults stats={stats} />
        </div>
      ) : null}
      {statsTab === "controls" ? (
        <div id="results-panel-controls" className="min-w-0 grid gap-3" role="tabpanel">
          <ControlHealthPanel stats={stats} />
          <ActivityPanel stats={stats} />
        </div>
      ) : null}
      {statsTab === "errors" ? <ErrorsPanel stats={stats} /> : null}
      {statsTab === "report" ? <HtmlReportPanel conn={conn} /> : null}

      {statsTab === "overview" ? (
        <div id="results-panel-overview" className="min-w-0 grid gap-3" role="tabpanel">
          <section className="min-w-0 grid gap-3 lg:grid-cols-2">
            <Card title="Equity curve">
              <EquityArea data={d.equityCurve} />
            </Card>
            <Card title="Per-trade PnL">
              <TradeBars data={d.tradeBars} />
            </Card>
          </section>

          <section className="min-w-0 grid gap-3 lg:grid-cols-2">
            <Card title="By symbol">
              <SymbolBars data={d.bySymbol} />
            </Card>
            <Card title="Exit reasons">
              {d.byReason.length === 0 ? (
                <p className="flex h-52 items-center justify-center text-sm text-muted">No exits yet</p>
              ) : (
                <ul className="space-y-3">
                  {d.byReason.map((r) => {
                    const max = Math.max(...d.byReason.map((x) => x.n), 1);
                    return (
                      <li key={r.reason}>
                        <div className="mb-1 flex justify-between text-sm">
                          <span>{r.reason}</span>
                          <span className={`font-mono tabular-nums ${pnlClass(r.pnl)}`}>
                            {r.n} · {r.pnl >= 0 ? "+" : ""}
                            {r.pnl.toFixed(4)}
                          </span>
                        </div>
                        <div className="h-1.5 overflow-hidden rounded-full bg-bg2">
                          <div
                            className="h-full rounded-full bg-primary-dim"
                            style={{ width: `${(r.n / max) * 100}%` }}
                          />
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </Card>
          </section>

          <ClosedTape rows={closed} />
        </div>
      ) : null}
      <SystemHealthFooter conn={conn} />
    </DeskShell>
  );
}

function HtmlReportPanel({ conn }: { conn: string }) {
  const href = `/results-export.html?conn=${encodeURIComponent(conn)}`;
  return (
    <section id="results-panel-report" className="rounded-radius border border-border bg-surface p-4" data-testid="html-report-panel" role="tabpanel">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Canonical HTML stats report</h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Read-only report generated from the same position-cost, PF, DDT, set, indication, strategy, and coverage snapshot as the exports.
          </p>
        </div>
        <a
          href={href}
          target="_blank"
          rel="noreferrer"
          className="inline-flex min-h-11 items-center rounded-lg border border-border px-3 text-sm"
        >
          Open in new tab
        </a>
      </div>
      <iframe
        title={`HTML stats report for ${conn}`}
        src={href}
        loading="lazy"
        referrerPolicy="no-referrer"
        className="mt-4 h-[640px] w-full rounded-lg border border-border bg-bg sm:h-[760px]"
      />
    </section>
  );
}

const EVALUATION_WINDOW_KEYS = ["last5", "last10", "last15", "last25", "last50", "last75"] as const;

function EvaluationWindowsStrip({ stats }: { stats: LiveStats | null }) {
  const windows = (stats?.pfCost?.evaluationWindows ?? stats?.sets?.liveOverview?.evaluationWindows ?? {}) as Record<
    string,
    EvaluationWindow
  >;
  const sets = stats?.sets;
  return (
    <section className="rounded-radius border border-border bg-surface p-4" data-testid="evaluation-windows">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Evaluation windows · cost-net PF</h2>
          <p className="mt-1 text-xs text-muted">Independent recent-position checks; a window is valid only when its requested sample is available.</p>
        </div>
        <span className="font-mono text-xs text-muted">
          Sets valid {sets?.validatedCount ?? 0}/{sets?.setCount ?? 0} · active {sets?.activeCount ?? 0}/{sets?.setCount ?? 0}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {EVALUATION_WINDOW_KEYS.map((key) => {
          const metric = windows[key] ?? {};
          const requested = Number(metric.requestedN ?? Number(key.replace("last", "")));
          const n = Number(metric.n ?? 0);
          const available = metric.available ?? n >= requested;
          const validated = Boolean(metric.validated && available);
          const tone = validated ? "text-primary" : available ? "text-warn" : "text-muted";
          return (
            <div key={key} className="rounded-lg border border-border bg-bg2 px-3 py-2">
              <div className="flex items-center justify-between gap-2 font-mono text-[11px] text-muted">
                <span>{key.replace("last", "Last ")}</span>
                <span>{n}/{requested}</span>
              </div>
              <div className={`mt-1 font-mono text-lg tabular-nums ${tone}`}>
                {Number.isFinite(Number(metric.pf)) ? Number(metric.pf).toFixed(2) : "—"}
              </div>
              <div className={`text-[10px] uppercase ${tone}`}>{validated ? "valid" : available ? "review" : "waiting"}</div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function ResultsTabs({
  value,
  onChange,
  stats,
}: {
  value: ResultTab;
  onChange: (value: ResultTab) => void;
  stats: LiveStats | null;
}) {
  const failedTests = stats?.tests?.filter((test) => !test.pass).length ?? 0;
  const errorCount = Math.max(Number(stats?.errors ?? 0), Number(stats?.activity?.errorCount ?? 0), failedTests);
  return (
    <nav className="overflow-x-auto rounded-radius border border-border bg-bg2 p-1" aria-label="Results sections" role="tablist">
      <div className="flex min-w-max gap-1">
        {RESULT_TABS.map((tab) => {
          const selected = tab.id === value;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={`results-panel-${tab.id}`}
              data-testid={`results-tab-${tab.id}`}
              title={tab.hint}
              onClick={() => onChange(tab.id)}
              className={`min-h-11 rounded-lg px-3 text-left text-xs transition-colors ${
                selected ? "bg-surface text-fg shadow-sm" : "text-muted hover:bg-surface/70 hover:text-fg"
              }`}
            >
              <span className="block font-medium">{tab.label}{tab.id === "errors" && errorCount ? ` · ${errorCount}` : ""}</span>
              <span className="hidden text-[10px] text-muted lg:block">{tab.hint}</span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}

function ControlHealthPanel({ stats }: { stats: LiveStats | null }) {
  const controls = stats?.coverage?.controls;
  const open = Number(controls?.open ?? stats?.openCount ?? 0);
  const protectedCount = Number(controls?.ok ?? stats?.open?.filter((position) => position.controls).length ?? 0);
  const missing = Number(controls?.missing ?? Math.max(0, open - protectedCount));
  const mode = controls?.mode ?? ((stats?.pulse as { controlOrdersPerConfig?: unknown } | undefined)?.controlOrdersPerConfig === false ? "aggregate" : "per-config");
  return (
    <section className="min-w-0 rounded-radius border border-border bg-surface p-4" data-testid="control-health-panel">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Exchange control health</h2>
          <p className="mt-1 text-xs text-muted">Protection orders are reconciled independently from foreign positions and orders.</p>
        </div>
        <span className="font-mono text-xs text-muted">mode {mode}</span>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="col-span-2 min-w-0 rounded-lg border border-border bg-bg2 px-3 py-2">
          <div className="font-mono text-[10px] tracking-wide text-muted uppercase">Positions/Orders</div>
          <div className="mt-1 flex flex-wrap gap-x-4 font-mono text-sm tabular-nums">
            <span className="whitespace-nowrap">Pos R {posOrdersCounts(stats).realPositions ?? "—"} L {posOrdersCounts(stats).livePositions ?? "—"}</span>
            <span className="whitespace-nowrap">Ord R {posOrdersCounts(stats).realOrders ?? "—"} L {posOrdersCounts(stats).liveOrders ?? "—"}</span>
          </div>
        </div>
        <HealthMetric label="Open groups" value={open} />
        <HealthMetric label="SL + TP protected" value={`${protectedCount}/${open}`} good={missing === 0} problem={missing > 0} />
        <HealthMetric label="Control groups" value={Number(controls?.groupCount ?? controls?.groups?.length ?? 0)} />
        <HealthMetric label="Reconciliation" value={stats?.coverage?.recon?.pending ? "pending" : stats?.coverage?.recon?.ok === false ? "review" : "ok"} good={stats?.coverage?.recon?.ok !== false && !stats?.coverage?.recon?.pending} problem={stats?.coverage?.recon?.ok === false} />
      </div>
      {missing > 0 ? <p className="mt-3 text-xs text-danger">{missing} protection group{missing === 1 ? "" : "s"} missing SL/TP — control loop will attach.</p> : null}
    </section>
  );
}

function HealthMetric({ label, value, good, problem }: { label: string; value: number | string; good?: boolean; problem?: boolean }) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-bg2 px-3 py-2">
      <div className="font-mono text-[10px] leading-tight tracking-wide text-muted uppercase">{label}</div>
      <div className={`mt-1 truncate font-mono text-lg tabular-nums ${problem ? "text-danger" : good === true ? "text-primary" : ""}`} title={String(value)}>{value}</div>
    </div>
  );
}

function ErrorsPanel({ stats }: { stats: LiveStats | null }) {
  const failedTests = (stats?.tests ?? []).filter((test) => !test.pass);
  const events = [...(stats?.activity?.tail ?? stats?.events ?? [])]
    .filter((event) => {
      const type = String(event.event_type ?? "").toLowerCase();
      const status = String(event.status ?? "").toLowerCase();
      return type === "error" || type === "rejected" || ["error", "rejected", "discrepant"].includes(status);
    })
    .slice(0, 24);
  const lastError = isBenignError(stats?.lastError) ? "" : String(stats?.lastError || "");
  const haltProblem = Boolean(stats?.halted) && !isBenignError(stats?.haltReason) && !String(stats?.haltReason || "").toLowerCase().includes("below min");
  const hasErrors = Boolean(lastError) || failedTests.length > 0 || events.length > 0 || Number(stats?.errors ?? 0) > 0 || haltProblem;
  return (
    <section id="results-panel-errors" className="rounded-radius border border-border bg-surface p-4" data-testid="errors-panel" role="tabpanel">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Recorded errors and rejected actions</h2>
          <p className="mt-1 text-xs text-muted">Red is reserved for faults. Equity halt and already-flat closes stay notices.</p>
        </div>
        <span className={hasErrors ? "font-mono text-xs text-danger" : "font-mono text-xs text-primary"}>
          {hasErrors ? "faults recorded" : "no recorded errors"}
        </span>
      </div>
      {stats?.haltReason ? <p className={`mb-3 text-sm ${haltClass(stats.haltReason, stats.halted)}`}>{stats.haltReason}</p> : null}
      {lastError ? <div className="mb-3 rounded-lg border border-border bg-bg2 p-3 text-sm text-danger"><span className="text-muted">Latest:</span> {lastError}</div> : null}
      {failedTests.length ? (
        <div className="mb-3 space-y-2">
          {failedTests.map((test) => <div key={`${test.connection || stats?.connection}:${test.name}`} className="rounded-lg border border-border bg-bg2 p-3 font-mono text-xs break-words"><span className="text-danger">{test.connection ? `${test.connection.replace("bingx-", "")} · ` : ""}{test.name}</span> · {test.detail}</div>)}
        </div>
      ) : null}
      {events.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[680px] text-left text-xs">
            <thead className="font-mono text-muted"><tr><th className="pb-2 font-medium">Time</th><th className="pb-2 font-medium">Event</th><th className="pb-2 font-medium">Status</th><th className="pb-2 font-medium">Code</th><th className="pb-2 font-medium">Detail</th></tr></thead>
            <tbody>
              {events.map((event, index) => (
                <tr key={event.event_id || `${event.ts}-${index}`} className="border-t border-border font-mono">
                  <td className="py-2 text-muted">{event.ts ? new Date(event.ts * 1000).toLocaleTimeString() : "—"}</td>
                  <td className="py-2">{String(event.event_type || "event").replaceAll("_", " ")}</td>
                  <td className="py-2 text-warn">{event.status || "—"}</td>
                  <td className="py-2 text-muted">{event.code || event.order_id || "—"}</td>
                  <td className="max-w-[360px] truncate py-2 text-muted">{event.detail || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {!hasErrors ? <p className="rounded-lg border border-border bg-bg2 p-6 text-center text-sm text-muted">No recorded errors. Healthy state and notices remain visible in Coverage, Controls, and Activity.</p> : null}
    </section>
  );
}

function ClosedTape({ rows }: { rows: LiveClosed[] }) {
  return (
    <Card title="Closed tape">
      <div className="overflow-x-auto">
        <table className="w-full min-w-3xl text-left text-sm">
          <thead className="font-mono text-xs text-muted">
            <tr>
              <th className="pb-2 font-medium">Time</th>
              <th className="pb-2 font-medium">Sym</th>
              <th className="pb-2 font-medium">Side</th>
              <th className="pb-2 font-medium">Route</th>
              <th className="pb-2 font-medium">PnL</th>
              <th className="pb-2 font-medium">Hold</th>
              <th className="pb-2 font-medium">Why</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr><td colSpan={7} className="py-8 text-center text-muted">Closed fills stream here</td></tr>
            ) : rows.map((c, i) => (
              <tr key={`${c.t}-${i}`} className="border-t border-border">
                <td className="py-2.5 font-mono text-xs text-muted">{new Date(c.t * 1000).toLocaleTimeString()}</td>
                <td className="py-2.5 font-medium">{c.symbol.replace("-USDT", "")}</td>
                <td className="py-2.5">
                  <span className={`inline-flex items-center gap-0.5 rounded-full px-2 py-0.5 font-mono text-xs ${sideChipClass(c.side)}`}>
                    {c.side === "LONG" ? <ArrowUpRight className="size-3" /> : <ArrowDownRight className="size-3" />}
                    {c.side}
                  </span>
                </td>
                <td className="py-2.5 font-mono text-xs">{c.entry.toPrecision(5)} → {c.exit.toPrecision(5)}</td>
                <td className={`py-2.5 font-mono tabular-nums ${pnlClass(c.pnl)}`}>
                  {c.pnl >= 0 ? "+" : ""}{c.pnl.toFixed(4)}<span className="ml-1 text-faint">({(c.pnl_pct * 100).toFixed(3)}%)</span>
                </td>
                <td className="py-2.5 font-mono text-muted">{c.hold_s.toFixed(0)}s</td>
                <td className="py-2.5 text-xs text-muted">{c.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Hero({ k, v, s, good, bad }: { k: string; v: string; s: string; good?: boolean; bad?: boolean }) {
  return (
    <div className="rounded-radius border border-border bg-surface p-4">
      <div className="font-mono text-xs tracking-wide text-muted uppercase">{k}</div>
      <div
        className={`mt-1 font-mono text-2xl font-medium tabular-nums ${
          good ? "text-primary" : bad ? "text-danger" : ""
        }`}
      >
        {v}
      </div>
      <div className="mt-1 text-xs text-muted">{s}</div>
    </div>
  );
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-radius border border-border bg-surface p-4">
      <h2 className="mb-3 text-sm font-medium tracking-wide text-muted uppercase">{title}</h2>
      {children}
    </section>
  );
}

function InternResults({ stats }: { stats: LiveStats | null }) {
  const gate = (stats?.coord as { gate?: { allow?: boolean; reasons?: string[] } } | undefined)?.gate;
  const sets = stats?.sets;
  const rows = [...(sets?.rows ?? [])].filter((row) => !row.axisKey || enabledAxes(stats).includes(row.axisKey.split(":")[0]))
    .sort((a, b) => (b.last15Ratio || 0) - (a.last15Ratio || 0)).slice(0, 8);
  return (
    <Card title="Intern coordination · positive-PF Sets">
      <p className="mb-3 text-sm text-muted">
        gate {gate?.allow ? "open" : "paused"} · valid {sets?.validatedCount ?? 0}/{sets?.setCount ?? 0} · active {sets?.activeCount ?? 0}/{sets?.setCount ?? 0} · hist {sets?.histFills ?? 0} · min PF {sets?.minPf ?? 1.1}
      </p>
      {gate?.reasons?.length ? (
        <p className="mb-3 font-mono text-xs text-warn">{gate.reasons.join(" · ")}</p>
      ) : null}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="font-mono text-[11px] text-muted">
            <tr>
              <th className="pb-2 font-medium">Set</th>
              <th className="pb-2 font-medium">On</th>
              <th className="pb-2 text-right font-medium">PF15</th>
              <th className="pb-2 text-right font-medium">R25</th>
              <th className="pb-2 text-right font-medium">n</th>
              <th className="pb-2 text-right font-medium">Max DDt</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.id}-${r.kind || r.pack || ""}-${i}`} className="border-t border-border font-mono text-xs">
                <td className="py-1.5">{r.id}</td>
                <td className={`py-1.5 ${activeClass(Boolean(r.active))}`}>{r.active ? "on" : "off"}</td>
                <td className={`py-1.5 text-right ${pfClass(r.last15Ratio, r.n)}`}>{r.last15Ratio.toFixed(2)}</td>
                <td className={`py-1.5 text-right ${pnlClass(r.last25AvgR)}`}>{r.last25AvgR.toFixed(2)}</td>
                <td className="py-1.5 text-right">
                  {r.n}
                </td>
                <td className="py-1.5 text-right">{formatDuration(r.maxDdS * 1000)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function SetResults({ stats }: { stats: LiveStats | null }) {
  return (
    <Card title="Sets · PF / DDT">
      <SetGroups sets={stats?.sets} axesEnabled={enabledAxes(stats).length > 0}>{(rows, context) => (
        <SetRows key={JSON.stringify(context.selection)} rows={rows} />
      )}</SetGroups>
    </Card>
  );
}

function SetRows({ rows }: { rows: SetOverviewRow[] }) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(rows.length / 25));
  const current = Math.min(page, pages - 1);
  return <div className="min-w-0 space-y-3">
      <div className="max-w-full overflow-x-auto" tabIndex={0} role="region" aria-label="Set measurements">
        <table className="w-full min-w-[720px] table-fixed text-left text-sm">
          <thead className="font-mono text-[11px] text-muted">
            <tr>
              <th className="w-56 pb-2 font-medium">Set</th>
              <th className="pb-2 font-medium">On</th>
              <th className="pb-2 text-right font-medium">n</th>
              <th className="pb-2 text-right font-medium" title="Cost PF · configured evaluation window">PF</th>
              <th className="pb-2 text-right font-medium" title="Average R · last 25 results">R25</th>
              <th className="pb-2 text-right font-medium">WR</th>
              <th className="pb-2 text-right font-medium">E</th>
              <th className="pb-2 text-right font-medium">Hold</th>
              <th className="pb-2 text-right font-medium">Max DDt</th>
              <th className="pb-2 text-right font-medium">Avg DDt</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={10} className="py-8 text-center text-muted">
                  1m historic replay fills each Set independently
                </td>
              </tr>
            ) : (
              rows.slice(current * 25, (current + 1) * 25).map((r, i) => (
                <tr key={setRowKey(r, i)} className="border-t border-border font-mono text-xs">
                  <td className="py-1.5 pr-3"><SetIdentity row={r} /></td>
                  <td className={`py-1.5 ${activeClass(Boolean(r.active))}`}>{r.active ? "on" : "off"}</td>
                  <td className="py-1.5 text-right">
                    {r.n}
                  </td>
                  <td className="py-1.5 text-right">{r.n ? setMetric(r.last15Ratio) : "—"}</td>
                  <td className={`py-1.5 text-right ${pnlClass(r.last25AvgR)}`}>{setMetric(r.last25AvgR)}</td>
                  <td className="py-1.5 text-right">{r.wr == null ? "—" : `${setMetric(r.wr, 0)}%`}</td>
                  <td className={`py-1.5 text-right ${pnlClass(r.expectancy)}`}>{setMetric(r.expectancy, 4)}</td>
                  <td className="py-1.5 text-right">{r.avgHoldS == null ? "—" : formatDuration(r.avgHoldS * 1000)}</td>
                  <td className="py-1.5 text-right">{r.maxDdS == null ? "—" : formatDuration(r.maxDdS * 1000)}</td>
                  <td className="py-1.5 text-right">{r.avgDdS == null ? "—" : formatDuration(r.avgDdS * 1000)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2 font-mono text-xs">
        <span className="mr-auto text-muted">{rows.length ? current * 25 + 1 : 0}–{Math.min((current + 1) * 25, rows.length)} / {rows.length}</span>
        <button type="button" disabled={!current} onClick={() => setPage(current - 1)} className="min-h-11 rounded-lg border border-border px-3 disabled:opacity-40">Previous</button>
        <span role="status">{current + 1}/{pages}</span>
        <button type="button" disabled={current + 1 >= pages} onClick={() => setPage(current + 1)} className="min-h-11 rounded-lg border border-border px-3 disabled:opacity-40">Next</button>
      </div>
    </div>;
}

function ExitResults({ stats }: { stats: LiveStats | null }) {
  const lanes = stats?.exits?.lanes ?? [];
  return (
    <Card title="Exit lanes · SL takes profit">
      <p className="mb-3 text-sm text-muted">
        Independent of TP · last pick {stats?.exits?.lastPick ?? "—"} · opt SL {Number(stats?.exits?.optSlPct ?? 0).toFixed(2)}%
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="font-mono text-[11px] text-muted">
            <tr>
              <th className="pb-2 font-medium">Lane</th>
              <th className="pb-2 font-medium">On</th>
              <th className="pb-2 text-right font-medium">n</th>
              <th className="pb-2 text-right font-medium">Last 15 PF</th>
              <th className="pb-2 text-right font-medium">Last 25 R</th>
              <th className="pb-2 text-right font-medium">Max DDt</th>
            </tr>
          </thead>
          <tbody>
            {lanes.length === 0 ? (
              <tr>
                <td colSpan={6} className="py-6 text-center text-muted">
                  Closes tag lock / peak / rev / time / hard
                </td>
              </tr>
            ) : (
              lanes.map((r) => (
                <tr key={r.key} className="border-t border-border font-mono text-xs">
                  <td className={`py-1.5 ${r.selected ? "text-primary" : ""}`}>{r.key}</td>
                  <td className={`py-1.5 ${activeClass(Boolean(r.active))}`}>{r.active ? "on" : "off"}</td>
                  <td className="py-1.5 text-right">{r.n}</td>
                  <td className="py-1.5 text-right">{r.last15Ratio.toFixed(2)}</td>
                  <td className={`py-1.5 text-right ${pnlClass(r.last25AvgR)}`}>{r.last25AvgR.toFixed(2)}</td>
                  <td className="py-1.5 text-right">{formatDuration(r.maxDdS * 1000)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function BlockResults({ stats }: { stats: LiveStats | null }) {
  const blk = stats?.block;
  const lanes = blk?.lanes ?? [];
  return (
    <Card title="Block strategy · CTS counts · PF gate">
      <p className="mb-3 text-sm text-muted">
        stack {blk?.maxStack ?? "—"} · vol {blk?.volumeRatio ?? "—"} · pfRatio {blk?.profitFactorRatio ?? "—"} · minPF {blk?.defaultMinPF ?? "—"} · live {blk?.activeLive ? "on" : "off"}
        {blk?.overall !== false ? " · overall Real" : ""}
      </p>
      {lanes.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted">Block lanes appear when a parent is open</p>
      ) : (
        <div className="space-y-4">
          {lanes.map((lane, index) => (
            <div key={`${lane.symbol}-${lane.side}-${index}`} className="rounded-lg border border-border p-3">
              <div className="mb-2 flex flex-wrap justify-between gap-2 text-sm">
                <span className="font-medium">
                  {lane.symbol.replace("-USDT", "")} {lane.side}
                </span>
                <span className="font-mono text-xs text-muted">
                  base {lane.baseQty} · add {lane.confirmedAdd} · agg {lane.aggregate}
                  {lane.realN != null ? ` · Real ${Number(lane.realPf ?? 0).toFixed(2)}/${lane.realN}` : ""}
                </span>
              </div>
              <table className="w-full text-left text-xs">
                <thead className="font-mono text-muted">
                  <tr>
                    <th className="pb-1 font-medium">#</th>
                    <th className="pb-1 text-right font-medium">inc</th>
                    <th className="pb-1 text-right font-medium">min PF</th>
                    <th className="pb-1 text-right font-medium">obs PF</th>
                    <th className="pb-1 font-medium">pass</th>
                    <th className="pb-1 font-medium">paused</th>
                    <th className="pb-1 font-medium">sat</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {lane.counts.map((c) => (
                    <tr key={c.n} className="border-t border-border">
                      <td className="py-1">{c.n}</td>
                      <td className="py-1 text-right">{c.inc}</td>
                      <td className="py-1 text-right">{c.minPF}</td>
                      <td className="py-1 text-right">{c.obsPF}</td>
                      <td className={c.pass ? "py-1 text-primary" : "py-1 text-muted"}>{c.pass ? "yes" : "no"}</td>
                      <td className={c.paused ? "py-1 text-warn" : "py-1"}>{c.paused ? "yes" : "no"}</td>
                      <td className="py-1">{c.satisfied ? "yes" : "no"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function DcaResults({ stats }: { stats: LiveStats | null }) {
  const dca = stats?.dca;
  const lanes = dca?.lanes ?? [];
  return (
    <Card title="DCA · independent CTS steps">
      <p className="mb-3 text-sm text-muted">
        {dca?.enabled ? "on" : "off"} · {dca?.active ? "active" : dca?.deactReason || "idle"} · steps {dca?.maxSteps ?? "—"} · PF15 {Number(dca?.last15Ratio ?? 1).toFixed(2)} · last25 R {Number(dca?.last25AvgR ?? 0).toFixed(2)} · dist {(dca?.distancesPct ?? []).join("/")}
      </p>
      {lanes.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted">Lanes attach when a parent is open</p>
      ) : (
        <div className="space-y-3">
          {lanes.map((lane, index) => (
            <div key={`${lane.symbol}-${lane.side}-${index}`} className="rounded-lg border border-border p-3 font-mono text-xs">
              <div className="mb-2 flex justify-between gap-2">
                <span>
                  {lane.symbol.replace("-USDT", "")} {lane.side}
                </span>
                <span className="text-muted">
                  parent {lane.parentQty} · avg {lane.avgEntry} · filled {lane.filledN}
                </span>
              </div>
              <div className="flex flex-wrap gap-2">
                {lane.steps.map((s) => (
                  <span key={s.n} className={s.filled ? "text-primary" : "text-muted"}>
                    #{s.n} {s.distancePct}% ×{s.mult}
                    {s.filled ? " filled" : ""}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
