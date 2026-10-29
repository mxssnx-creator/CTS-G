import type { ReactNode } from "react";
import type { CostPfMetric } from "@/lib/analytics";
import { formatDuration, type buildOverview } from "@/lib/analytics";
import type { LiveStats } from "@/lib/live-stats";
import { formatPf, stageFigures } from "@/lib/sets-view";

type Overview = ReturnType<typeof buildOverview>;

export function StatsOverview({
  data,
  compact,
  live,
}: {
  data: Overview;
  compact?: boolean;
  live?: LiveStats | null;
}) {
  const dd = data.drawdown3d;
  const all = data.drawdownAll;
  const cost: CostPfMetric = {
    ...data.costPf,
    ...(live?.pfCost
      ? {
          n: live.pfCost.n ?? data.costPf.n,
          count: live.pfCost.count ?? data.costPf.count,
          avgR: live.pfCost.avgR ?? data.costPf.avgR,
          ratio: live.pfCost.ratio ?? data.costPf.ratio,
          pf: live.pfCost.pf ?? data.costPf.pf,
          costPct: live.pfCost.costPct ?? data.costPf.costPct,
          netPct: live.pfCost.netPct ?? data.costPf.netPct,
          netSumPct: live.pfCost.netSumPct ?? data.costPf.netSumPct,
          grossPct: live.pfCost.grossPct ?? data.costPf.grossPct,
          minPf: live.pfCost.minPf ?? data.costPf.minPf,
          pass: live.pfCost.pass ?? data.costPf.pass,
        }
      : {}),
  };
  const heroes = (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Hero
          k={`Last ${cost.n || 15} PF`}
          v={cost.count ? formatPf(cost.pf) : "—"}
          s={`1.00=neutral · 1.10=+1×cost · R ${cost.avgR.toFixed(2)} · net ${cost.netSumPct >= 0 ? "+" : ""}${cost.netSumPct.toFixed(2)}% (${cost.netPct >= 0 ? "+" : ""}${cost.netPct.toFixed(3)}% per position) · n ${cost.count} · ${cost.pass ? "pass" : "block"}`}
          tone={cost.count < 8 ? "ok" : cost.pass ? "good" : "bad"}
        />
        {live?.sets?.pfBase ? (
          <Hero
            k="Effective PF · after Base"
            v={live.sets.pfBase.base.sets ? live.sets.pfBase.base.pf.toFixed(2) : "—"}
            s={`${live.sets.pfBase.base.sets} valid Sets${live.sets.pfBase.validatedPct != null ? ` (${live.sets.pfBase.validatedPct.toFixed(1)}%${live.sets.pfBase.validatedBasePct != null && live.sets.pfBase.main?.on ? `, Base alone ${live.sets.pfBase.validatedBasePct.toFixed(1)}%` : ""})` : ""} · n ${live.sets.pfBase.base.n}${live.sets.pfBase.main?.on ? ` · Main last ${live.sets.pfBase.main.lastN}: PF ${live.sets.pfBase.main.pf.toFixed(2)}` : ""}${live.sets.pfBase.real ? ` · Real Sets PF ${live.sets.pfBase.real.pf.toFixed(2)} (${live.sets.pfBase.real.orders} orders)` : ""} · before Base ${live.sets.pfBase.unfiltered.pf.toFixed(2)} (under 1 is normal)`}
            tone={!live.sets.pfBase.base.sets ? "ok" : live.sets.pfBase.base.pf >= 1.1 ? "good" : live.sets.pfBase.base.pf < 1 ? "bad" : "ok"}
          />
        ) : null}
        {live?.execution?.sets.stages ? (
          <Hero
            k="Stages · Base → Main → Real"
            v={`${live.execution.sets.stages.real + live.execution.sets.stages.realNeg} Sets on Real`}
            s={stageLine(live)}
            tone="ok"
          />
        ) : null}
        {live?.execution ? (
          <Hero
            k="Executed · after all gates"
            v={`${live.execution.open.positions} open`}
            s={`${live.execution.sets.valid}/${live.execution.sets.configured} Sets valid · ${live.execution.open.sets} Sets running${live.execution.open.underInvalidSet ? ` (${live.execution.open.underInvalidSet} under a Set that no longer passes, handled to close)` : ""} · ${live.execution.closed.orders} orders closed (Set ${live.execution.closed.setLane} · Exit ${live.execution.closed.exitLane}) · ${live.execution.closed.setsThatTraded} Sets traded${live.execution.closed.droppedNoRecord ? ` · ${live.execution.closed.droppedNoRecord} dropped without a record` : ""}`}
            tone="ok"
          />
        ) : null}
        <Hero
          k="Drawdown time avg"
          v={formatDuration(dd.averageDurationMs || all.averageDurationMs)}
          s={`${dd.episodes || all.episodes} episodes · 3d`}
          tone={dd.inDrawdown ? "bad" : "ok"}
        />
        <Hero
          k="Current DD time"
          v={formatDuration(dd.currentDurationMs)}
          s={dd.inDrawdown ? `depth ${dd.currentDepth.toFixed(4)}` : "at peak"}
          tone={dd.inDrawdown ? "bad" : "good"}
        />
        <Hero
          k="Max DD episode"
          v={formatDuration(dd.maxDurationMs || all.maxDurationMs)}
          s={`depth ${Math.max(dd.maxDepth, all.maxDepth).toFixed(4)}`}
        />
      </div>
  );
  if (compact) return heroes;
  return (
    <section className="grid gap-3">
      {heroes}

      <StagesCard live={live} />

      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="Profit factor · 1.00=neutral · 1.10=+1×PositionCost">
          <table className="w-full text-sm">
            <thead className="font-mono text-[11px] text-muted">
              <tr>
                <th className="pb-2 text-left font-medium">Window</th>
                <th className="pb-2 text-right font-medium">n</th>
                <th className="pb-2 text-right font-medium">R</th>
                <th className="pb-2 text-right font-medium">PF</th>
                <th className="pb-2 text-right font-medium" title="Mean net result per position (percent, after PositionCost)">Net / pos %</th>
                <th className="pb-2 text-right font-medium" title="The window's net result summed over its positions (percent of one position): the net the PF divides">Net Σ %</th>
              </tr>
            </thead>
            <tbody>
              <PfRow label="Last 4" m={data.last4} />
              <PfRow label="Last 15" m={data.positionWindows["15"]} />
              <PfRow label="Last 25" m={data.positionWindows["25"]} />
              <PfRow label="Last 75" m={data.positionWindows["75"]} />
              <PfRow label="4 hours" m={data.timeWindows["4h"]} />
              <PfRow label="12 hours" m={data.timeWindows["12h"]} />
              <PfRow label="48 hours" m={data.timeWindows["48h"]} />
            </tbody>
          </table>
        </Card>
        <Card title="Drawdown time">
          <Dl
            rows={[
              ["Lookback", `${dd.lookbackDays}d · ${dd.samples} closes`],
              ["Episodes", String(dd.episodes)],
              ["Average", formatDuration(dd.averageDurationMs)],
              ["Maximum", formatDuration(dd.maxDurationMs)],
              ["Current", dd.inDrawdown ? formatDuration(dd.currentDurationMs) : "flat"],
              ["Total underwater", formatDuration(dd.totalDurationMs)],
              ["Max depth", dd.maxDepth.toFixed(4)],
              ["Current depth", dd.currentDepth.toFixed(4)],
              ["All-tape avg", formatDuration(all.averageDurationMs)],
              ["All-tape max", formatDuration(all.maxDurationMs)],
            ]}
          />
        </Card>
      </div>
    </section>
  );
}

const STAGE_SHORT: Record<string, string> = { base: "Base", main: "Main", real: "Real", realNeg: "Real Neg", valid: "Valid" };

/** One line for the Stages hero: per stage the Sets that pass it, their share of all Sets and their own PF (the valid ones, not the overall average). */
function stageLine(live: LiveStats | null | undefined): string {
  const rows = stageFigures(live?.sets?.pfBase).filter((r) => r.key !== "valid");
  if (!rows.length) {
    const st = live?.execution?.sets.stages;
    return st ? `Base ${st.base} · Main ${st.main} · Real ${st.real} · Real Negative ${st.realNeg}` : "";
  }
  return rows.map((r) => `${STAGE_SHORT[r.key]} ${r.sets} (${r.pct.toFixed(1)}%) PF ${formatPf(r.pf)}`).join(" · ");
}

function StagesCard({ live }: { live: LiveStats | null | undefined }) {
  const rows = stageFigures(live?.sets?.pfBase);
  if (!rows.length) return null;
  const all = live?.sets?.pfBase?.stages?.allSets ?? 0;
  return (
    <Card title={`Stages · the Sets that pass each stage (of ${all} configured)`}>
      <table className="w-full text-sm" data-testid="stage-figures">
        <thead className="font-mono text-[11px] text-muted">
          <tr>
            <th className="pb-2 text-left font-medium">Stage</th>
            <th className="pb-2 text-right font-medium">Valid Sets</th>
            <th className="pb-2 text-right font-medium">% of all</th>
            <th className="pb-2 text-right font-medium" title="Gross profit over gross loss of the valid Sets' window orders">PF (valid)</th>
            <th className="pb-2 text-right font-medium" title="Mean of the valid Sets' own PFs">Mean PF</th>
            <th className="pb-2 text-right font-medium">Window orders</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-t border-border font-mono text-xs">
              <td className="py-1.5">{r.label}</td>
              <td className="py-1.5 text-right">{r.sets}</td>
              <td className="py-1.5 text-right">{r.pct.toFixed(1)}%</td>
              <td className={`py-1.5 text-right ${r.pf === null ? "" : r.pf >= 1.1 ? "text-primary" : r.pf < 1 ? "text-danger" : ""}`}>{formatPf(r.pf)}</td>
              <td className="py-1.5 text-right">{formatPf(r.meanPf)}</td>
              <td className="py-1.5 text-right">{r.orders}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted">
        Each row averages only the Sets that pass that stage (never every Set); the share is against all configured Sets. Base is in-sample (the Sets were selected on those orders): read it next to Main and Real.
      </p>
    </Card>
  );
}

function PfRow({ label, m }: { label: string; m: CostPfMetric }) {
  const tone = m.count < 1 ? "ok" : m.pf >= 1.1 ? "good" : m.pf < 1 ? "bad" : "ok";
  return (
    <tr className="border-t border-border">
      <td className="py-2">{label}</td>
      <td className="py-2 text-right font-mono tabular-nums">{m.count}</td>
      <td className="py-2 text-right font-mono tabular-nums">{m.avgR.toFixed(2)}</td>
      <td className={`py-2 text-right font-mono tabular-nums ${tone === "good" ? "text-primary" : tone === "bad" ? "text-danger" : ""}`}>
        {m.count ? formatPf(m.pf) : "—"}
      </td>
      <td className={`py-2 text-right font-mono tabular-nums ${m.count < 1 ? "" : m.netPct >= 0 ? "text-primary" : "text-danger"}`}>
        {m.count < 1 ? "—" : `${m.netPct >= 0 ? "+" : ""}${m.netPct.toFixed(3)}`}
      </td>
      <td className={`py-2 text-right font-mono tabular-nums ${m.count < 1 ? "" : m.netSumPct >= 0 ? "text-primary" : "text-danger"}`}>
        {m.count < 1 ? "—" : `${m.netSumPct >= 0 ? "+" : ""}${m.netSumPct.toFixed(2)}`}
      </td>
    </tr>
  );
}

function Hero({
  k,
  v,
  s,
  tone,
}: {
  k: string;
  v: string;
  s: string;
  tone?: "good" | "bad" | "ok";
}) {
  const cls = tone === "good" ? "text-primary" : tone === "bad" ? "text-danger" : "text-fg";
  return (
    <div className="rounded-radius border border-border bg-surface p-4">
      <p className="font-mono text-xs tracking-wide text-muted uppercase">{k}</p>
      <p className={`mt-1 font-mono text-2xl tabular-nums ${cls}`}>{v}</p>
      <p className="mt-1 text-xs text-muted">{s}</p>
    </div>
  );
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-radius border border-border bg-surface p-4">
      <h2 className="mb-3 text-sm font-medium tracking-wide text-muted uppercase">{title}</h2>
      {children}
    </div>
  );
}

function Dl({ rows }: { rows: [string, string][] }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="text-right font-mono tabular-nums">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
