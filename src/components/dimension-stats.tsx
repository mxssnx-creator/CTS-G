import { useMemo, useState } from "react";
import { CartesianGrid, ReferenceLine, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from "recharts";
import type { LiveStats } from "@/lib/live-stats";
import { formatDuration } from "@/lib/analytics";
import { enabledAxes, INDICATION_GROUPS, setLabel, setMetric, STRATEGY_GROUPS, type SetOverviewRow } from "@/lib/set-overview";
import { detailsCsv, dimensionMatrix, metricPoints, sortSetDetails, STRATEGY_COLORS, type DetailSort, type MetricPoint } from "@/lib/dimension-stats";
import { SetGroups, type SetGroupContext } from "./set-groups";
import { ClientChart } from "./visual-stats";

const label = (value: string) => value === "dca" ? "DCA" : value[0]?.toUpperCase() + value.slice(1);
const duration = (seconds: number | null | undefined) => seconds == null || !Number.isFinite(seconds) ? "—" : formatDuration(seconds * 1000);

export default function DimensionStats({ stats, focus }: { stats: LiveStats | null; focus: "indications" | "strategies" }) {
  return (
    <section className="min-w-0 rounded-radius border border-border bg-surface p-4" data-testid="dimension-stats">
      <h2 className="text-sm font-medium tracking-wide text-fg uppercase">{label(focus)} · multidimensional analysis</h2>
      <p className="mt-1 mb-4 text-sm text-muted">Compare indication × strategy × TP range. System calculations and confirmed exchange results have separate measurements.</p>
      <SetGroups sets={stats?.sets} axesEnabled={enabledAxes(stats).length > 0} showEmptyPanel>
        {(rows, context) => <Analysis key={JSON.stringify(context.selection)} rows={rows} context={context} />}
      </SetGroups>
    </section>
  );
}

function Analysis({ rows, context }: { rows: SetOverviewRow[]; context: SetGroupContext }) {
  const matrix = useMemo(() => dimensionMatrix(context.groups), [context.groups]);
  const chart = useMemo(() => metricPoints(rows), [rows]);
  const indications = INDICATION_GROUPS.filter((kind) => matrix.some((cell) => cell.indication === kind));
  const strategies = STRATEGY_GROUPS.filter((kind) => matrix.some((cell) => cell.strategy === kind));
  const max = Math.max(1, ...matrix.map((cell) => cell.count));
  const total = matrix.reduce((sum, cell) => sum + cell.count, 0);
  const series = useMemo(() => STRATEGY_GROUPS.map((strategy) => ({ strategy, points: chart.points.filter((row) => row.strategyType === strategy) })).filter((group) => group.points.length), [chart.points]);
  return (
    <div className="min-w-0 space-y-4">
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        <div className="min-w-0 rounded-lg border border-border bg-bg2 p-3" data-testid="dimension-matrix">
          <h3 className="text-sm font-medium">Indication × strategy</h3>
          <p className="mt-1 mb-3 text-xs text-muted">{total.toLocaleString("en-US")} {context.version ? "configurations · complete catalog counts" : "available preview rows · legacy snapshot"}. Select a cell to inspect its sets.</p>
          {matrix.length ? <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <caption className="sr-only">Configuration counts for each indication and strategy in the selected source and TP range</caption>
              <thead><tr><th scope="col" className="p-1 font-normal text-muted">Indication</th>{strategies.map((strategy) => <th key={strategy} scope="col" className="p-1 text-center font-medium" style={{ color: STRATEGY_COLORS[strategy] }}>{label(strategy)}</th>)}</tr></thead>
              <tbody>{indications.map((kind) => <tr key={kind}>
                <th scope="row" className="pr-2 font-normal text-muted">{label(kind)}</th>
                {strategies.map((strategy) => {
                  const count = matrix.find((cell) => cell.indication === kind && cell.strategy === strategy)?.count ?? 0;
                  return <td key={strategy} className="p-0.5"><button type="button" disabled={!count}
                    aria-label={`${label(kind)}, ${label(strategy)}: ${count} configurations`}
                    onClick={() => context.select({ ...context.selection, indication: kind, strategy })}
                    className="min-h-11 w-full min-w-14 rounded-md border border-border px-2 font-mono tabular-nums focus-visible:outline-2 focus-visible:outline-primary disabled:text-faint"
                    style={{ backgroundColor: count ? `rgba(61, 207, 142, ${0.08 + 0.32 * Math.sqrt(count / max)})` : undefined }}>
                    {count ? count.toLocaleString("en-US") : "—"}
                  </button></td>;
                })}
              </tr>)}</tbody>
            </table>
          </div> : <Empty text="No configurations in this selection." />}
          <p className="mt-3 text-xs text-muted">Brighter cells contain more configurations. Counts describe calculation or execution lanes; they are not a trade total.</p>
        </div>

        <div className="min-w-0 rounded-lg border border-border bg-bg2 p-3" data-testid="dimension-scatter">
          <h3 className="text-sm font-medium">Cost PF × drawdown time × sample size</h3>
          <p className="mt-1 text-xs text-muted">Each point is one independent set. Colour = strategy · area = retained samples.</p>
          {chart.points.length ? <ClientChart height={300}>
            <ScatterChart margin={{ top: 18, right: 12, left: 0, bottom: 20 }}>
              <CartesianGrid stroke="#1d3a32" />
              <XAxis type="number" dataKey="pf" name="Cost PF" domain={["auto", "auto"]} tick={{ fill: "#7f9d90", fontSize: 11 }} tickFormatter={(v) => setMetric(Number(v))} label={{ value: "Cost PF · independent window", position: "bottom", fill: "#7f9d90", fontSize: 11 }} />
              <YAxis type="number" dataKey="ddMinutes" name="Max DDT" unit="m" width={54} tick={{ fill: "#7f9d90", fontSize: 11 }} tickFormatter={(v) => Number(v).toLocaleString("en-US", { maximumFractionDigits: 1 })} />
              <ZAxis type="number" dataKey="samples" range={[25, 220]} name="Retained samples" />
              <ReferenceLine x={1} stroke="#7f9d90" strokeDasharray="3 3" />
              <Tooltip content={<PointTip />} />
              {series.map(({ strategy, points }) => <Scatter key={strategy} name={label(strategy)} data={points} fill={STRATEGY_COLORS[strategy]} fillOpacity={0.7} isAnimationActive={false} />)}
            </ScatterChart>
          </ClientChart> : <Empty text="Points appear when a set has both PF and DDT measurements. Missing values are not plotted as zero." />}
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-2 text-xs" aria-label="Strategy colour legend">{series.map(({ strategy }) => <span key={strategy} className="flex items-center gap-1.5"><span className="inline-block size-2 rounded-full" style={{ backgroundColor: STRATEGY_COLORS[strategy] }} />{label(strategy)}</span>)}</div>
          <p className="mt-3 text-xs text-muted">{chart.points.length} of {chart.available} measured preview sets plotted · up to 350 points, balanced across connections, indications and strategies. Dotted line: cost-neutral PF 1.00.</p>
        </div>
      </div>
      <p className="text-xs text-muted" data-testid="dimension-sample-note">PF, DDT and the details below use the bounded per-group preview, not the entire catalog. No PF or DDT averages are calculated across sets or connections. Calculation qualification does not enable execution.</p>
      <Details rows={rows} />
    </div>
  );
}

function PointTip({ active, payload }: { active?: boolean; payload?: Array<{ payload?: MetricPoint }> }) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  return <div className="max-w-72 rounded-lg border border-border bg-surface p-3 text-xs shadow-lg">
    <p className="mb-2 break-words text-fg">{setLabel(row)}</p>
    <dl className="grid grid-cols-2 gap-x-3 gap-y-1 font-mono">
      <dt>Cost PF</dt><dd className="text-right">{setMetric(row.pf)}</dd>
      <dt>Max DDT</dt><dd className="text-right">{duration(row.maxDdS)}</dd>
      <dt>Average DDT</dt><dd className="text-right">{duration(row.avgDdS)}</dd>
      <dt>Samples</dt><dd className="text-right">{row.n}</dd>
      <dt>Win rate</dt><dd className="text-right">{setMetric(row.wr, 1)}%</dd>
      <dt>Net expectancy</dt><dd className="text-right">{row.expectancy == null ? "—" : `${setMetric(row.expectancy * 100, 3)}%`}</dd>
    </dl>
  </div>;
}

function Details({ rows }: { rows: SetOverviewRow[] }) {
  const [sort, setSort] = useState<DetailSort>("identity");
  const [page, setPage] = useState(0);
  const ordered = useMemo(() => sortSetDetails(rows, sort), [rows, sort]);
  const pages = Math.max(1, Math.ceil(ordered.length / 25));
  const current = Math.min(page, pages - 1);
  const exportCsv = () => {
    const url = URL.createObjectURL(new Blob([detailsCsv(ordered)], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `set-metrics-${rows[0]?.scope ?? "empty"}.csv`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <div className="min-w-0" data-testid="dimension-details">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      <h3 className="text-sm font-medium">Detailed set measurements <span className="font-mono text-xs text-muted">· {rows.length} preview rows</span></h3>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <label className="flex items-center gap-2">Sort by<select aria-label="Sort detailed measurements" value={sort} onChange={(e) => { setSort(e.target.value as DetailSort); setPage(0); }} className="min-h-11 rounded-lg border border-border bg-bg2 px-2">
          <option value="identity">Set identity</option><option value="pf">Cost PF · high first</option><option value="ddt">Max DDT · high first</option><option value="samples">Samples · high first</option>
        </select></label>
        <button type="button" disabled={!rows.length} onClick={exportCsv} className="min-h-11 rounded-lg border border-border px-3 disabled:opacity-40">Export selected CSV</button>
      </div>
    </div>
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full min-w-[1000px] text-left text-xs">
        <caption className="sr-only">Independent per-set measurements for the selected source, indication, range and strategy</caption>
        <thead className="bg-bg2 text-muted"><tr>{["Set / configuration", "Samples", "Cost PF", "Last 25 R", "Max DDT", "Avg DDT", "Win rate", "Net E %", "Avg hold", "Calculation"].map((title) => <th key={title} scope="col" className="p-2 font-normal">{title}</th>)}</tr></thead>
        <tbody>{ordered.slice(current * 25, (current + 1) * 25).map((row) => <tr key={row.id} className="border-t border-border font-mono tabular-nums">
          <td className="max-w-80 p-2"><details><summary className="cursor-pointer break-words text-fg">{setLabel(row)}</summary><p className="mt-2 break-all text-[10px] text-muted">{row.setId || row.id}</p><p className="mt-1 text-muted">{row.scope} · {row.connection || "selected connection"} · {row.side || "combined directions"}</p></details></td>
          <td className="p-2">{row.n}</td><td className="p-2">{row.n ? setMetric(row.last15Ratio) : "—"}</td><td className="p-2">{row.n ? setMetric(row.last25AvgR) : "—"}</td>
          <td className="p-2">{row.n ? duration(row.maxDdS) : "—"}</td><td className="p-2">{row.n ? duration(row.avgDdS) : "—"}</td><td className="p-2">{row.n && row.wr != null ? `${setMetric(row.wr, 1)}%` : "—"}</td>
          <td className="p-2">{row.n && row.expectancy != null ? setMetric(row.expectancy * 100, 3) : "—"}</td><td className="p-2">{row.n ? duration(row.avgHoldS) : "—"}</td>
          <td className={`max-w-48 p-2 ${row.active ? "text-primary" : "text-muted"}`}>{row.active ? "Qualified" : row.deactReason || "Waiting"}</td>
        </tr>)}</tbody>
      </table>
      {!rows.length ? <p className="p-6 text-center text-sm text-muted">No measurements in this selection yet.</p> : null}
    </div>
    <div className="mt-3 flex items-center justify-end gap-3 text-xs">
      <button type="button" disabled={!current} onClick={() => setPage(current - 1)} className="min-h-11 rounded-lg border border-border px-3 disabled:opacity-40">Previous measurements</button>
      <span role="status">{current + 1} / {pages}</span>
      <button type="button" disabled={current + 1 >= pages} onClick={() => setPage(current + 1)} className="min-h-11 rounded-lg border border-border px-3 disabled:opacity-40">Next measurements</button>
    </div>
  </div>;
}

function Empty({ text }: { text: string }) {
  return <p className="flex min-h-44 items-center justify-center px-4 text-center text-sm text-muted">{text}</p>;
}
