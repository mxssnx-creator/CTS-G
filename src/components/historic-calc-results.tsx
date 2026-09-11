import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { formatDuration } from "@/lib/analytics";
import type { HistCalcJob } from "@/lib/hist-calc";

type ChartFrameProps = {
  children: React.ReactElement;
  height?: number;
};

function ChartFrame({ children, height = 240 }: ChartFrameProps) {
  const [ready, setReady] = useState(false);

  useEffect(() => setReady(true), []);

  if (!ready) return <div className="w-full rounded-lg bg-bg2" style={{ height }} />;

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        {children}
      </ResponsiveContainer>
    </div>
  );
}

function numberValue(value: unknown, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function percent(completed: unknown, requested: unknown) {
  const total = numberValue(requested);
  if (total <= 0) return 0;
  return Math.max(0, Math.min(100, (numberValue(completed) / total) * 100));
}

function shortName(value: string) {
  return value.replace(/-USDT$/i, "").replace(/^indications:/, "ind:").slice(0, 18);
}

function ResultStat({ label, value, tone = "text-fg" }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-border bg-bg2 px-3 py-3">
      <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted">{label}</p>
      <p className={`mt-1 font-mono text-lg tabular-nums ${tone}`}>{value}</p>
    </div>
  );
}

function ChartCard({ title, hint, children }: { title: string; hint: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-bg2 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">{title}</h3>
        <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted">{hint}</p>
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function CoverageMeter({ label, completed, requested }: { label: string; completed: unknown; requested: unknown }) {
  const pct = percent(completed, requested);
  return (
    <div>
      <div className="flex items-center justify-between gap-3 font-mono text-xs">
        <span className="text-muted">{label}</span>
        <span className={pct >= 99.9 ? "text-primary" : "text-warn"}>{pct.toFixed(0)}%</span>
      </div>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-border">
        <div className="h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
      </div>
      <p className="mt-1 font-mono text-[10px] text-muted">
        {numberValue(completed).toLocaleString()} / {numberValue(requested).toLocaleString()}
      </p>
    </div>
  );
}

export function HistoricCalcResults({ job }: { job: HistCalcJob }) {
  const strategyData = useMemo(
    () =>
      Object.entries(job.byStrategy || {})
        .map(([strategy, value]) => ({
          strategy: shortName(strategy),
          pf: numberValue(value.pf),
          n: numberValue(value.n),
          validated: Boolean(value.validated),
        }))
        .sort((a, b) => b.pf - a.pf)
        .slice(0, 10),
    [job.byStrategy],
  );

  const symbolData = useMemo(
    () =>
      (job.bySymbol || [])
        .map((value) => ({
          symbol: shortName(value.symbol),
          pf: numberValue(value.pf),
          n: numberValue(value.n),
          validated: Boolean(value.validated),
        }))
        .sort((a, b) => b.pf - a.pf)
        .slice(0, 10),
    [job.bySymbol],
  );

  const riskData = useMemo(
    () =>
      (job.rows || [])
        .filter((row) => numberValue(row.n) > 0)
        .slice(0, 40)
        .map((row) => ({
          id: row.id,
          label: shortName(row.id),
          pf: numberValue(row.last15Ratio),
          ddHours: numberValue(row.maxDdS) / 3600,
          n: numberValue(row.n),
          validated: Boolean(row.validated),
        })),
    [job.rows],
  );

  const coverage = job.coverage || {};
  const state = job.phase === "ready" || job.ready ? "Published" : job.phase === "stopped" ? "Stopped" : job.phase;
  const stateTone = job.error ? "text-danger" : job.phase === "ready" || job.ready ? "text-primary" : "text-warn";
  const winner = job.winner;

  return (
    <div className="space-y-3" data-testid="calc-result-snapshot">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <ResultStat label="Snapshot" value={state} tone={stateTone} />
        <ResultStat label="Validated" value={`${job.validatedCount ?? 0}/${job.rowCount ?? 0}`} />
        <ResultStat label="Evaluation window" value={`${job.hours ?? 0}h · ${job.evaluationBars ?? job.lookback ?? 0} bars`} />
        <ResultStat label="Data source" value={String(job.source || "pending")} />
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <ChartCard title="Strategy quality" hint="PF after cost">
          {strategyData.length ? (
            <ChartFrame height={Math.max(220, strategyData.length * 28)}>
              <BarChart data={strategyData} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 4 }}>
                <CartesianGrid stroke="var(--color-border)" horizontal={false} />
                <XAxis type="number" domain={[0, "auto"]} tick={{ fill: "var(--color-muted)", fontSize: 10 }} />
                <YAxis type="category" dataKey="strategy" width={86} tick={{ fill: "var(--color-fg)", fontSize: 10 }} />
                <Tooltip
                  cursor={{ fill: "var(--color-surface2)" }}
                  contentStyle={{ backgroundColor: "var(--color-surface)", border: "1px solid var(--color-border)", borderRadius: "0.5rem" }}
                />
                <ReferenceLine x={1} stroke="var(--color-warn)" strokeDasharray="4 4" />
                <Bar dataKey="pf" name="PF" radius={[0, 4, 4, 0]}>
                  {strategyData.map((row) => (
                    <Cell key={row.strategy} fill={row.validated ? "var(--color-primary)" : "var(--color-muted)"} />
                  ))}
                </Bar>
              </BarChart>
            </ChartFrame>
          ) : (
            <div className="flex h-52 items-center justify-center text-sm text-muted">Strategy detail appears after the first published run.</div>
          )}
        </ChartCard>

        <ChartCard title="Risk map" hint="PF vs max DD time">
          {riskData.length ? (
            <ChartFrame height={260}>
              <ScatterChart margin={{ top: 10, right: 16, left: 0, bottom: 8 }}>
                <CartesianGrid stroke="var(--color-border)" />
                <XAxis type="number" dataKey="ddHours" name="max DD" unit="h" tick={{ fill: "var(--color-muted)", fontSize: 10 }} />
                <YAxis type="number" dataKey="pf" name="PF" tick={{ fill: "var(--color-muted)", fontSize: 10 }} />
                <ZAxis type="number" dataKey="n" range={[48, 240]} name="samples" />
                <Tooltip
                  cursor={{ strokeDasharray: "3 3" }}
                  contentStyle={{ backgroundColor: "var(--color-surface)", border: "1px solid var(--color-border)", borderRadius: "0.5rem" }}
                />
                <ReferenceLine y={1} stroke="var(--color-warn)" strokeDasharray="4 4" />
                <Scatter name="Configurations" data={riskData} fill="var(--color-primary)">
                  {riskData.map((row) => (
                    <Cell key={row.id} fill={row.validated ? "var(--color-primary)" : "var(--color-faint)"} />
                  ))}
                </Scatter>
              </ScatterChart>
            </ChartFrame>
          ) : (
            <div className="flex h-52 items-center justify-center text-sm text-muted">Risk points appear after configuration rows are scored.</div>
          )}
        </ChartCard>

        <ChartCard title="Symbol coverage" hint="PF by symbol">
          {symbolData.length ? (
            <ChartFrame height={Math.max(220, symbolData.length * 24)}>
              <BarChart data={symbolData} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 4 }}>
                <CartesianGrid stroke="var(--color-border)" horizontal={false} />
                <XAxis type="number" domain={[0, "auto"]} tick={{ fill: "var(--color-muted)", fontSize: 10 }} />
                <YAxis type="category" dataKey="symbol" width={62} tick={{ fill: "var(--color-fg)", fontSize: 10 }} />
                <Tooltip
                  cursor={{ fill: "var(--color-surface2)" }}
                  contentStyle={{ backgroundColor: "var(--color-surface)", border: "1px solid var(--color-border)", borderRadius: "0.5rem" }}
                />
                <ReferenceLine x={1} stroke="var(--color-warn)" strokeDasharray="4 4" />
                <Bar dataKey="pf" name="PF" radius={[0, 4, 4, 0]}>
                  {symbolData.map((row) => (
                    <Cell key={row.symbol} fill={row.validated ? "var(--color-primary)" : "var(--color-muted)"} />
                  ))}
                </Bar>
              </BarChart>
            </ChartFrame>
          ) : (
            <div className="flex h-52 items-center justify-center text-sm text-muted">Symbol coverage appears after bars are loaded.</div>
          )}
        </ChartCard>

        <ChartCard title="Run health" hint="coverage and winner">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-4">
              <CoverageMeter label="Symbols" completed={coverage.symbols?.completed} requested={coverage.symbols?.requested} />
              <CoverageMeter label="Bars" completed={coverage.bars?.completed} requested={coverage.bars?.requested} />
              <CoverageMeter label="Set evaluations" completed={coverage.evaluations?.completed} requested={coverage.evaluations?.requested} />
              <CoverageMeter label="Worker tasks" completed={coverage.tasks?.completed} requested={coverage.tasks?.requested} />
            </div>
            <div className="flex flex-col gap-2 rounded-lg border border-border bg-surface px-3 py-3 text-sm">
              <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted">Selected winner</p>
              {winner ? (
                <>
                  <p className="break-all font-mono text-primary">{winner.id}</p>
                  <div className="grid grid-cols-2 gap-2 font-mono text-xs">
                    <span className="text-muted">PF</span><span className="text-right">{numberValue(winner.last15Ratio).toFixed(2)}</span>
                    <span className="text-muted">Samples</span><span className="text-right">{winner.n}</span>
                    <span className="text-muted">Max DD time</span><span className="text-right">{formatDuration(numberValue(winner.maxDdS) * 1000)}</span>
                    <span className="text-muted">Net avg</span><span className="text-right">{(numberValue(winner.netAvg) * 100).toFixed(3)}%</span>
                  </div>
                </>
              ) : (
                <p className="text-muted">No validated winner has been published yet.</p>
              )}
            </div>
          </div>
        </ChartCard>
      </div>
    </div>
  );
}
