import type { ReactNode } from "react";
import type { LiveStats, PfBase, StageFigure, WindowFigure, WindowKey } from "@/lib/live-stats";

/**
 * Three detailed overviews of the Set engine for the desk: Historic (the replayed history behind every verdict), Indications (per pack: Sets,
 * validated share, effective and unfiltered PF) and Strategies · Stages (Base -> Main -> Real -> Real Negative and every last-N window on its own).
 * Every PF is gains over losses of the pooled net results it names; "—" where the engine has not scored yet.
 */
type Progress = {
  phase?: string;
  pct?: number;
  detail?: string;
  cycle?: number;
  lastRunMs?: number;
  elapsedMs?: number;
  symbolsDone?: number;
  symbolsTotal?: number;
  setsTotal?: number;
  ready?: boolean;
  error?: string;
};

export function OverviewSections({ stats }: { stats: LiveStats | null }) {
  const sets = stats?.sets;
  if (!sets) return null;
  const pb = sets.pfBase;
  return (
    <section className="grid gap-3 xl:grid-cols-3" data-testid="overview-sections">
      <Card title="Historic" subtitle="the replayed 1m history behind every Set verdict">
        <Historic stats={stats} />
      </Card>
      <Card title="Indications" subtitle="per pack: Sets, validated share, effective PF after Base">
        <Indications pb={pb} />
      </Card>
      <Card title="Strategies · Stages" subtitle="Base → Main → Real, each stage on its own Sets">
        <Stages pb={pb} reasons={sets.counts?.reasons} />
      </Card>
    </section>
  );
}

function Historic({ stats }: { stats: LiveStats | null }) {
  const s = (stats?.sets ?? {}) as NonNullable<LiveStats["sets"]> & { progress?: Progress; freshS?: number; quality?: { excluded?: number } };
  const p = s.progress ?? {};
  const days = (s.lookback ?? 0) / 1440;
  const hist = s.pfBase?.history ?? [];
  return (
    <div className="grid gap-3">
      <div>
        <div className="flex items-center justify-between font-mono text-xs">
          <span className={s.ready ? "text-primary" : "text-warn"}>{s.ready ? "ready" : "warming up"} · {p.phase ?? "—"}</span>
          <span className="text-muted">{p.detail ?? ""}</span>
        </div>
        <Bar value={p.pct ?? 0} />
      </div>
      <Grid
        rows={[
          ["Lookback", `${s.lookback ?? "—"} bars (${days.toFixed(1)} d)`],
          ["Stage context", `${s.stageContextBars ?? "—"} bars`],
          ["Symbols with full / short context", `${s.stageContextFullSymbols ?? "—"} / ${s.stageContextShortSymbols ?? "—"}`],
          ["Symbols with bars", String(s.barsSymbols ?? "—")],
          ["Replay cycle", `${p.cycle ?? "—"} · last ${p.lastRunMs ? (p.lastRunMs / 1000).toFixed(0) + " s" : "—"}`],
          ["Sets configured", String(s.setCount ?? p.setsTotal ?? "—")],
          ["Verdict freshness", s.freshS ? `${s.freshS.toFixed(0)} s` : "—"],
          ["Symbols excluded (data check)", String(s.quality?.excluded ?? 0)],
        ]}
      />
      {p.error ? <p className="font-mono text-xs text-danger">{p.error}</p> : null}
      {hist.length > 1 ? (
        <div>
          <p className="mb-1 font-mono text-[11px] text-muted uppercase">effective PF · validated % per scoring pass</p>
          <Spark values={hist.map((h) => h.pf)} ref1={1} />
          <Spark values={hist.map((h) => h.validatedPct)} tone="muted" />
        </div>
      ) : null}
    </div>
  );
}

function Indications({ pb }: { pb?: PfBase }) {
  const rows = Object.entries(pb?.byPack ?? {}).sort((a, b) => (b[1].valid ?? 0) - (a[1].valid ?? 0));
  if (!rows.length) return <p className="text-sm text-muted">No pack scored yet.</p>;
  const maxSets = Math.max(...rows.map(([, v]) => v.total || 0), 1);
  return (
    <div className="overflow-x-auto">
      <table className="w-full font-mono text-xs tabular-nums">
        <thead className="text-left text-[10px] text-muted uppercase">
          <tr>
            <th className="py-1 pr-2">pack</th>
            <th className="pr-2 text-right">Sets</th>
            <th className="pr-2 text-right">valid</th>
            <th className="pr-2 text-right">eff. PF</th>
            <th className="pr-2 text-right">unfilt.</th>
            <th className="text-right">floor</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([pack, v]) => (
            <tr key={pack} className="border-t border-border">
              <td className="py-1 pr-2">
                <span className="block max-w-[11rem] truncate" title={pack}>{pack.replace(/^extra\./, "")}</span>
                <span className="mt-0.5 block h-1 rounded bg-bg2">
                  <span className="block h-1 rounded bg-primary" style={{ width: `${Math.min(100, (100 * (v.total || 0)) / maxSets)}%` }} />
                </span>
              </td>
              <td className="pr-2 text-right">{v.total}</td>
              <td className="pr-2 text-right">
                {v.valid} <span className="text-muted">({(v.validatedPct ?? 0).toFixed(1)}%)</span>
              </td>
              <td className={`pr-2 text-right ${pfTone(v.base?.pf, v.base?.n)}`}>{pfText(v.base?.pf, v.base?.n)}</td>
              <td className="pr-2 text-right text-muted">{pfText(v.unfiltered?.pf, v.unfiltered?.n)}</td>
              <td className="text-right text-muted">{v.minPf != null ? v.minPf.toFixed(2) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-[11px] text-muted">eff. PF = the Sets that pass Base, over their window orders (in-sample); the executed PF is on the results page.</p>
    </div>
  );
}

const STAGES: Array<[keyof NonNullable<PfBase["stages"]>, string]> = [
  ["base", "Base"],
  ["main", "Main"],
  ["real", "Real"],
  ["realNeg", "Real Negative"],
  ["valid", "Valid on a lane"],
];
const WINDOWS: Array<[WindowKey, string]> = [
  ["baseLong", "Base long"],
  ["baseShort", "Base short"],
  ["baseMinimal", "Base minimal"],
  ["mainLong", "Main long"],
  ["mainShort", "Main short"],
  ["mainMinimal", "Main minimal"],
  ["negative", "Negative (dip)"],
  ["real", "Real"],
  ["realNeg", "Real Negative"],
];

function Stages({ pb, reasons }: { pb?: PfBase; reasons?: Record<string, number> }) {
  const st = pb?.stages;
  const all = st?.allSets ?? 0;
  return (
    <div className="grid gap-3">
      <div className="grid gap-1.5">
        {STAGES.map(([k, label]) => {
          const f = (st?.[k] ?? null) as StageFigure | null;
          const pct = all ? (100 * (f?.sets ?? 0)) / all : 0;
          return (
            <div key={k} className="font-mono text-xs">
              <div className="flex justify-between">
                <span>{label}</span>
                <span className="tabular-nums">
                  {f?.sets ?? 0} Sets · <span className={pfTone(f?.pf, f?.n)}>PF {pfText(f?.pf, f?.n)}</span>
                  <span className="text-muted"> · {f?.orders ?? 0} orders</span>
                </span>
              </div>
              <Bar value={pct} />
            </div>
          );
        })}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full font-mono text-xs tabular-nums">
          <thead className="text-left text-[10px] text-muted uppercase">
            <tr>
              <th className="py-1 pr-2">last-N window</th>
              <th className="pr-2 text-right">N</th>
              <th className="pr-2 text-right">pass</th>
              <th className="pr-2 text-right">PF</th>
              <th className="text-right">orders</th>
            </tr>
          </thead>
          <tbody>
            {WINDOWS.map(([k, label]) => {
              const w = pb?.windows?.[k] as WindowFigure | undefined;
              if (!w) return null;
              return (
                <tr key={k} className={`border-t border-border ${w.on ? "" : "text-muted"}`}>
                  <td className="py-1 pr-2">{label}</td>
                  <td className="pr-2 text-right">{w.on ? (Array.isArray(w.n) ? w.n.join("/") : (w.n ?? "—")) : "off"}</td>
                  <td className="pr-2 text-right">
                    {w.sets}/{w.pop} <span className="text-muted">({(w.pct ?? 0).toFixed(1)}%)</span>
                  </td>
                  <td className={`pr-2 text-right ${pfTone(w.pf, w.orders)}`}>{pfText(w.pf, w.orders)}</td>
                  <td className="text-right">{w.orders}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {reasons && Object.keys(reasons).length ? (
        <div className="flex flex-wrap gap-1.5 font-mono text-[11px]">
          {Object.entries(reasons)
            .sort((a, b) => b[1] - a[1])
            .map(([r, n]) => (
              <span key={r} className="rounded-lg border border-border bg-bg2 px-2 py-0.5 text-muted">
                {r} {n}
              </span>
            ))}
        </div>
      ) : null}
    </div>
  );
}

function Card({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <div className="min-w-0 rounded-radius border border-border bg-surface p-4">
      <h2 className="text-sm font-medium tracking-wide uppercase">{title}</h2>
      <p className="mb-3 text-[11px] text-muted">{subtitle}</p>
      {children}
    </div>
  );
}

function Grid({ rows }: { rows: Array<[string, string]> }) {
  return (
    <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1 text-xs">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="text-right font-mono tabular-nums">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function Bar({ value }: { value: number }) {
  const v = Math.max(0, Math.min(100, value || 0));
  return (
    <div className="mt-1 h-1.5 rounded bg-bg2">
      <div className="h-1.5 rounded bg-primary" style={{ width: `${v}%` }} />
    </div>
  );
}

function Spark({ values, ref1, tone }: { values: number[]; ref1?: number; tone?: "muted" }) {
  const v = values.filter((x) => Number.isFinite(x));
  if (v.length < 2) return null;
  const lo = Math.min(...v, ref1 ?? Infinity);
  const hi = Math.max(...v, ref1 ?? -Infinity);
  const span = hi - lo || 1;
  const pts = v.map((x, i) => `${(i / (v.length - 1)) * 100},${30 - ((x - lo) / span) * 28 - 1}`).join(" ");
  const y1 = ref1 != null ? 30 - ((ref1 - lo) / span) * 28 - 1 : null;
  return (
    <svg viewBox="0 0 100 30" preserveAspectRatio="none" className="h-8 w-full" aria-hidden="true">
      {y1 != null ? <line x1="0" x2="100" y1={y1} y2={y1} className="stroke-border" strokeWidth="0.5" strokeDasharray="2 2" /> : null}
      <polyline points={pts} fill="none" className={tone === "muted" ? "stroke-muted" : "stroke-primary"} strokeWidth="1" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function pfText(pf: number | null | undefined, n: number | null | undefined): string {
  if (pf == null || !n) return "—";
  return pf >= 99 ? "∞" : pf.toFixed(2);
}

function pfTone(pf: number | null | undefined, n: number | null | undefined): string {
  if (pf == null || !n) return "text-muted";
  return pf >= 1 ? "text-primary" : "text-danger";
}
