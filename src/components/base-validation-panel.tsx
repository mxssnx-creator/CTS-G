import { useMemo, type ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LiveStats } from "@/lib/live-stats";
import { ClientChart } from "@/components/visual-stats";
import { POSITION_COST_PCT_DEFAULT } from "@/lib/analytics";
import { hourlyPf } from "@/lib/hourly-pf";
import { basePackFigures, historicalPlotPf } from "@/lib/base-progress";

const PACK_LABEL: Record<string, string> = {
  indications: "Indications",
  general: "General",
  shortrange: "Short Range",
};

function Panel({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-border bg-surface p-4">
      <h3 className="font-mono text-[11px] tracking-wide text-muted uppercase">{title}</h3>
      {hint ? <p className="mt-1 text-xs text-muted">{hint}</p> : null}
      <div className="mt-3">{children}</div>
    </section>
  );
}

/** Base-only and all-stage cohorts are displayed separately. */
export function BaseValidationPanel({ live, minPf = 1.1 }: { live: LiveStats | null; minPf?: number }) {
  const pb = live?.sets?.pfBase;
  const counts = live?.sets?.counts;
  const cost = live?.pfCost?.costPct ?? POSITION_COST_PCT_DEFAULT;
  const packs = useMemo(
    () =>
      basePackFigures(pb?.byPack).map((v) => ({ ...v, pack: PACK_LABEL[v.id] ?? v.id })),
    [pb],
  );
  const hourly = useMemo(
    () => hourlyPf(live?.closed ?? [], cost, (live as { now?: number } | null)?.now ?? Date.now() / 1000),
    [live, cost],
  );
  const states = useMemo(() => {
    if (!counts) return [];
    const reasons = counts.reasons ?? {};
    const rows = [{ name: "valid", n: counts.valid, color: "var(--color-good, #2fbf71)" }];
    rows.push({ name: "coordination wait", n: counts.waiting ?? 0, color: "var(--color-warn, #e0a030)" });
    for (const [k, v] of Object.entries(reasons)) rows.push({ name: k, n: v, color: "var(--color-muted, #8a93a6)" });
    rows.push({ name: "locked", n: counts.locked, color: "var(--color-warn, #e0a030)" });
    rows.push({ name: "error", n: counts.error, color: "var(--color-danger, #e5484d)" });
    return rows.filter((r) => r.n > 0);
  }, [counts]);
  const progress = useMemo(
    () =>
      (pb?.history ?? []).map((h) => ({
        time: new Date(h.t * 1000).toISOString().slice(11, 16),
        pf: historicalPlotPf(h.pf, h.n),
        validated: h.validatedPct,
      })),
    [pb],
  );
  if (!pb) return null;
  const hoursWithCash = hourly.filter((h) => !h.partial && h.netCash !== null);
  const hoursPositive = hoursWithCash.filter((h) => h.netCash !== null && h.netCash > 0).length;
  const noLossHours = hourly.filter((h) => h.noLoss).length;
  const missingHours = hourly.filter((h) => h.missing > 0).length;
  return (
    <div className="grid gap-3 lg:grid-cols-2" data-testid="base-validation-panel">
      <Panel
        title="Effective PF after Base"
        hint={`${pb.baseOnly?.sets ?? "—"} of ${pb.total ?? pb.unfiltered.sets} Base-validated (${pb.validatedBasePct != null ? pb.validatedBasePct.toFixed(1) + "%" : "—"}) · n ${pb.baseOnly?.n ?? "—"}${pb.eligible != null ? ` · ${pb.eligible} ready for entry` : ""} · ambiguous no-loss sentinels are not plotted`}
      >
        <ClientChart height={200}>
          <BarChart data={packs} margin={{ top: 6, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border, #2a2f3a)" />
            <XAxis dataKey="pack" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} domain={[0, "auto"]} />
            <Tooltip />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <ReferenceLine y={1} stroke="#8a93a6" strokeDasharray="4 2" />
            <ReferenceLine y={minPf} stroke="#e0a030" strokeDasharray="4 2" label={{ value: `min ${minPf}`, fontSize: 10 }} />
            <Bar dataKey="effective" name="Effective (after Base)" fill="#2fbf71" radius={[4, 4, 0, 0]} />
            <Bar dataKey="unfiltered" name="Unfiltered" fill="#6b7385" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ClientChart>
      </Panel>
      <Panel title="Base-validated Sets" hint="Share of each strategy's Sets that pass Base PF and risk validation, independently of later stages and minimal coordination.">
        <ClientChart height={200}>
          <BarChart data={packs} layout="vertical" margin={{ top: 6, right: 16, left: 12, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border, #2a2f3a)" />
            <XAxis type="number" domain={[0, 100]} tick={{ fontSize: 11 }} unit="%" />
            <YAxis type="category" dataKey="pack" tick={{ fontSize: 11 }} width={84} />
            <Tooltip formatter={(v) => `${Number(v).toFixed(1)}%`} />
            <Bar dataKey="validated" name="Validated %" fill="#4c8dff" radius={[0, 4, 4, 0]} />
          </BarChart>
        </ClientChart>
      </Panel>
      <Panel
        title="Recorded closes per hour · UTC"
        hint={`Last 24 clock-hour buckets · ${hoursPositive}/${hoursWithCash.length} completed, measurable close-cash hours strictly positive · ${noLossHours} no-loss return hours not plotted${missingHours ? ` · ${missingHours} hours with missing returns` : ""} · current hour partial; fees may be estimated`}
      >
        <ClientChart height={200}>
          <LineChart data={hourly} margin={{ top: 6, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border, #2a2f3a)" />
            <XAxis dataKey="hour" tick={{ fontSize: 10 }} interval={2} />
            <YAxis tick={{ fontSize: 11 }} domain={[0, "auto"]} />
            <Tooltip />
            <ReferenceLine y={1} stroke="#8a93a6" strokeDasharray="4 2" />
            <ReferenceLine y={minPf} stroke="#e0a030" strokeDasharray="4 2" />
            <Line type="monotone" dataKey="pf" name="PF" stroke="#4c8dff" strokeWidth={2} dot={{ r: 2 }} connectNulls={false} />
          </LineChart>
        </ClientChart>
        <p className="mt-2 text-xs text-muted">Return PF does not weight different order amounts. Close cash below does; neither measures account equity per hour. Historical open-position P&amp;L, funding and complete exchange coverage are not available here. Empty hours are unknown, not profitable.</p>
        <details className="mt-3"><summary className="cursor-pointer text-sm">Hourly amounts and completeness</summary>
          <div className="mt-2 overflow-x-auto"><table className="w-full min-w-xl text-left text-xs">
            <caption className="sr-only">Available recorded closes only, not verified hourly account profit</caption>
            <thead><tr>{["UTC start", "Window", "Closes", "Return PF", "Cash PF", "Close net cash", "Missing return / amount", "Estimated or unknown fees"].map(label => <th scope="col" className="p-2 font-medium" key={label}>{label}</th>)}</tr></thead>
            <tbody>{hourly.map(row => <tr key={row.from} className="border-t border-border">
              <th scope="row" className="p-2 font-medium">{new Date(row.from*1000).toISOString().slice(0,16).replace("T"," ")}</th>
              <td className="p-2">{row.partial ? "Partial" : "Completed"}</td><td className="p-2">{row.n}</td>
              <td className="p-2">{row.pf?.toFixed(3) ?? (row.noLoss ? "No loss" : "—")}</td>
              <td className="p-2">{row.cashPf?.toFixed(3) ?? (row.cashNoLoss ? "No loss" : "—")}</td>
              <td className="p-2">{row.netCash?.toFixed(4) ?? "Unknown"}</td><td className="p-2">{row.missing} / {row.cashMissing}</td><td className="p-2">{row.estimated}</td>
            </tr>)}</tbody>
          </table></div>
        </details>
      </Panel>
      <Panel
        title="Continuous progress"
        hint={`Base-window PF and share of all-stage validated Sets, one point per scoring pass${pb.basis ? " · basis: " + pb.basis : ""} · ambiguous no-loss sentinels are not plotted`}
      >
        <ClientChart height={200}>
          <LineChart data={progress} margin={{ top: 6, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border, #2a2f3a)" />
            <XAxis dataKey="time" tick={{ fontSize: 10 }} minTickGap={24} />
            <YAxis yAxisId="pf" tick={{ fontSize: 11 }} domain={[0, "auto"]} />
            <YAxis yAxisId="pct" orientation="right" tick={{ fontSize: 11 }} unit="%" domain={[0, 100]} />
            <Tooltip />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <ReferenceLine yAxisId="pf" y={minPf} stroke="#e0a030" strokeDasharray="4 2" />
            <Line yAxisId="pf" type="monotone" dataKey="pf" name="Effective PF" stroke="#1f9d5c" strokeWidth={2} dot={false} />
            <Line yAxisId="pct" type="monotone" dataKey="validated" name="All-stage validated %" stroke="#4c8dff" strokeWidth={1.5} dot={false} />
          </LineChart>
        </ClientChart>
      </Panel>
      <Panel title="Set states" hint="Every Set is ready, waiting for minimal coordination, invalid, locked or in error.">
        <ClientChart height={200}>
          <BarChart data={states} margin={{ top: 6, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border, #2a2f3a)" />
            <XAxis dataKey="name" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
            <Tooltip />
            <Bar dataKey="n" name="Sets" radius={[4, 4, 0, 0]}>
              {states.map((r) => (
                <Cell key={r.name} fill={r.color} />
              ))}
            </Bar>
          </BarChart>
        </ClientChart>
      </Panel>
    </div>
  );
}
