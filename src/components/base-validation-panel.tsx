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

/** The post-Base standard on one card row: effective PF vs unfiltered per pack, share of Sets validated, the Set
 * state split and the PF of each of the last 24 hours. The unfiltered PF is shown for reference only: it is under 1
 * by design, because Base validation exists to drop the losing Sets. */
export function BaseValidationPanel({ live, minPf = 1.1 }: { live: LiveStats | null; minPf?: number }) {
  const pb = live?.sets?.pfBase;
  const counts = live?.sets?.counts;
  const cost = live?.pfCost?.costPct ?? POSITION_COST_PCT_DEFAULT;
  const packs = useMemo(
    () =>
      Object.entries(pb?.byPack ?? {}).map(([id, v]) => ({
        pack: PACK_LABEL[id] ?? id,
        effective: v.base.sets ? v.base.pf : null,
        unfiltered: v.unfiltered.pf,
        validated: v.validatedPct,
        valid: v.valid,
        total: v.total,
      })),
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
        pf: h.n ? Math.min(h.pf, 9.99) : null,
        validated: h.validatedPct,
      })),
    [pb],
  );
  if (!pb) return null;
  const hoursWithTrades = hourly.filter((h) => h.n > 0);
  const hoursPositive = hoursWithTrades.filter((h) => (h.pf ?? 0) >= 1).length;
  return (
    <div className="grid gap-3 lg:grid-cols-2" data-testid="base-validation-panel">
      <Panel
        title="Effective PF after Base"
        hint={`${pb.base.sets} of ${pb.total ?? pb.unfiltered.sets} PF-validated (${(pb.validatedPct ?? 0).toFixed(1)}%)${pb.eligible != null ? ` · ${pb.eligible} ready for entry` : ""} · n ${pb.base.n} · before Base ${pb.unfiltered.pf.toFixed(2)}`}
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
      <Panel title="Validated Sets" hint="Share of each strategy's Sets that pass the configured PF and risk validation, independent of minimal coordination.">
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
        title="PF per hour"
        hint={`Last 24 h, net of PositionCost ${cost}% · ${hoursPositive}/${hoursWithTrades.length} hours with trades at PF ≥ 1`}
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
      </Panel>
      <Panel
        title="Continuous progress"
        hint={`Effective PF (rolling gate window of each validated Set) and the validated share over time, one point per scoring pass${pb.basis ? " · basis: " + pb.basis : ""}`}
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
            <Line yAxisId="pct" type="monotone" dataKey="validated" name="Validated %" stroke="#4c8dff" strokeWidth={1.5} dot={false} />
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
