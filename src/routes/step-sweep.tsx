import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Area,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { DeskShell } from "@/components/desk-shell";
import { ClientChart } from "@/components/visual-stats";

type StepRow = {
  step: number;
  tpPct?: number;
  pf: number;
  classicPf?: number;
  pfDdRatio: number;
  maxDdS: number;
  wr: number;
  n: number;
  sets?: number;
  validatedSets?: number;
  netAvg?: number;
  validated?: boolean;
  listings?: Array<{
    id: string;
    pack: string;
    kind: string;
    slRatio: number;
    trailKey: string;
    n: number;
    pf: number;
    maxDdS: number;
    wr: number;
    validated?: boolean;
  }>;
  listingCount?: number;
  bySymbol?: Array<{ symbol: string; pf: number; maxDdS: number; n: number; wr: number; validated?: boolean }>;
  bySide?: Record<string, { pf: number; maxDdS: number; n: number; wr: number; validated?: boolean }>;
};

type RangeRow = {
  label: string;
  stepCount: number;
  pf: number;
  pfDdRatio: number;
  maxDdS: number;
  wr: number;
  n: number;
  sets?: number;
  validatedSets?: number;
  netAvg?: number;
  validated?: boolean;
};

type SweepReport = {
  phase: string;
  ready?: boolean;
  error?: string;
  source?: string;
  detail?: string;
  pct?: number;
  generatedAt?: string;
  hours?: number;
  symbols?: string[];
  ranked?: Array<{ symbol: string; vol1h: number; vol24h: number; quoteVolume: number; changePct: number }>;
  byStep?: StepRow[];
  ranges?: RangeRow[];
  heatmap?: Array<{ step: number; slRatio: number; pf: number; validated?: boolean; maxDdS: number }>;
  bySymbol?: Array<{ symbol: string; pf: number; maxDdS: number; n: number; wr: number; validated?: boolean }>;
  bestStep?: StepRow;
  validatedCount?: number;
  rowCount?: number;
  positivePf?: number;
  elapsedMs?: number;
  coverage?: { setCount?: number; trails?: number };
};

export const Route = createFileRoute("/step-sweep")({ component: StepSweepPage });

function fmtDd(seconds: number | undefined) {
  const s = Number(seconds || 0);
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(2)}h`;
}

function StepSweepPage() {
  const [data, setData] = useState<SweepReport | null>(null);
  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const r = await fetch(`/step-sweep-24h.json?t=${Date.now()}`, { cache: "no-store" });
        if (!r.ok) return;
        const json = (await r.json()) as SweepReport;
        if (!stop) setData(json);
      } catch {
        /* keep last */
      }
    };
    void load();
    const id = window.setInterval(load, 4000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, []);

  const steps = data?.byStep ?? [];
  const ranges = data?.ranges ?? [];
  const ranked = data?.ranked ?? [];
  const ready = Boolean(data?.ready) && data?.phase === "ready" && !data?.error;
  const chart = useMemo(
    () =>
      steps.map((s) => ({
        step: s.step,
        pf: s.pf,
        ddMin: (s.maxDdS || 0) / 60,
        pfDd: s.pfDdRatio,
        wr: s.wr,
        fills: s.n,
        valid: s.validatedSets || 0,
      })),
    [steps],
  );
  const heat = data?.heatmap ?? [];
  const sls = Array.from(new Set(heat.map((c) => Number(c.slRatio)))).sort((a, b) => a - b);
  const stepIds = Array.from(new Set(heat.map((c) => Number(c.step)))).sort((a, b) => a - b);
  const heatLookup = new Map(heat.map((c) => [`${c.step}:${Number(c.slRatio).toFixed(1)}`, c]));
  const pfMin = Math.min(...heat.map((c) => c.pf), 0);
  const pfMax = Math.max(...heat.map((c) => c.pf), 1);

  return (
    <DeskShell live={ready} mode={ready ? "SWEEP READY" : (data?.phase || "SWEEP").toUpperCase()}>
      <p className="font-mono text-[11px] tracking-wide text-muted uppercase" data-testid="step-sweep-identity">
        24h historic · steps 3–12 · {data?.symbols?.join(" · ") || "ranking 1H vol"} · intern 1.00 · floor {data?.positivePf ?? 1.1}
      </p>
      <header className="grid gap-3 lg:grid-cols-4">
        <Hero k="Window" v="24h" s="1m bars · default hist book" />
        <Hero k="Symbols" v={String(data?.symbols?.length || 0)} s={(data?.symbols || []).join(" ") || "fetching ticker"} />
        <Hero k="Best step" v={String(data?.bestStep?.step ?? "—")} s={`PF ${(data?.bestStep?.pf ?? 0).toFixed(3)} · PF/DD ${(data?.bestStep?.pfDdRatio ?? 0).toFixed(3)}`} good={Boolean(data?.bestStep?.validated)} />
        <Hero k="Validated" v={String(data?.validatedCount ?? 0)} s={data?.detail || `${data?.pct ?? 0}%`} />
      </header>

      {!ready ? (
        <section className="rounded-radius border border-border bg-surface p-4">
          <p className="text-sm text-muted">{data?.error || data?.detail || "Ranking volatility and replaying the 24h book…"}</p>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-bg2">
            <div className="h-full bg-primary" style={{ width: `${Math.max(4, Math.min(100, data?.pct ?? 4))}%` }} />
          </div>
        </section>
      ) : null}

      <section className="rounded-radius border border-border bg-surface p-4">
        <h2 className="mb-3 text-sm font-medium">Most volatile USDT perps (1H range, then 24H)</h2>
        <table className="w-full text-sm">
          <thead className="font-mono text-[11px] uppercase tracking-wide text-muted">
            <tr><th className="py-1 text-left">#</th><th className="text-left">Symbol</th><th className="text-left">1H vol</th><th className="text-left">24H</th><th className="text-left">Quote</th></tr>
          </thead>
          <tbody>
            {ranked.map((r, i) => (
              <tr key={r.symbol} className="border-t border-border/60">
                <td className="py-1.5">{i + 1}</td>
                <td className="font-medium">{r.symbol}</td>
                <td className="font-mono">{r.vol1h?.toFixed(3)}%</td>
                <td className="font-mono">{r.vol24h?.toFixed(3)}%</td>
                <td className="font-mono">{((r.quoteVolume || 0) / 1e6).toFixed(1)}m</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="grid gap-3 lg:grid-cols-2">
        <ChartCard title="PF and drawdown by step">
          <ClientChart height={260}>
            <ComposedChart data={chart}>
              <CartesianGrid stroke="var(--color-grid)" />
              <XAxis dataKey="step" stroke="var(--color-muted)" />
              <YAxis yAxisId="l" stroke="var(--color-primary)" />
              <YAxis yAxisId="r" orientation="right" stroke="var(--color-danger)" />
              <Tooltip />
              <Legend />
              <Area yAxisId="l" type="monotone" dataKey="pf" name="PF" stroke="var(--color-primary)" fill="var(--color-primary)" fillOpacity={0.18} />
              <Line yAxisId="r" type="monotone" dataKey="ddMin" name="Max DD min" stroke="var(--color-danger)" dot={false} />
            </ComposedChart>
          </ClientChart>
        </ChartCard>
        <ChartCard title="PF/DD ratio and win rate">
          <ClientChart height={260}>
            <ComposedChart data={chart}>
              <CartesianGrid stroke="var(--color-grid)" />
              <XAxis dataKey="step" stroke="var(--color-muted)" />
              <YAxis />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="pfDd" name="PF/DD" stroke="#62e6ff" />
              <Line type="monotone" dataKey="wr" name="Win %" stroke="#e0b15a" />
            </ComposedChart>
          </ClientChart>
        </ChartCard>
        <ChartCard title="Multi-dimension · PF vs drawdown">
          <ClientChart height={280}>
            <ScatterChart>
              <CartesianGrid stroke="var(--color-grid)" />
              <XAxis dataKey="pf" name="PF" stroke="var(--color-muted)" />
              <YAxis dataKey="ddMin" name="DD min" stroke="var(--color-muted)" />
              <Tooltip cursor={{ strokeDasharray: "3 3" }} />
              <Scatter data={chart} fill="var(--color-primary)">
                {chart.map((s) => (
                  <Cell key={s.step} fill="var(--color-primary)" />
                ))}
              </Scatter>
            </ScatterChart>
          </ClientChart>
        </ChartCard>
        <ChartCard title="Radar · PF, PF/DD, WR, validated">
          <ClientChart height={280}>
            <RadarChart data={chart}>
              <PolarGrid stroke="var(--color-border)" />
              <PolarAngleAxis dataKey="step" stroke="var(--color-muted)" />
              <PolarRadiusAxis stroke="var(--color-faint)" />
              <Radar name="PF" dataKey="pf" stroke="var(--color-primary)" fill="var(--color-primary)" fillOpacity={0.18} />
              <Radar name="PF/DD" dataKey="pfDd" stroke="#62e6ff" fill="#62e6ff" fillOpacity={0.08} />
              <Radar name="WR" dataKey="wr" stroke="#e0b15a" fill="transparent" />
            </RadarChart>
          </ClientChart>
        </ChartCard>
      </section>

      <section className="rounded-radius border border-border bg-surface p-4">
        <h2 className="mb-3 text-sm font-medium">Each step (3, 4, … 12)</h2>
        <Table>
          <thead>
            <tr>
              {["Step", "TP", "PF", "Classic", "PF/DD", "Max DD", "WR", "Fills", "Valid/sets", "Net avg"].map((h) => (
                <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {steps.map((s) => (
              <tr key={s.step} className="border-t border-border/60">
                <td className="py-1.5 font-medium">{s.step}</td>
                <td>{(s.tpPct ?? s.step * 0.1).toFixed(2)}%</td>
                <td className={s.validated ? "text-primary" : "text-danger"}>{s.pf.toFixed(3)}</td>
                <td>{(s.classicPf ?? 0).toFixed(2)}</td>
                <td>{s.pfDdRatio.toFixed(3)}</td>
                <td>{fmtDd(s.maxDdS)}</td>
                <td>{s.wr.toFixed(1)}%</td>
                <td>{s.n}</td>
                <td>{s.validatedSets}/{s.sets}</td>
                <td>{(s.netAvg ?? 0).toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      </section>

      <section className="rounded-radius border border-border bg-surface p-4">
        <h2 className="mb-2 text-sm font-medium">Step range count · 3 → N</h2>
        <p className="mb-3 text-sm text-muted">Each row adds one TP step onto the same 24h tape. Count 10 is the full 3–12 grid.</p>
        <Table>
          <thead>
            <tr>
              {["Range", "Count", "PF", "PF/DD", "Max DD", "WR", "Fills", "Valid/sets"].map((h) => (
                <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ranges.map((s) => (
              <tr key={s.label} className="border-t border-border/60">
                <td className="py-1.5 font-medium">{s.label}</td>
                <td>{s.stepCount}</td>
                <td className={s.validated ? "text-primary" : "text-danger"}>{s.pf.toFixed(3)}</td>
                <td>{s.pfDdRatio.toFixed(3)}</td>
                <td>{fmtDd(s.maxDdS)}</td>
                <td>{s.wr.toFixed(1)}%</td>
                <td>{s.n}</td>
                <td>{s.validatedSets}/{s.sets}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      </section>

      {heat.length ? (
        <section className="overflow-auto rounded-radius border border-border bg-surface p-4">
          <h2 className="mb-3 text-sm font-medium">Heatmap · step × SL:TP (cost-net PF)</h2>
          <table className="w-max min-w-full text-center font-mono text-[11px]">
            <thead>
              <tr>
                <th className="px-1 py-1 text-muted">Step \\ SL</th>
                {sls.map((sl) => <th key={sl} className="px-1 py-1 text-muted">{sl.toFixed(1)}</th>)}
              </tr>
            </thead>
            <tbody>
              {stepIds.map((step) => (
                <tr key={step}>
                  <th className="px-1 py-1 text-left text-muted">{step}</th>
                  {sls.map((sl) => {
                    const cell = heatLookup.get(`${step}:${sl.toFixed(1)}`);
                    const pf = cell?.pf ?? 0;
                    const t = (pf - pfMin) / ((pfMax - pfMin) || 1);
                    return (
                      <td
                        key={sl}
                        title={`step ${step} SL ${sl} PF ${pf.toFixed(3)}`}
                        style={{ background: `hsla(${cell?.validated ? 152 : 8},70%,${18 + t * 32}%,.9)` }}
                        className="px-1 py-1"
                      >
                        {cell ? pf.toFixed(2) : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {(data?.bySymbol || []).length ? (
        <section className="rounded-radius border border-border bg-surface p-4">
          <h2 className="mb-3 text-sm font-medium">Per-symbol 24h tape</h2>
          <Table>
            <thead>
              <tr>{["Symbol", "PF", "Max DD", "Fills", "WR"].map((h) => <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>)}</tr>
            </thead>
            <tbody>
              {(data?.bySymbol || []).map((s) => (
                <tr key={s.symbol} className="border-t border-border/60">
                  <td className="py-1.5">{s.symbol}</td>
                  <td className={s.validated ? "text-primary" : "text-danger"}>{s.pf.toFixed(3)}</td>
                  <td>{fmtDd(s.maxDdS)}</td>
                  <td>{s.n}</td>
                  <td>{s.wr.toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </section>
      ) : null}

      {steps.map((s) => (
        <section key={`list-${s.step}`} className="rounded-radius border border-border bg-surface p-4">
          <h2 className="mb-3 text-sm font-medium">Step {s.step} listings · TP {(s.tpPct ?? s.step * 0.1).toFixed(2)}% · {s.validatedSets}/{s.listingCount || s.sets} validated</h2>
          <Table>
            <thead>
              <tr>{["#", "Set", "Pack", "Kind", "SL", "Trail", "PF", "DD", "Fills"].map((h) => <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>)}</tr>
            </thead>
            <tbody>
              {(s.listings || []).map((row, i) => (
                <tr key={row.id} className="border-t border-border/60">
                  <td className="py-1.5">{i + 1}</td>
                  <td className="font-mono text-xs">{row.id}</td>
                  <td>{row.pack}</td>
                  <td>{row.kind}</td>
                  <td>{row.slRatio.toFixed(1)}</td>
                  <td>{row.trailKey || "base"}</td>
                  <td className={row.validated ? "text-primary" : "text-danger"}>{row.pf.toFixed(3)}</td>
                  <td>{fmtDd(row.maxDdS)}</td>
                  <td>{row.n}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </section>
      ))}

      <p className="text-xs text-muted">
        Independent hist_calc · default settings · axes off · Block on · DCA off · intern PF 1.00 identity · live floor {data?.positivePf ?? 1.1} · no live flatten · {data?.source || "pending"} · {(Number(data?.elapsedMs || 0) / 1000).toFixed(0)}s
      </p>
    </DeskShell>
  );
}

function Hero({ k, v, s, good }: { k: string; v: string; s: string; good?: boolean }) {
  return (
    <article className="rounded-radius border border-border bg-surface p-4">
      <div className="font-mono text-[11px] uppercase tracking-wide text-muted">{k}</div>
      <div className={`mt-1 text-2xl font-semibold ${good ? "text-primary" : ""}`}>{v}</div>
      <div className="text-xs text-muted">{s}</div>
    </article>
  );
}

function ChartCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-radius border border-border bg-surface p-4">
      <h2 className="mb-2 text-sm font-medium">{title}</h2>
      {children}
    </section>
  );
}

function Table({ children }: { children: ReactNode }) {
  return <table className="w-full text-sm">{children}</table>;
}
