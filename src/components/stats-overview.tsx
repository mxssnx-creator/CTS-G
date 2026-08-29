import type { ReactNode } from "react";
import type { CostPfMetric } from "@/lib/analytics";
import { formatDuration, type buildOverview } from "@/lib/analytics";
import type { LiveStats } from "@/lib/live-stats";

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
          classicPf: live.pfCost.classicPf ?? data.costPf.classicPf,
          costPct: live.pfCost.costPct ?? data.costPf.costPct,
          netPct: live.pfCost.netPct ?? data.costPf.netPct,
          grossPct: live.pfCost.grossPct ?? data.costPf.grossPct,
          minPf: live.pfCost.minPf ?? data.costPf.minPf,
          pass: live.pfCost.pass ?? data.costPf.pass,
        }
      : {}),
  };
  const heroes = (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Hero
          k="Last 15 PF"
          v={cost.count ? cost.ratio.toFixed(2) : "—"}
          s={`1.00=neutral · 1.10=+1×cost · R ${cost.avgR.toFixed(2)} · n ${cost.count} · ${cost.pass ? "pass" : "block"}`}
          tone={cost.count < 8 ? "ok" : cost.pass ? "good" : "bad"}
        />
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

      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="Profit factor · 1.00=neutral · 1.10=+1×PositionCost">
          <table className="w-full text-sm">
            <thead className="font-mono text-[11px] text-muted">
              <tr>
                <th className="pb-2 text-left font-medium">Window</th>
                <th className="pb-2 text-right font-medium">n</th>
                <th className="pb-2 text-right font-medium">R</th>
                <th className="pb-2 text-right font-medium">PF</th>
                <th className="pb-2 text-right font-medium">Net%</th>
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

function PfRow({ label, m }: { label: string; m: CostPfMetric }) {
  const tone = m.count < 1 ? "ok" : m.ratio >= 1.1 ? "good" : m.ratio < 1 ? "bad" : "ok";
  return (
    <tr className="border-t border-border">
      <td className="py-2">{label}</td>
      <td className="py-2 text-right font-mono tabular-nums">{m.count}</td>
      <td className="py-2 text-right font-mono tabular-nums">{m.avgR.toFixed(2)}</td>
      <td className={`py-2 text-right font-mono tabular-nums ${tone === "good" ? "text-primary" : tone === "bad" ? "text-danger" : ""}`}>
        {m.count ? m.ratio.toFixed(2) : "—"}
      </td>
      <td className={`py-2 text-right font-mono tabular-nums ${m.netPct >= 0 ? "text-primary" : "text-danger"}`}>
        {m.netPct >= 0 ? "+" : ""}
        {m.netPct.toFixed(3)}
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
