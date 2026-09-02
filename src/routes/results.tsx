import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import { DeskShell } from "@/components/desk-shell";
import { useConnection } from "@/components/connection-provider";
import { fetchLiveStats, pickView, type LiveStats } from "@/lib/live-stats";
import { derive } from "@/lib/derive-stats";
import { buildOverview, formatDuration } from "@/lib/analytics";
import { StatsOverview } from "@/components/stats-overview";
import { CoveragePanel } from "@/components/coverage-overview";
import { IndicationKindsPanel, StrategyStatsPanel } from "@/components/kind-strategy-stats";
import { EquityArea, SymbolBars, TradeBars } from "@/components/visual-stats";

export const Route = createFileRoute("/results")({ component: ResultsPage });

function ResultsPage() {
  const { conn } = useConnection();
  const [raw, setRaw] = useState<LiveStats | null>(null);
  useEffect(() => {
    let alive = true;
    setRaw(null);
    let timer: ReturnType<typeof setTimeout> | null = null;
    const pull = async () => {
      const s = await fetchLiveStats(conn);
      if (!alive) return;
      setRaw(s);
      const hidden = typeof document !== "undefined" && document.hidden;
      timer = setTimeout(pull, hidden ? 8000 : 4000);
    };
    void pull();
    return () => {
      alive = false;
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
  const closed = stats?.closed ?? [];

  return (
    <DeskShell
      live={Boolean(stats?.running && !stats?.halted && !stats?.paused)}
      mode={stats?.paused ? "PAUSED" : stats?.mode}
      paused={Boolean(stats?.paused || stats?.haltReason === "paused")}
      statsType={stats?.connType}
      statsId={stats?.connection}
    >
      <p className="font-mono text-[11px] tracking-wide text-muted uppercase" data-testid="results-identity">
        {stats?.connType || conn} · {stats?.connection || conn} · {stats?.unit || ""} · {stats?.openCount ?? 0} open · {closed.length} closed
      </p>
      <StatsOverview data={overview} live={stats} />

      <div className="flex flex-wrap gap-2">
        <a
          href={`/results-export.json?conn=${encodeURIComponent(conn)}`}
          download={`pulse-results-${conn}.json`}
          className="inline-flex min-h-11 items-center rounded-lg bg-primary px-4 text-sm font-medium text-bg"
        >
          Download JSON
        </a>
        <a
          href={`/results-export.md?conn=${encodeURIComponent(conn)}`}
          download={`pulse-results-${conn}.md`}
          className="inline-flex min-h-11 items-center rounded-lg border border-border px-4 text-sm"
        >
          Download report
        </a>
      </div>

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Hero k="Trades" v={String(closed.length)} s={`${d.longs}L / ${d.shorts}S`} />
        <Hero k="Gross profit" v={`+${d.gp.toFixed(4)}`} s={`avg win ${d.avgWin.toFixed(4)}`} good />
        <Hero k="Gross loss" v={d.gl ? `-${d.gl.toFixed(4)}` : "0"} s={`avg loss ${d.avgLoss.toFixed(4)}`} bad={d.gl > 0} />
        <Hero
          k="PF after cost"
          v={(stats?.pfCost?.ratio ?? stats?.profitFactor ?? 1).toFixed(2)}
          s={`1.00=neutral · 1.10=+1×cost · min ${stats?.pfCost?.minPf ?? 1.1} · ${stats?.pfCost?.pass ? "pass" : "gate"}`}
        />
      </section>

      <CoveragePanel live={stats} />
      <IndicationKindsPanel stats={stats} />
      <StrategyStatsPanel stats={stats} />
      <InternResults stats={stats} />
      <SetResults stats={stats} />
      <ExitResults stats={stats} />
      <BlockResults stats={stats} />
      <DcaResults stats={stats} />

      <section className="grid gap-3 lg:grid-cols-2">
        <Card title="Equity curve">
          <EquityArea data={d.equityCurve} />
        </Card>
        <Card title="Per-trade PnL">
          <TradeBars data={d.tradeBars} />
        </Card>
      </section>

      <section className="grid gap-3 lg:grid-cols-2">
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
                      <span className={`font-mono tabular-nums ${r.pnl >= 0 ? "text-primary" : "text-danger"}`}>
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
              {closed.length === 0 ? (
                <tr>
                  <td colSpan={7} className="py-8 text-center text-muted">
                    Closed fills stream here
                  </td>
                </tr>
              ) : (
                closed.map((c, i) => (
                  <tr key={`${c.t}-${i}`} className="border-t border-border">
                    <td className="py-2.5 font-mono text-xs text-muted">
                      {new Date(c.t * 1000).toLocaleTimeString()}
                    </td>
                    <td className="py-2.5 font-medium">{c.symbol.replace("-USDT", "")}</td>
                    <td className="py-2.5">
                      <span
                        className={`inline-flex items-center gap-0.5 rounded-full px-2 py-0.5 font-mono text-xs ${
                          c.side === "LONG" ? "bg-primary-dim/40 text-primary" : "bg-danger/15 text-danger"
                        }`}
                      >
                        {c.side === "LONG" ? (
                          <ArrowUpRight className="size-3" />
                        ) : (
                          <ArrowDownRight className="size-3" />
                        )}
                        {c.side}
                      </span>
                    </td>
                    <td className="py-2.5 font-mono text-xs">
                      {c.entry.toPrecision(5)} → {c.exit.toPrecision(5)}
                    </td>
                    <td className={`py-2.5 font-mono tabular-nums ${c.pnl >= 0 ? "text-primary" : "text-danger"}`}>
                      {c.pnl >= 0 ? "+" : ""}
                      {c.pnl.toFixed(4)}
                      <span className="ml-1 text-faint">({(c.pnl_pct * 100).toFixed(3)}%)</span>
                    </td>
                    <td className="py-2.5 font-mono text-muted">{c.hold_s.toFixed(0)}s</td>
                    <td className="py-2.5 text-xs text-muted">{c.reason}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </DeskShell>
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
    <section className="rounded-radius border border-border bg-surface p-4">
      <h2 className="mb-3 text-sm font-medium tracking-wide text-muted uppercase">{title}</h2>
      {children}
    </section>
  );
}

function InternResults({ stats }: { stats: LiveStats | null }) {
  const gate = (stats?.coord as { gate?: { allow?: boolean; reasons?: string[] } } | undefined)?.gate;
  const sets = stats?.sets;
  const rows = [...(sets?.rows ?? [])].sort((a, b) => (b.last15Ratio || 0) - (a.last15Ratio || 0)).slice(0, 8);
  return (
    <Card title="Intern coordination · positive-PF Sets">
      <p className="mb-3 text-sm text-muted">
        gate {gate?.allow ? "open" : "paused"} · intern {sets?.activeCount ?? 0}/{sets?.setCount ?? 0} active · hist {sets?.histFills ?? 0} · min PF {sets?.minPf ?? 1.1}
      </p>
      {gate?.reasons?.length ? (
        <p className="mb-3 font-mono text-xs text-danger">{gate.reasons.join(" · ")}</p>
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
            {rows.map((r) => (
              <tr key={r.id} className="border-t border-border font-mono text-xs">
                <td className="py-1.5">{r.id}</td>
                <td className={r.active ? "py-1.5 text-primary" : "py-1.5 text-danger"}>{r.active ? "on" : "off"}</td>
                <td className={`py-1.5 text-right ${r.last15Ratio >= 1.1 ? "text-primary" : "text-danger"}`}>{r.last15Ratio.toFixed(2)}</td>
                <td className={`py-1.5 text-right ${r.last25AvgR < 0 ? "text-danger" : "text-primary"}`}>{r.last25AvgR.toFixed(2)}</td>
                <td className="py-1.5 text-right">
                  {r.n}
                  {r.liveN ? `+${r.liveN}` : ""}
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
  const rows = stats?.sets?.rows ?? [];
  return (
    <Card title="Independent Sets · last 15 PF · max DD time · last 25 R">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="font-mono text-[11px] text-muted">
            <tr>
              <th className="pb-2 font-medium">Set</th>
              <th className="pb-2 font-medium">On</th>
              <th className="pb-2 text-right font-medium">n</th>
              <th className="pb-2 text-right font-medium">Last 15 PF</th>
              <th className="pb-2 text-right font-medium">Last 25 R</th>
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
              rows.map((r) => (
                <tr key={r.id} className="border-t border-border font-mono text-xs">
                  <td className="py-1.5">
                    {r.pack} sl{r.slRatio.toFixed(1)} st{r.step ?? "—"} {r.trailKey}
                  </td>
                  <td className={r.active ? "py-1.5 text-primary" : "py-1.5 text-danger"}>{r.active ? "on" : "off"}</td>
                  <td className="py-1.5 text-right">
                    {r.n}
                    {r.liveN ? `+${r.liveN}` : ""}
                  </td>
                  <td className="py-1.5 text-right">{r.last15Ratio.toFixed(2)}</td>
                  <td className={`py-1.5 text-right ${r.last25AvgR < 0 ? "text-danger" : "text-primary"}`}>{r.last25AvgR.toFixed(2)}</td>
                  <td className="py-1.5 text-right">{Number(r.wr ?? 0).toFixed(0)}%</td>
                  <td className={`py-1.5 text-right ${(r.expectancy ?? 0) < 0 ? "text-danger" : "text-primary"}`}>{Number(r.expectancy ?? 0).toFixed(4)}</td>
                  <td className="py-1.5 text-right">{formatDuration(Number(r.avgHoldS ?? 0) * 1000)}</td>
                  <td className="py-1.5 text-right">{formatDuration(r.maxDdS * 1000)}</td>
                  <td className="py-1.5 text-right">{formatDuration(r.avgDdS * 1000)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
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
                  <td className={r.active ? "py-1.5 text-primary" : "py-1.5 text-danger"}>{r.active ? "on" : "off"}</td>
                  <td className="py-1.5 text-right">{r.n}</td>
                  <td className="py-1.5 text-right">{r.last15Ratio.toFixed(2)}</td>
                  <td className={`py-1.5 text-right ${r.last25AvgR < 0 ? "text-danger" : "text-primary"}`}>{r.last25AvgR.toFixed(2)}</td>
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
      </p>
      {lanes.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted">Block lanes appear when a parent is open</p>
      ) : (
        <div className="space-y-4">
          {lanes.map((lane) => (
            <div key={`${lane.symbol}-${lane.side}`} className="rounded-lg border border-border p-3">
              <div className="mb-2 flex flex-wrap justify-between gap-2 text-sm">
                <span className="font-medium">
                  {lane.symbol.replace("-USDT", "")} {lane.side}
                </span>
                <span className="font-mono text-xs text-muted">
                  base {lane.baseQty} · add {lane.confirmedAdd} · agg {lane.aggregate}
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
                      <td className={c.pass ? "py-1 text-primary" : "py-1 text-danger"}>{c.pass ? "yes" : "no"}</td>
                      <td className={c.paused ? "py-1 text-danger" : "py-1"}>{c.paused ? "yes" : "no"}</td>
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
          {lanes.map((lane) => (
            <div key={`${lane.symbol}-${lane.side}`} className="rounded-lg border border-border p-3 font-mono text-xs">
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
