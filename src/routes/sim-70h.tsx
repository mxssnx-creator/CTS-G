import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { DeskShell } from "@/components/desk-shell";
import { ClientChart } from "@/components/visual-stats";
import { INDICATION_GROUPS, STRATEGY_GROUPS } from "@/lib/set-overview";
import { STRATEGY_COLORS } from "@/lib/dimension-stats";

export const Route = createFileRoute("/sim-70h")({ component: Sim70hPage });

const LAST_N = [10, 20, 30, 40, 50, 60, 70] as const;
const FAMILY_COLORS: Record<string, string> = {
  overall: "#d9f0e6",
  normal: STRATEGY_COLORS.normal,
  trailing: STRATEGY_COLORS.trailing,
  axis: STRATEGY_COLORS.axis,
  block: STRATEGY_COLORS.block,
  dca: STRATEGY_COLORS.dca,
};

type WindowCell = {
  requestedN?: number;
  n?: number;
  pf?: number | null;
  wr?: number | null;
  available?: boolean;
  validated?: boolean;
  evalN?: number;
  maxDdS?: number | null;
};
type WindowMap = Record<string, WindowCell>;
type GroupBlob = { n?: number; windows?: WindowMap };
type MatrixCell = { indication: string; strategy: string; n?: number; windows?: WindowMap };
type SuccessRow = { setId?: string; indication?: string; strategy?: string; n?: number; pf?: number; wr?: number };
type PricePt = { t?: number; c?: number };
type SimReport = {
  ok?: boolean;
  phase?: string;
  pct?: number;
  hours?: number;
  lookback?: number;
  minPf?: number;
  lastN?: number[];
  symbols?: string[];
  source?: string;
  elapsedS?: number;
  setCount?: number;
  processedCount?: number;
  histFills?: number;
  comboCount?: number;
  validatedByN?: Record<string, number>;
  overallWindows?: WindowMap;
  byIndication?: Record<string, GroupBlob>;
  byStrategy?: Record<string, GroupBlob>;
  bySide?: Record<string, GroupBlob>;
  byPack?: Record<string, GroupBlob>;
  bySl?: Record<string, GroupBlob>;
  byStep?: Record<string, GroupBlob>;
  byTrail?: Record<string, GroupBlob>;
  bySymbol?: Record<string, GroupBlob>;
  byType?: Record<string, GroupBlob>;
  combo?: {
    matrix?: MatrixCell[];
    families?: Record<string, GroupBlob>;
    withWithout?: Record<string, { with?: GroupBlob; without?: GroupBlob }>;
    successfulByN?: Record<string, SuccessRow[]>;
    coverage?: {
      indications?: Record<string, boolean>;
      strategies?: Record<string, boolean>;
      families?: Record<string, boolean>;
    };
  };
  pricePaths?: Record<string, PricePt[]>;
  issues?: string[];
  errors?: Record<string, string>;
};
type Progress = { phase?: string; pct?: number; detail?: string; elapsedS?: number; ok?: boolean };

function pfClass(pf?: number | null, validated?: boolean) {
  if (pf == null || !Number.isFinite(pf)) return "text-muted";
  if (validated || pf >= 1.15) return "text-primary";
  if (pf >= 1) return "text-warn";
  return "text-danger";
}

function cell(group: GroupBlob | WindowMap | undefined, n: number): WindowCell {
  const windows = (group && "windows" in (group as GroupBlob) ? (group as GroupBlob).windows : (group as WindowMap)) || {};
  return windows[`last${n}`] || {};
}

function Sim70hPage() {
  const [report, setReport] = useState<SimReport | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [nFocus, setNFocus] = useState<number>(30);
  const ready = report?.phase === "ready";

  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const [pRes, jRes] = await Promise.all([
          fetch("/sim-70h.progress.json", { cache: "no-store" }),
          fetch("/sim-70h.json", { cache: "no-store" }),
        ]);
        if (pRes.ok) setProgress(await pRes.json());
        if (jRes.ok) {
          const body = (await jRes.json()) as SimReport;
          if (body?.phase === "ready" || body?.overallWindows) setReport(body);
        }
      } catch {
        /* keep last snapshot */
      }
    };
    void pull();
    const id = window.setInterval(() => {
      if (stop) return;
      void pull();
    }, 2000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, []);

  const lastN = report?.lastN?.length ? report.lastN : [...LAST_N];
  const familyChart = useMemo(() => {
    const families = report?.combo?.families || {};
    return lastN.map((n) => {
      const row: Record<string, number | string> = { n: `last ${n}` };
      const overall = cell(report?.overallWindows, n).pf;
      if (overall != null) row.overall = overall;
      for (const name of ["normal", "trailing", "axis", "block", "dca"]) {
        const pf = cell(families[name], n).pf;
        if (pf != null) row[name] = pf;
      }
      return row;
    });
  }, [report, lastN]);
  const indicationChart = useMemo(() => {
    const groups = report?.byIndication || {};
    return lastN.map((n) => {
      const row: Record<string, number | string> = { n: `last ${n}` };
      for (const kind of INDICATION_GROUPS) {
        const pf = cell(groups[kind], n).pf;
        if (pf != null) row[kind] = pf;
      }
      return row;
    });
  }, [report, lastN]);
  const symbolChart = useMemo(() => {
    const groups = report?.bySymbol || {};
    return lastN.map((n) => {
      const row: Record<string, number | string> = { n: `last ${n}` };
      for (const symbol of report?.symbols || []) {
        const pf = cell(groups[symbol], n).pf;
        if (pf != null) row[symbol] = pf;
      }
      return row;
    });
  }, [report, lastN]);

  const pct = Number(progress?.pct ?? report?.pct ?? 0);
  const running = !ready && Boolean(progress?.phase && progress.phase !== "error");

  return (
    <DeskShell>
      <p className="font-mono text-[11px] tracking-wide text-muted uppercase" data-testid="sim70-identity">
        70h simulation · 5 symbols · last-N 10–70 step 10 · {report?.source || progress?.phase || "queued"}
      </p>

      <section className="rounded-radius border border-border bg-surface p-4" data-testid="sim70-progress">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Complete trade test</h2>
            <p className="mt-1 max-w-2xl text-sm text-muted">
              {progress?.detail || (ready ? "Replay finished. Every indication, strategy, type and last-N window is below." : "Waiting for the 70-hour replay.")}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <a href="/sim-70h.html" target="_blank" rel="noreferrer" className="inline-flex min-h-11 items-center rounded-lg bg-primary px-4 text-sm font-medium text-bg">
              Open HTML report
            </a>
            <a href="/sim-70h.html" download="cts-g-70h-report.html" className="inline-flex min-h-11 items-center rounded-lg border border-border px-4 text-sm">
              Download HTML
            </a>
            <a href="/sim-70h.json" download="cts-g-70h-report.json" className="inline-flex min-h-11 items-center rounded-lg border border-border px-4 text-sm">
              Download JSON
            </a>
          </div>
        </div>
        <div className="mt-4 h-2 overflow-hidden rounded-full bg-bg2">
          <div className="h-full rounded-full bg-primary" style={{ width: `${Math.max(2, Math.min(100, pct))}%` }} />
        </div>
        <p className="mt-2 font-mono text-xs text-muted">
          {running ? `Running · ${pct}%` : ready ? `Ready · ${report?.elapsedS ?? "—"}s` : `${progress?.phase || "queued"} · ${pct}%`}
        </p>
      </section>

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Hero k="Window" v="70h" s={`${Number(report?.lookback || 4200).toLocaleString("en-US")} 1m bars`} />
        <Hero k="Symbols" v={String(report?.symbols?.length || 5)} s={(report?.symbols || []).join(" · ") || "BTC ETH SOL XRP BCH"} />
        <Hero k="Sets" v={Number(report?.processedCount || report?.setCount || 0).toLocaleString("en-US")} s={`${Number(report?.histFills || 0).toLocaleString("en-US")} fills`} />
        <Hero k="Validated @ 30" v={String(report?.validatedByN?.last30 ?? "—")} s={`floor PF ${report?.minPf ?? 1.15}`} />
      </section>

      <FocusN value={nFocus} onChange={setNFocus} />

      <section className="min-w-0 rounded-radius border border-border bg-surface p-4">
        <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Overall and strategy families · PF vs last-N</h2>
        <p className="mt-1 mb-3 text-sm text-muted">Cost-net last-N. 1.00 is cost-neutral. 1.15 is the live floor. Windows are independent.</p>
        {familyChart.some((row) => Object.keys(row).length > 1) ? (
          <ClientChart height={280}>
            <LineChart data={familyChart} margin={{ top: 8, right: 12, left: 0, bottom: 8 }}>
              <CartesianGrid stroke="var(--color-border)" />
              <XAxis dataKey="n" tick={{ fill: "var(--color-muted)", fontSize: 11 }} />
              <YAxis domain={[0.6, 1.8]} tick={{ fill: "var(--color-muted)", fontSize: 11 }} width={42} />
              <Tooltip contentStyle={{ background: "var(--color-surface)", border: "1px solid var(--color-border)" }} />
              <Legend />
              {Object.entries(FAMILY_COLORS).map(([key, color]) => (
                <Line key={key} type="monotone" dataKey={key} stroke={color} dot={false} strokeWidth={key === "overall" ? 2.4 : 1.6} isAnimationActive={false} />
              ))}
            </LineChart>
          </ClientChart>
        ) : (
          <Empty text={running ? "Charts fill as the replay scores last-N windows." : "No family PF yet."} />
        )}
        <WindowTable label="Family" groups={{ overall: { windows: report?.overallWindows }, ...(report?.combo?.families || {}) }} lastN={lastN} />
      </section>

      <section className="min-w-0 rounded-radius border border-border bg-surface p-4">
        <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Indications · PF vs last-N</h2>
        <p className="mt-1 mb-3 text-sm text-muted">Each kind is an independent tape: state, signals, active, direction, move, common, trend, break, plus general/combined.</p>
        {indicationChart.some((row) => Object.keys(row).length > 1) ? (
          <ClientChart height={300}>
            <LineChart data={indicationChart} margin={{ top: 8, right: 12, left: 0, bottom: 8 }}>
              <CartesianGrid stroke="var(--color-border)" />
              <XAxis dataKey="n" tick={{ fill: "var(--color-muted)", fontSize: 11 }} />
              <YAxis domain={[0.5, 2]} tick={{ fill: "var(--color-muted)", fontSize: 11 }} width={42} />
              <Tooltip contentStyle={{ background: "var(--color-surface)", border: "1px solid var(--color-border)" }} />
              <Legend />
              {INDICATION_GROUPS.map((kind, i) => (
                <Line key={kind} type="monotone" dataKey={kind} stroke={IND_COLORS[i % IND_COLORS.length]} dot={false} strokeWidth={1.5} isAnimationActive={false} />
              ))}
            </LineChart>
          </ClientChart>
        ) : (
          <Empty text="Indication series appear after scoring." />
        )}
        <WindowTable label="Indication" groups={report?.byIndication || {}} lastN={lastN} />
      </section>

      <Heatmaps matrix={report?.combo?.matrix || []} n={nFocus} />

      <section className="min-w-0 grid gap-3 lg:grid-cols-2">
        <Card title="Strategies">
          <WindowTable label="Strategy" groups={report?.byStrategy || {}} lastN={lastN} />
        </Card>
        <Card title="Types · side / pack / trail">
          <WindowTable label="Type" groups={report?.byType || {}} lastN={lastN} />
        </Card>
        <Card title="Sides">
          <WindowTable label="Side" groups={report?.bySide || {}} lastN={lastN} />
        </Card>
        <Card title="Packs">
          <WindowTable label="Pack" groups={report?.byPack || {}} lastN={lastN} />
        </Card>
        <Card title="SL:TP ratios">
          <WindowTable label="SL" groups={report?.bySl || {}} lastN={lastN} />
        </Card>
        <Card title="TP steps">
          <WindowTable label="Step" groups={report?.byStep || {}} lastN={lastN} />
        </Card>
      </section>

      <section className="min-w-0 rounded-radius border border-border bg-surface p-4">
        <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Symbols · PF vs last-N</h2>
        {symbolChart.some((row) => Object.keys(row).length > 1) ? (
          <ClientChart height={260}>
            <LineChart data={symbolChart} margin={{ top: 8, right: 12, left: 0, bottom: 8 }}>
              <CartesianGrid stroke="var(--color-border)" />
              <XAxis dataKey="n" tick={{ fill: "var(--color-muted)", fontSize: 11 }} />
              <YAxis domain={[0.5, 2]} tick={{ fill: "var(--color-muted)", fontSize: 11 }} width={42} />
              <Tooltip contentStyle={{ background: "var(--color-surface)", border: "1px solid var(--color-border)" }} />
              <Legend />
              {(report?.symbols || []).map((symbol, i) => (
                <Line key={symbol} type="monotone" dataKey={symbol} stroke={IND_COLORS[i % IND_COLORS.length]} dot={false} strokeWidth={1.8} isAnimationActive={false} />
              ))}
            </LineChart>
          </ClientChart>
        ) : (
          <Empty text="Symbol PF fills after replay." />
        )}
        <WindowTable label="Symbol" groups={report?.bySymbol || {}} lastN={lastN} />
        <PricePaths paths={report?.pricePaths || {}} />
      </section>

      <WithWithout blob={report?.combo?.withWithout} lastN={lastN} />
      <Successful n={nFocus} rows={(report?.combo?.successfulByN || {})[`last${nFocus}`] || []} validatedByN={report?.validatedByN || {}} />

      <section className="rounded-radius border border-border bg-surface p-4" data-testid="sim70-html">
        <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Standalone HTML report</h2>
        <p className="mt-1 mb-3 text-sm text-muted">Same numbers, with SVG diagrams for every indication, strategy, type and last-N window.</p>
        <iframe title="70-hour HTML report" src="/sim-70h.html" className="h-[640px] w-full rounded-lg border border-border bg-bg sm:h-[820px]" />
      </section>

      {(report?.issues?.length || report?.errors) ? (
        <section className="rounded-radius border border-border bg-surface p-4">
          <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Issues</h2>
          <ul className="mt-2 space-y-1 font-mono text-xs text-warn">
            {(report?.issues || []).map((issue) => <li key={issue}>{issue}</li>)}
            {Object.entries(report?.errors || {}).map(([symbol, err]) => <li key={symbol}>{symbol}: {err}</li>)}
          </ul>
        </section>
      ) : null}
    </DeskShell>
  );
}

const IND_COLORS = ["#3dcf8e", "#62e6ff", "#e0b15a", "#ef6f63", "#9b8cff", "#79b8ff", "#f27bbd", "#d9f0e6", "#c9a2ff", "#7f9d90"];

function Hero({ k, v, s }: { k: string; v: string; s: string }) {
  return (
    <article className="rounded-radius border border-border bg-surface p-4">
      <div className="text-[11px] tracking-wide text-muted uppercase">{k}</div>
      <div className="mt-1 text-2xl font-semibold tracking-tight">{v}</div>
      <div className="mt-1 text-xs text-muted [overflow-wrap:anywhere]">{s}</div>
    </article>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="min-w-0 rounded-radius border border-border bg-surface p-4">
      <h2 className="mb-3 text-sm font-medium tracking-wide text-muted uppercase">{title}</h2>
      {children}
    </section>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="flex h-40 items-center justify-center text-sm text-muted">{text}</p>;
}

function FocusN({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  return (
    <div className="flex flex-wrap gap-1 rounded-radius border border-border bg-surface p-1" role="tablist" aria-label="Last-N focus">
      {LAST_N.map((n) => (
        <button
          key={n}
          type="button"
          role="tab"
          aria-selected={value === n}
          className={`min-h-11 rounded-lg px-3 text-sm ${value === n ? "bg-bg2 text-fg" : "text-muted"}`}
          onClick={() => onChange(n)}
        >
          Last {n}
        </button>
      ))}
    </div>
  );
}

function WindowTable({ label, groups, lastN }: { label: string; groups: Record<string, GroupBlob | undefined>; lastN: number[] }) {
  const names = Object.keys(groups).filter((name) => groups[name]);
  if (!names.length) return <Empty text={`No ${label.toLowerCase()} tapes yet.`} />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-left text-xs">
        <thead>
          <tr>
            <th className="p-2 font-normal text-muted">{label}</th>
            {lastN.map((n) => <th key={n} className="p-2 font-normal text-muted">Last {n}</th>)}
          </tr>
        </thead>
        <tbody>
          {names.map((name) => (
            <tr key={name} className="border-t border-border">
              <th className="p-2 font-medium">{name}</th>
              {lastN.map((n) => {
                const c = cell(groups[name], n);
                return (
                  <td key={n} className={`p-2 font-mono tabular-nums ${pfClass(c.pf, c.validated)}`}>
                    {c.pf == null ? "—" : Number(c.pf).toFixed(2)}
                    <span className="block text-[10px] text-muted">{c.n || 0}/{n}{c.validated ? " · valid" : c.available ? " · partial" : ""}</span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Heatmaps({ matrix, n }: { matrix: MatrixCell[]; n: number }) {
  const lookup = new Map(matrix.map((row) => [`${row.indication}|${row.strategy}`, row]));
  return (
    <section className="min-w-0 rounded-radius border border-border bg-surface p-4" data-testid="sim70-heatmap">
      <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Indication × strategy · last {n}</h2>
      <p className="mt-1 mb-3 text-sm text-muted">Every possibility in the catalog. Colour follows cost-net PF of that cell’s own last-N tape.</p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-center text-xs">
          <thead>
            <tr>
              <th className="p-2 text-left font-normal text-muted">Indication</th>
              {STRATEGY_GROUPS.map((strategy) => <th key={strategy} className="p-2 font-medium" style={{ color: STRATEGY_COLORS[strategy] }}>{strategy}</th>)}
            </tr>
          </thead>
          <tbody>
            {INDICATION_GROUPS.map((kind) => (
              <tr key={kind} className="border-t border-border">
                <th className="p-2 text-left font-normal text-muted">{kind}</th>
                {STRATEGY_GROUPS.map((strategy) => {
                  const row = lookup.get(`${kind}|${strategy}`);
                  const c = cell(row, n);
                  const pf = c.pf;
                  const fills = c.evalN || c.n || row?.n || 0;
                  const alpha = pf == null ? 0 : Math.max(0.06, Math.min(0.45, (Number(pf) - 0.7) / 1.4));
                  return (
                    <td key={strategy} className="p-1">
                      <div
                        className={`min-h-11 rounded-md border border-border px-2 py-2 font-mono tabular-nums ${pfClass(pf, c.validated)}`}
                        style={{ backgroundColor: pf == null ? undefined : `color-mix(in srgb, var(--color-primary) ${Math.round(alpha * 100)}%, transparent)` }}
                      >
                        {pf == null ? "—" : Number(pf).toFixed(2)}
                        <span className="block text-[10px] text-muted">{fills}</span>
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function PricePaths({ paths }: { paths: Record<string, PricePt[]> }) {
  const symbols = Object.keys(paths);
  if (!symbols.length) return null;
  return (
    <div className="mt-4 grid gap-3">
      {symbols.map((symbol) => {
        const pts = paths[symbol] || [];
        const closes = pts.map((p) => Number(p.c || 0)).filter((v) => v > 0);
        if (closes.length < 2) return null;
        const base = closes[0];
        const data = closes.map((c, i) => ({ i, v: c / base }));
        return (
          <div key={symbol}>
            <h3 className="mb-1 text-xs text-muted">{symbol} · normalized 70h path</h3>
            <ClientChart height={120}>
              <LineChart data={data} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid stroke="var(--color-border)" />
                <XAxis dataKey="i" hide />
                <YAxis domain={["auto", "auto"]} width={36} tick={{ fill: "var(--color-muted)", fontSize: 10 }} />
                <Line type="monotone" dataKey="v" stroke="var(--color-primary)" dot={false} strokeWidth={1.5} isAnimationActive={false} />
              </LineChart>
            </ClientChart>
          </div>
        );
      })}
    </div>
  );
}

function WithWithout({ blob, lastN }: { blob?: Record<string, { with?: GroupBlob; without?: GroupBlob }>; lastN: number[] }) {
  if (!blob) return null;
  const groups: Record<string, GroupBlob> = {};
  for (const [name, pair] of Object.entries(blob)) {
    if (pair?.with) groups[`${name} with`] = pair.with;
    if (pair?.without) groups[`${name} without`] = pair.without;
  }
  return (
    <section className="min-w-0 rounded-radius border border-border bg-surface p-4">
      <h2 className="mb-3 text-sm font-medium tracking-wide text-muted uppercase">With / without Block and DCA</h2>
      <WindowTable label="Variant" groups={groups} lastN={lastN} />
    </section>
  );
}

function Successful({ n, rows, validatedByN }: { n: number; rows: SuccessRow[]; validatedByN: Record<string, number> }) {
  return (
    <section className="min-w-0 rounded-radius border border-border bg-surface p-4">
      <h2 className="text-sm font-medium tracking-wide text-muted uppercase">Validated positive configs · last {n}</h2>
      <p className="mt-1 mb-3 text-sm text-muted">
        Counts across windows: {LAST_N.map((w) => `last ${w}=${validatedByN[`last${w}`] ?? 0}`).join(" · ")}
      </p>
      {rows.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-xs">
            <thead>
              <tr>
                <th className="p-2 font-normal text-muted">Set</th>
                <th className="p-2 font-normal text-muted">Indication</th>
                <th className="p-2 font-normal text-muted">Strategy</th>
                <th className="p-2 font-normal text-muted">PF</th>
                <th className="p-2 font-normal text-muted">N</th>
                <th className="p-2 font-normal text-muted">WR</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 24).map((row) => (
                <tr key={`${row.setId}-${row.indication}-${row.strategy}`} className="border-t border-border">
                  <td className="p-2 font-mono">{row.setId}</td>
                  <td className="p-2">{row.indication}</td>
                  <td className="p-2">{row.strategy}</td>
                  <td className={`p-2 font-mono ${pfClass(row.pf, true)}`}>{row.pf == null ? "—" : Number(row.pf).toFixed(3)}</td>
                  <td className="p-2 font-mono">{row.n}</td>
                  <td className="p-2 font-mono">{row.wr == null ? "—" : `${Number(row.wr).toFixed(1)}%`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty text={`No validated positive configs at last ${n} yet.`} />
      )}
    </section>
  );
}
