import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
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
import { HistTestControls } from "@/components/hist-test-controls";
import { ComboEvalPanel } from "@/components/combo-eval-panel";
import { ClientChart } from "@/components/visual-stats";
import {
  clampHistTestHours,
  fetchHistTest,
  HIST_TEST_HOURS_DEFAULT,
  HIST_TEST_MIN_PF,
  HIST_TEST_TARGET_DEFAULT,
  histTestIsRunning,
  histTestPollMs,
  pauseHistTest,
  startHistTest,
  stopHistTest,
  type HistTestJob,
} from "@/lib/hist-test";

type StepRow = {
  step: number;
  tpPct?: number;
  pf: number;
  classicPf?: number;
  pfDdRatio: number;
  maxDdS: number;
  wr: number;
  n: number;
  evalN?: number;
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
  bySymbol?: Array<{ symbol: string; pf: number; maxDdS: number; n: number; wr: number; evalN?: number; validated?: boolean }>;
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

type CovBlob = {
  requested?: number;
  completed?: number;
  coveragePct?: number;
  started?: number;
  skipped?: number;
  failed?: number;
  done?: number;
  total?: number;
};

type StatBlob = {
  pf?: number;
  n?: number;
  wr?: number;
  evalN?: number;
  maxDdS?: number;
  validated?: boolean;
};

type SweepReport = {
  phase: string;
  ready?: boolean;
  running?: boolean;
  paused?: boolean;
  ok?: boolean;
  error?: string;
  source?: string;
  detail?: string;
  pct?: number;
  generatedAt?: string;
  hours?: number;
  minPf?: number;
  targetCount?: number;
  filled?: number;
  symbols?: string[];
  positive?: string[];
  rejected?: Array<{ symbol?: string; pf?: number; n?: number; wr?: number } | string>;
  ranked?: Array<{ symbol: string; vol1h: number; vol24h: number; quoteVolume: number; changePct: number; pf?: number; positive?: boolean }>;
  byStep?: StepRow[];
  ranges?: RangeRow[];
  heatmap?: Array<{ step: number; slRatio: number; pf: number; validated?: boolean; maxDdS: number }>;
  bySymbol?: Array<{ symbol: string; pf: number; maxDdS: number; n: number; wr: number; evalN?: number; validated?: boolean }>;
  byDirection?: Record<string, StatBlob>;
  byStrategy?: Record<string, StatBlob>;
  pfStats?: Record<string, StatBlob>;
  withWithout?: Record<string, { with?: StatBlob; without?: StatBlob }>;
  comboMatrix?: Array<{ indication: string; strategy: string; n?: number; pf?: number; wr?: number; evalN?: number; validated?: boolean }>;
  successfulConfigs?: Array<{ indication?: string; config?: string; strategy?: string; setId?: string; pf?: number; n?: number; wr?: number; validated?: boolean; slRatio?: number; step?: number; trailKey?: string }>;
  combo?: { engine?: string; journal?: string; cells?: number; successfulCount?: number };
  bestStep?: StepRow;
  validatedCount?: number;
  rowCount?: number;
  positivePf?: number;
  elapsedMs?: number;
  coverage?: {
    setCount?: number;
    trails?: number;
    sets?: CovBlob;
    symbols?: CovBlob;
    bars?: CovBlob;
    evaluations?: CovBlob;
    tasks?: CovBlob;
    indexed?: boolean;
    independentStrategy?: boolean;
    independentDirection?: boolean;
    independentConfigs?: boolean;
    independentCombo?: boolean;
    independentIndication?: boolean;
    slTpCover?: boolean;
    histFills?: number;
  };
  audit?: {
    ok?: boolean;
    pass?: number;
    fail?: number;
    failed?: string[];
    rows?: Array<{ name?: string; ok?: boolean; detail?: unknown }>;
  };
  heal?: {
    ticks?: number;
    lastAt?: string | number;
    ok?: boolean;
    detail?: string;
    phase?: string;
  };
  fill?: { filled?: number; target?: number; short?: number; evaluated?: number };
};

export const Route = createFileRoute("/step-sweep")({ component: StepSweepPage });

function fmtDd(seconds: number | undefined) {
  const s = Number(seconds || 0);
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(2)}h`;
}

function covPct(blob?: CovBlob) {
  if (typeof blob?.coveragePct === "number" && Number.isFinite(blob.coveragePct)) return blob.coveragePct;
  const requested = Number(blob?.requested ?? blob?.total ?? 0);
  const completed = Number(blob?.completed ?? blob?.done ?? 0);
  if (requested <= 0) return 0;
  return Math.max(0, Math.min(100, (completed / requested) * 100));
}

function covDone(blob?: CovBlob) {
  return Number(blob?.completed ?? blob?.done ?? 0);
}

function covReq(blob?: CovBlob) {
  return Number(blob?.requested ?? blob?.total ?? 0);
}

function yn(value?: boolean) {
  if (value === true) return "yes";
  if (value === false) return "no";
  return "—";
}

function fmtDetail(value: unknown): string {
  if (value == null || value === "") return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value) && value.every((x) => typeof x === "string" || typeof x === "number")) {
    return value.map(String).join(" · ") || "—";
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function StepSweepPage() {
  const [job, setJob] = useState<HistTestJob | null>(null);
  const [data, setData] = useState<SweepReport | null>(null);
  const seqRef = useRef(0);
  useEffect(() => {
    let stop = false;
    const load = async (signal?: AbortSignal) => {
      const seq = seqRef.current;
      try {
        const hist = await fetchHistTest(signal);
        if (stop || signal?.aborted || seq !== seqRef.current) return;
        setJob(hist);
        const hasTape =
          Boolean(hist.ready) ||
          (hist.symbols || []).length > 0 ||
          (hist.bySymbol || []).length > 0 ||
          Boolean(hist.phase && hist.phase !== "idle" && hist.phase !== "stopped");
        if (hasTape) {
          setData(hist as SweepReport);
          return;
        }
        const r = await fetch(`/step-sweep-24h.json?t=${Date.now()}`, { cache: "no-store", signal });
        if (!r.ok || stop || signal?.aborted || seq !== seqRef.current) return;
        const body = (await r.json()) as SweepReport;
        if (body && (body.phase || body.hours || body.symbols)) setData(body);
      } catch {
        /* keep last */
      }
    };
    void load();
    const id = window.setInterval(() => void load(), histTestPollMs(job, document.hidden));
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [job?.phase, job?.paused]);

  const onHistTestControl = async (action: "start" | "stop" | "pause" | "resume") => {
    const seq = ++seqRef.current;
    const hours = clampHistTestHours(job?.hours ?? data?.hours ?? HIST_TEST_HOURS_DEFAULT);
    const minPf = Number(job?.minPf ?? data?.minPf ?? HIST_TEST_MIN_PF) || HIST_TEST_MIN_PF;
    const symbolCap = Math.max(1, Math.round(Number(job?.targetCount ?? data?.targetCount ?? HIST_TEST_TARGET_DEFAULT) || HIST_TEST_TARGET_DEFAULT));
    const next =
      action === "stop"
        ? await stopHistTest()
        : action === "pause"
          ? await pauseHistTest()
          : await startHistTest({ hours, minPf, symbolCap, action: action === "resume" ? "resume" : "start" });
    if (seq !== seqRef.current) return;
    setJob(next);
    if (next.phase && next.phase !== "idle") setData((prev) => ({ ...(prev || {}), ...next } as SweepReport));
  };

  const report = data;
  const hours = job?.hours || report?.hours || HIST_TEST_HOURS_DEFAULT;
  const floor = job?.minPf ?? report?.minPf ?? report?.positivePf ?? HIST_TEST_MIN_PF;
  const steps = report?.byStep ?? [];
  const ranges = report?.ranges ?? [];
  const ranked = report?.ranked ?? [];
  const ready = Boolean(report?.ready) && report?.phase === "ready" && !report?.error;
  const targetCount = job?.targetCount ?? report?.targetCount ?? report?.fill?.target ?? HIST_TEST_TARGET_DEFAULT;
  const filled = job?.filled ?? job?.positive?.length ?? report?.filled ?? report?.positive?.length ?? 0;
  const rejected = report?.rejected ?? [];
  const coverage = report?.coverage;
  const audit = report?.audit;
  const heal = report?.heal;
  const byDirection = Object.entries(report?.byDirection || {});
  const byStrategy = Object.entries(report?.byStrategy || {});
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
  const correctness = [
    { k: "Independent strategy", v: yn(coverage?.independentStrategy), ok: coverage?.independentStrategy },
    { k: "Independent direction", v: yn(coverage?.independentDirection), ok: coverage?.independentDirection },
    { k: "Independent configs", v: yn(coverage?.independentConfigs), ok: coverage?.independentConfigs },
    { k: "Independent combo", v: yn(coverage?.independentCombo), ok: coverage?.independentCombo },
    { k: "Combo engine", v: report?.combo?.engine || "—", ok: report?.combo?.engine === "sqlite-memory" },
    { k: "SL:TP cover", v: yn(coverage?.slTpCover), ok: coverage?.slTpCover },
    { k: "Indexed", v: yn(coverage?.indexed), ok: coverage?.indexed },
    {
      k: "Heal ticks",
      v: [heal?.ticks ?? 0, heal?.phase, heal?.detail, heal?.lastAt != null && heal.lastAt !== "" ? heal.lastAt : null].filter((x) => x != null && x !== "").join(" · "),
      ok: heal?.ok,
    },
    { k: "Hist fills", v: String(coverage?.histFills ?? 0) },
  ];

  return (
    <DeskShell mode={histTestIsRunning(job?.phase) ? String(job?.phase || "TEST").toUpperCase() : ready ? "TEST READY" : "HISTORIC TEST"}>
      <section data-testid="test-historic" className="rounded-radius border-2 border-primary bg-surface p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-primary">Historic test</p>
            <h2 className="mt-1 text-2xl font-semibold tracking-tight text-fg">Start · Pause · Stop</h2>
            <p className="mt-1 text-sm text-muted">
              Same controls as the desk engine bar. Pause holds the run; Stop ends it; Start resumes when paused. Independent of Live and VST.
            </p>
          </div>
          <span className={`shrink-0 rounded-full px-3 py-1 text-sm font-medium ${histTestIsRunning(job?.phase) ? "bg-primary text-bg" : "bg-bg2 text-muted"}`}>
            {job?.phase ? String(job.phase).toUpperCase() : "IDLE"}
          </span>
        </div>
        <div className="mt-4">
          <HistTestControls job={job} hours={hours} minPf={Number(floor) || HIST_TEST_MIN_PF} onControl={onHistTestControl} />
        </div>
      </section>
      <p className="font-mono text-[11px] tracking-wide text-muted uppercase" data-testid="step-sweep-identity">
        {hours}h tape · fill {filled}/{targetCount} · steps {report?.byStep?.length ? `${report.byStep[0]?.step}–${report.byStep[report.byStep.length - 1]?.step}` : "3–12"} · {(job?.positive || job?.symbols || report?.symbols || []).join(" · ") || "ranking 1H vol"} · intern 1.00 · floor {Number(floor).toFixed(2)}
      </p>
      <header className="grid gap-3 lg:grid-cols-4">
        <Hero k="Window" v={`${hours}h`} s="1m bars · historic test" />
        <Hero k="Symbols" v={`${filled} / ${targetCount}`} s={`Positive / ${targetCount}`} />
        <Hero k="Best step" v={String(data?.bestStep?.step ?? "—")} s={`PF ${(data?.bestStep?.pf ?? 0).toFixed(3)} · PF/DD ${(data?.bestStep?.pfDdRatio ?? 0).toFixed(3)}`} good={Boolean(data?.bestStep?.validated)} />
        <Hero k="Validated" v={String(data?.validatedCount ?? 0)} s={data?.detail || `${data?.pct ?? 0}%`} />
      </header>

      {!ready ? (
        <section className="rounded-radius border border-border bg-surface p-4" data-testid="sweep-progress">
          <p className="text-sm text-muted">{data?.error || data?.detail || `Ranking volatility and replaying the ${hours}h book…`}</p>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-bg2">
            <div className="h-full bg-primary" style={{ width: `${Math.max(4, Math.min(100, data?.pct ?? 4))}%` }} />
          </div>
        </section>
      ) : null}

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5" data-testid="sweep-coverage">
        <CovCard label="Symbols" blob={coverage?.symbols} />
        <CovCard label="Sets" blob={coverage?.sets} />
        <CovCard label="Bars" blob={coverage?.bars} />
        <CovCard label="Evals" blob={coverage?.evaluations} />
        <CovCard label="Tasks" blob={coverage?.tasks} />
      </section>

      <section className="grid gap-3 lg:grid-cols-2">
        <section className="rounded-radius border border-border bg-surface p-4">
          <h2 className="mb-3 text-sm font-medium">Correctness · indications / strategies / coord</h2>
          <div className="font-mono text-xs">
            {correctness.map((row) => (
              <div key={row.k} className="flex justify-between gap-3 border-t border-border/60 py-1.5 first:border-t-0">
                <span className="text-muted">{row.k}</span>
                <span className={row.ok === true ? "text-primary" : row.ok === false ? "text-danger" : ""}>{row.v}</span>
              </div>
            ))}
          </div>
        </section>
        <section className="rounded-radius border border-border bg-surface p-4">
          <h2 className="mb-3 text-sm font-medium">Processing audit</h2>
          <Table>
            <thead>
              <tr>
                {["Check", "Result", "Detail"].map((h) => (
                  <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(audit?.rows || []).map((row, i) => (
                <tr key={row.name || i} className="border-t border-border/60">
                  <td className="py-1.5">{row.name || "—"}</td>
                  <td className={row.ok ? "text-primary" : "text-danger"}>{row.ok ? "pass" : "fail"}</td>
                  <td className="font-mono text-xs">{fmtDetail(row.detail)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </section>
      </section>

      {rejected.length ? (
        <section className="rounded-radius border border-border bg-surface p-4" data-testid="sweep-rejected">
          <h2 className="mb-3 text-sm font-medium">Rejected · PF floor 1.10</h2>
          <Table>
            <thead>
              <tr>
                {["Symbol", "PF", "N", "WR"].map((h) => (
                  <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rejected.map((r, i) => {
                const row = typeof r === "string" ? { symbol: r } : r;
                return (
                  <tr key={row.symbol || i} className="border-t border-border/60">
                    <td className="py-1.5">{row.symbol || "—"}</td>
                    <td className="text-muted">{row.pf != null ? row.pf.toFixed(3) : "—"}</td>
                    <td>{row.n ?? "—"}</td>
                    <td>{row.wr != null ? `${row.wr.toFixed(1)}%` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
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
              {["Step", "TP", "PF", "Classic", "PF/DD", "Max DD", "WR", "Eval N", "Fills", "Valid/sets", "Net avg"].map((h) => (
                <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {steps.map((s) => (
              <tr key={s.step} className="border-t border-border/60">
                <td className="py-1.5 font-medium">{s.step}</td>
                <td>{(s.tpPct ?? s.step * 0.1).toFixed(2)}%</td>
                <td className={s.validated ? "text-primary" : "text-muted"}>{s.pf.toFixed(3)}</td>
                <td>{(s.classicPf ?? 0).toFixed(2)}</td>
                <td>{s.pfDdRatio.toFixed(3)}</td>
                <td>{fmtDd(s.maxDdS)}</td>
                <td>{s.wr.toFixed(1)}%</td>
                <td>{s.evalN ?? 0}</td>
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
                <td className={s.validated ? "text-primary" : "text-muted"}>{s.pf.toFixed(3)}</td>
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
              <tr>{["Symbol", "PF", "Max DD", "Eval N", "Fills", "WR"].map((h) => <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>)}</tr>
            </thead>
            <tbody>
              {(data?.bySymbol || []).map((s) => (
                <tr key={s.symbol} className="border-t border-border/60">
                  <td className="py-1.5">{s.symbol}</td>
                  <td className={s.validated ? "text-primary" : "text-muted"}>{s.pf.toFixed(3)}</td>
                  <td>{fmtDd(s.maxDdS)}</td>
                  <td>{s.evalN ?? 0}</td>
                  <td>{s.n}</td>
                  <td>{s.wr.toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </section>
      ) : null}

      {byDirection.length || byStrategy.length ? (
        <section className="grid gap-3 lg:grid-cols-2">
          {byDirection.length ? (
            <section className="rounded-radius border border-border bg-surface p-4">
              <h2 className="mb-3 text-sm font-medium">Direction</h2>
              <Table>
                <thead>
                  <tr>
                    {["Side", "PF", "Eval N", "Fills", "WR", "Max DD"].map((h) => (
                      <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {byDirection.map(([k, v]) => (
                    <tr key={k} className="border-t border-border/60">
                      <td className="py-1.5">{k}</td>
                      <td className={v.validated ? "text-primary" : "text-muted"}>{(v.pf ?? 0).toFixed(3)}</td>
                      <td>{v.evalN ?? 0}</td>
                      <td>{v.n ?? 0}</td>
                      <td>{(v.wr ?? 0).toFixed(1)}%</td>
                      <td>{fmtDd(v.maxDdS)}</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </section>
          ) : null}
          {byStrategy.length ? (
            <section className="rounded-radius border border-border bg-surface p-4">
              <h2 className="mb-3 text-sm font-medium">Strategy</h2>
              <Table>
                <thead>
                  <tr>
                    {["Lane", "PF", "Eval N", "Fills", "WR"].map((h) => (
                      <th key={h} className="py-1 text-left font-mono text-[11px] uppercase tracking-wide text-muted">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {byStrategy.map(([k, v]) => (
                    <tr key={k} className="border-t border-border/60">
                      <td className="py-1.5">{k}</td>
                      <td className={v.validated ? "text-primary" : "text-muted"}>{(v.pf ?? 0).toFixed(3)}</td>
                      <td>{v.evalN ?? 0}</td>
                      <td>{v.n ?? 0}</td>
                      <td>{(v.wr ?? 0).toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </section>
          ) : null}
        </section>
      ) : null}

      <ComboEvalPanel job={report} />

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
                  <td className={row.validated ? "text-primary" : "text-muted"}>{row.pf.toFixed(3)}</td>
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

function CovCard({ label, blob }: { label: string; blob?: CovBlob }) {
  const pct = covPct(blob);
  const req = covReq(blob);
  const done = covDone(blob);
  const counts = req <= 0 && done <= 0 ? "—" : `${done} / ${req}`;
  return (
    <article className="rounded-radius border border-border bg-surface p-4">
      <div className="font-mono text-[11px] uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{pct.toFixed(0)}%</div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-bg2">
        <div className="h-full bg-primary" style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
      </div>
      <div className="mt-2 font-mono text-xs text-muted">{counts}</div>
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
