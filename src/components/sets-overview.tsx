import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ReferenceLine, Tooltip, XAxis, YAxis } from "recharts";
import { ClientChart } from "@/components/visual-stats";
import { formatDuration, POSITION_COST_PCT_DEFAULT } from "@/lib/analytics";
import type { LiveStats } from "@/lib/live-stats";
import {
  DEFAULT_RANGE_ID,
  DEFAULT_TYPE,
  STEP_RANGES,
  STRATEGY_TYPES,
  filterSets,
  formatPf,
  pfByStep,
  rangeById,
  rankRows,
  stepHistogram,
  stepOf,
  summarize,
  summarizeByPack,
  typeById,
  type SetIndexRow,
  type StrategyType,
} from "@/lib/sets-view";

const PACK_LABEL: Record<string, string> = { indications: "Indications", general: "General", shortrange: "Short Range" };
const STORE_KEY = "pulse.setsView";
const TABLE_ROWS = 60;

function num(v: unknown, d = 2): string {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) : "—";
}

/** The selection survives a reload; storage can be missing or blocked, so every access is guarded. */
function loadSelection(): { type: StrategyType; range: string } {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    if (raw) {
      const j = JSON.parse(raw) as { type?: string; range?: string };
      return { type: typeById(j.type), range: rangeById(j.range).id };
    }
  } catch {
    /* storage unavailable: the defaults apply */
  }
  return { type: DEFAULT_TYPE, range: DEFAULT_RANGE_ID };
}

function Seg<T extends string>({
  label,
  value,
  options,
  onChange,
  testid,
  disabled,
  note,
}: {
  label: string;
  value: T;
  options: Array<{ id: T; label: string; hint?: string }>;
  onChange: (v: T) => void;
  testid: string;
  disabled?: boolean;
  note?: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid={testid}>
      <span className="font-mono text-[11px] tracking-wide text-muted uppercase">{label}</span>
      <div role="group" aria-label={label} className={`inline-flex overflow-hidden rounded-lg border border-border ${disabled ? "opacity-50" : ""}`}>
        {options.map((o) => (
          <button
            key={o.id}
            type="button"
            title={o.hint}
            disabled={disabled}
            aria-pressed={value === o.id}
            data-testid={`${testid}-${o.id}`}
            onClick={() => onChange(o.id)}
            className={`px-3 py-1.5 font-mono text-xs transition-colors ${
              value === o.id ? "bg-primary text-bg" : "bg-surface text-muted hover:text-text"
            }`}
          >
            {o.label}
          </button>
        ))}
      </div>
      {note ? <span className="text-xs text-muted">{note}</span> : null}
    </div>
  );
}

function Kpi({ k, v, s, good, bad }: { k: string; v: string; s?: string; good?: boolean; bad?: boolean }) {
  return (
    <div className="rounded-lg border border-border p-3">
      <div className="font-mono text-[11px] tracking-wide text-muted uppercase">{k}</div>
      <div className={`mt-1 font-mono text-xl ${good ? "text-primary" : bad ? "text-danger" : ""}`}>{v}</div>
      {s ? <div className="mt-0.5 text-xs text-muted">{s}</div> : null}
    </div>
  );
}

function Panel({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-border bg-surface p-4">
      <h3 className="font-mono text-[11px] tracking-wide text-muted uppercase">{title}</h3>
      {hint ? <p className="mt-1 text-xs text-muted">{hint}</p> : null}
      <div className="mt-3">{children}</div>
    </section>
  );
}

function setLabel(r: SetIndexRow): string {
  const pack = PACK_LABEL[r.pack] ?? r.pack;
  if (r.kind === "trail") return `${pack} trail ${r.tr}`;
  return `${pack} sl${num(r.sl)} st${r.st}${r.kind === "exit" ? " exit" : ""}`;
}

function SetsBody({ stats, type, rangeId }: { stats: LiveStats; type: "normal" | "exit" | "trailing"; rangeId: string }) {
  const cost = stats.pfCost?.costPct ?? POSITION_COST_PCT_DEFAULT;
  const minPf = stats.sets?.minPf ?? 1.1;
  const index = stats.sets?.index;
  const rows = useMemo(() => filterSets(index, type, rangeId, cost), [index, type, rangeId, cost]);
  const sum = useMemo(() => summarize(rows), [rows]);
  const packs = useMemo(() => Object.entries(summarizeByPack(rows)), [rows]);
  const hist = useMemo(() => stepHistogram(rows, cost), [rows, cost]);
  const pfStep = useMemo(() => pfByStep(rows, cost), [rows, cost]);
  const ranked = useMemo(() => rankRows(rows), [rows]);
  if (!index || index.length === 0) {
    return <p className="py-6 text-center text-sm text-muted">The Set list appears after the first historic replay pass.</p>;
  }
  const shown = ranked.slice(0, TABLE_ROWS);
  return (
    <div className="space-y-3" data-testid="sets-overview-body">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        <Kpi k="Sets shown" v={String(sum.total)} s={`${type === "normal" ? "base" : type === "exit" ? "exit (no TP)" : "trailing"} · step ${rangeById(rangeId).label}`} />
        <Kpi k="Validated" v={`${num(sum.validatedPct, 1)}%`} s={`${sum.valid} of ${sum.total} pass Base`} good={sum.validatedPct >= 25} />
        <Kpi
          k="Effective PF"
          v={formatPf(sum.effectivePf)}
          s={`after Base · min ${minPf}`}
          good={(sum.effectivePf ?? 0) >= minPf}
          bad={sum.effectivePf !== null && sum.effectivePf < 1}
        />
        <Kpi k="Orders" v={String(sum.orders)} s="valid Sets, gate windows" />
        <Kpi k="Before Base" v={formatPf(sum.unfilteredPf)} s="under 1 is normal" />
      </div>
      {packs.length > 1 ? (
        <div className="flex flex-wrap gap-3 font-mono text-xs text-muted" data-testid="sets-overview-packs">
          {packs.map(([p, s]) => (
            <span key={p}>
              {PACK_LABEL[p] ?? p}: {s.valid}/{s.total} ({num(s.validatedPct, 1)}%) · PF {formatPf(s.effectivePf)}
            </span>
          ))}
        </div>
      ) : null}
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Sets per step" hint="All shown Sets and those that pass Base, by TP step (trailing: arm in PositionCost steps).">
          <ClientChart height={180}>
            <BarChart data={hist} margin={{ top: 6, right: 8, left: -12, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border, #2a2f3a)" />
              <XAxis dataKey="step" tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
              <Tooltip />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar dataKey="total" name="Sets" fill="#6b7385" radius={[3, 3, 0, 0]} />
              <Bar dataKey="valid" name="Validated" fill="#2fbf71" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ClientChart>
        </Panel>
        <Panel title="Effective PF per step" hint="Valid Sets of each step pooled over their gate windows. A step with no valid Set has no bar.">
          <ClientChart height={180}>
            <BarChart data={pfStep} margin={{ top: 6, right: 8, left: -12, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border, #2a2f3a)" />
              <XAxis dataKey="step" tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 11 }} domain={[0, "auto"]} />
              <Tooltip />
              <ReferenceLine y={1} stroke="#8a93a6" strokeDasharray="4 2" />
              <ReferenceLine y={minPf} stroke="#e0a030" strokeDasharray="4 2" />
              <Bar dataKey="pf" name="Effective PF" fill="#4c8dff" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ClientChart>
        </Panel>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[820px] text-left text-sm" data-testid="sets-overview-table">
          <thead className="font-mono text-[11px] text-muted">
            <tr>
              <th className="pb-2 font-medium">Set</th>
              <th className="pb-2 font-medium">Base</th>
              <th className="pb-2 text-right font-medium">Step</th>
              <th className="pb-2 text-right font-medium">n</th>
              <th className="pb-2 text-right font-medium">Gate PF</th>
              <th className="pb-2 text-right font-medium">Last 15 PF</th>
              <th className="pb-2 text-right font-medium">Last 25 R</th>
              <th className="pb-2 text-right font-medium">WR</th>
              <th className="pb-2 text-right font-medium">E</th>
              <th className="pb-2 text-right font-medium">Hold</th>
              <th className="pb-2 text-right font-medium">Max DDt</th>
              <th className="pb-2 text-right font-medium">Avg DDt</th>
              <th className="pb-2 text-right font-medium" title="Main Set: orders opened while Base passed · the Main gate's PF on its last N">Main Set n · PF</th>
              <th className="pb-2 text-right font-medium" title="Real Set: orders opened while Base and Main passed · PF on its last N">Real Set n · PF</th>
            </tr>
          </thead>
          <tbody>
            {shown.length === 0 ? (
              <tr>
                <td colSpan={14} className="py-8 text-center text-muted">
                  No Set in this step range.
                </td>
              </tr>
            ) : (
              shown.map((r) => (
                <tr key={r.id} className="border-t border-border font-mono text-xs">
                  <td className="py-1.5">{setLabel(r)}</td>
                  <td className={r.on === 1 ? "py-1.5 text-primary" : "py-1.5 text-danger"}>{r.on === 1 ? "valid" : "off"}</td>
                  <td className="py-1.5 text-right">{num(stepOf(r, cost), 0)}</td>
                  <td className="py-1.5 text-right">{r.n ?? "—"}</td>
                  <td className={`py-1.5 text-right ${r.pf >= minPf ? "text-primary" : "text-danger"}`}>{formatPf(r.pf)}</td>
                  <td className="py-1.5 text-right">{formatPf(r.p15)}</td>
                  <td className={`py-1.5 text-right ${Number(r.r25 ?? 0) < 0 ? "text-danger" : "text-primary"}`}>{num(r.r25)}</td>
                  <td className="py-1.5 text-right">{r.wr === undefined ? "—" : `${num(r.wr, 0)}%`}</td>
                  <td className={`py-1.5 text-right ${Number(r.e ?? 0) < 0 ? "text-danger" : "text-primary"}`}>{num(r.e, 4)}</td>
                  <td className="py-1.5 text-right">{r.h === undefined ? "—" : formatDuration(r.h * 1000)}</td>
                  <td className="py-1.5 text-right">{formatDuration(r.dd * 1000)}</td>
                  <td className="py-1.5 text-right">{r.da === undefined ? "—" : formatDuration(r.da * 1000)}</td>
                  <td className="py-1.5 text-right">{r.mainN === undefined ? "—" : `${r.mainN} · ${formatPf(r.mainPf ?? 0)}`}</td>
                  <td className="py-1.5 text-right">{r.realN === undefined ? "—" : `${r.realN} · ${formatPf(r.realPf ?? 0)}`}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
        {ranked.length > TABLE_ROWS ? (
          <p className="mt-2 text-xs text-muted">
            Showing the first {TABLE_ROWS} of {ranked.length} Sets: valid Sets first, stable ones with the most orders on top.
          </p>
        ) : null}
      </div>
    </div>
  );
}

function AxisBody({ stats }: { stats: LiveStats }) {
  const coord = stats.coord;
  const axes = Object.entries(coord?.axes ?? {});
  const stages = Object.entries(coord?.stages ?? {}).map(([name, v]) => ({ name, pf: Number(v.pf ?? 0), n: Number(v.n ?? 0), open: Boolean(v.open) }));
  const minPf = coord?.minPf ?? 1.1;
  const gate = coord?.gate;
  return (
    <div className="space-y-3" data-testid="sets-overview-axis">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi k="Gate" v={gate?.allow === false ? "blocked" : "open"} s={(gate?.reasons ?? []).slice(0, 2).join(" · ") || "no reason"} good={gate?.allow !== false} bad={gate?.allow === false} />
        <Kpi k="Min PF" v={num(minPf)} s={`window ${coord?.pfWindow ?? "—"}`} />
        <Kpi k="PositionCost" v={`${num(coord?.positionCostPct, 2)}%`} />
        <Kpi k="Rearrange" v={coord?.rearrange ? "on" : "off"} s="Exit-lane positions only" />
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Axes" hint="Each axis is a window over the closed positions; the gate asks every enabled axis.">
          <table className="w-full text-left text-xs">
            <thead className="font-mono text-muted">
              <tr>
                <th className="pb-1 font-medium">Axis</th>
                <th className="pb-1 font-medium">On</th>
                <th className="pb-1 text-right font-medium">Max window</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {axes.length === 0 ? (
                <tr>
                  <td colSpan={3} className="py-4 text-center text-muted">
                    No axis data yet
                  </td>
                </tr>
              ) : (
                axes.map(([name, a]) => (
                  <tr key={name} className="border-t border-border">
                    <td className="py-1">{name}</td>
                    <td className={a.enabled ? "py-1 text-primary" : "py-1 text-danger"}>{a.enabled ? "on" : "off"}</td>
                    <td className="py-1 text-right">{a.max_window}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </Panel>
        <Panel title="Stage PF" hint="PF of the last closes at each stage; below the line the stage does not pass.">
          <ClientChart height={180}>
            <BarChart data={stages} margin={{ top: 6, right: 8, left: -12, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border, #2a2f3a)" />
              <XAxis dataKey="name" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} domain={[0, "auto"]} />
              <Tooltip />
              <ReferenceLine y={1} stroke="#8a93a6" strokeDasharray="4 2" />
              <ReferenceLine y={minPf} stroke="#e0a030" strokeDasharray="4 2" />
              <Bar dataKey="pf" name="PF" fill="#4c8dff" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ClientChart>
        </Panel>
      </div>
    </div>
  );
}

function BlockBody({ stats }: { stats: LiveStats }) {
  const blk = stats.block;
  const lanes = blk?.lanes ?? [];
  const counts = blk?.allCounts ?? [];
  return (
    <div className="space-y-3" data-testid="sets-overview-block">
      <p className="text-sm text-muted">
        {blk?.enabled ? "on" : "off"} · max stack {blk?.maxStack || "unlimited"} · volume ratio {blk?.volumeRatio ?? "—"} · PF ratio{" "}
        {blk?.profitFactorRatio ?? "—"} · min PF {blk?.defaultMinPF ?? "—"}. Block stacks on positions without a Set only.
      </p>
      <Panel title="Counts" hint="Group n adds volume ratio x base volume once its PF decision passes.">
        <table className="w-full text-left text-xs">
          <thead className="font-mono text-muted">
            <tr>
              <th className="pb-1 font-medium">Group</th>
              <th className="pb-1 text-right font-medium">Increment</th>
              <th className="pb-1 text-right font-medium">Target add</th>
              <th className="pb-1 text-right font-medium">Target block</th>
              <th className="pb-1 text-right font-medium">Min PF</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {counts.map((c) => (
              <tr key={c.n} className="border-t border-border">
                <td className="py-1">{c.n}</td>
                <td className="py-1 text-right">{num(c.inc)}</td>
                <td className="py-1 text-right">{num(c.targetAdd, 3)}</td>
                <td className="py-1 text-right">{num(c.targetBlock, 3)}</td>
                <td className="py-1 text-right">{num(c.minPF, 3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      {lanes.length === 0 ? (
        <p className="py-4 text-center text-sm text-muted">Block lanes appear when a parent without a Set is open.</p>
      ) : (
        lanes.map((lane) => (
          <div key={`${lane.symbol}-${lane.side}`} className="rounded-lg border border-border p-3">
            <div className="mb-2 flex flex-wrap justify-between gap-2 text-sm">
              <span className="font-medium">
                {lane.symbol.replace("-USDT", "")} {lane.side}
              </span>
              <span className="font-mono text-xs text-muted">
                base {lane.baseQty} · add {lane.confirmedAdd} · agg {lane.aggregate}
              </span>
            </div>
            <table className="w-full text-left text-xs">
              <thead className="font-mono text-muted">
                <tr>
                  <th className="pb-1 font-medium">#</th>
                  <th className="pb-1 text-right font-medium">inc</th>
                  <th className="pb-1 text-right font-medium">min PF</th>
                  <th className="pb-1 text-right font-medium">obs PF</th>
                  <th className="pb-1 font-medium">pass</th>
                  <th className="pb-1 font-medium">paused</th>
                  <th className="pb-1 font-medium">sat</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {lane.counts.map((c) => (
                  <tr key={c.n} className="border-t border-border">
                    <td className="py-1">{c.n}</td>
                    <td className="py-1 text-right">{c.inc}</td>
                    <td className="py-1 text-right">{c.minPF}</td>
                    <td className="py-1 text-right">{c.obsPF}</td>
                    <td className={c.pass ? "py-1 text-primary" : "py-1 text-danger"}>{c.pass ? "yes" : "no"}</td>
                    <td className={c.paused ? "py-1 text-danger" : "py-1"}>{c.paused ? "yes" : "no"}</td>
                    <td className="py-1">{c.satisfied ? "yes" : "no"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}
    </div>
  );
}

function DcaBody({ stats }: { stats: LiveStats }) {
  const dca = stats.dca;
  const lanes = dca?.lanes ?? [];
  return (
    <div className="space-y-3" data-testid="sets-overview-dca">
      <p className="text-sm text-muted">
        {dca?.enabled ? "on" : "off"} · {dca?.active ? "active" : dca?.deactReason || "idle"} · steps {dca?.maxSteps || "—"} · PF15{" "}
        {num(dca?.pf15)} · last 25 R {num(dca?.last25AvgR)} · distance {(dca?.distancesPct ?? []).join("/") || "—"} % · volume x{" "}
        {(dca?.mults ?? []).join("/") || "—"}
        {dca?.enabled ? "" : " · DCA is switched off until it is approved again"}
      </p>
      {lanes.length === 0 ? (
        <p className="py-4 text-center text-sm text-muted">DCA lanes attach when a parent without a Set is open.</p>
      ) : (
        lanes.map((lane) => (
          <div key={`${lane.symbol}-${lane.side}`} className="rounded-lg border border-border p-3 font-mono text-xs">
            <div className="mb-2 flex justify-between gap-2">
              <span>
                {lane.symbol.replace("-USDT", "")} {lane.side}
              </span>
              <span className="text-muted">
                parent {lane.parentQty} · avg {lane.avgEntry} · filled {lane.filledN}
              </span>
            </div>
            <div className="flex flex-wrap gap-2">
              {lane.steps.map((s) => (
                <span key={s.n} className={s.filled ? "text-primary" : "text-muted"}>
                  #{s.n} {s.distancePct}% x{s.mult}
                  {s.filled ? " filled" : ""}
                </span>
              ))}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

/**
 * The Sets overview. Two selections sit at the top: the step range (first and default: All) and the strategy type
 * (Normal, Exit, Trailing, Axis, Block, DCA). The range filters Sets, so it applies to Normal, Exit and Trailing; Axis, Block and
 * DCA show their own panels.
 */
export function SetsOverview({ stats, title = "Sets overview" }: { stats: LiveStats | null; title?: string }) {
  const [type, setType] = useState<StrategyType>(DEFAULT_TYPE);
  const [rangeId, setRangeId] = useState<string>(DEFAULT_RANGE_ID);
  useEffect(() => {
    const s = loadSelection();
    setType(s.type);
    setRangeId(s.range);
  }, []);
  const save = (t: StrategyType, r: string) => {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify({ type: t, range: r }));
    } catch {
      /* storage unavailable: the selection lasts for this view only */
    }
  };
  const isSets = type === "normal" || type === "exit" || type === "trailing";
  return (
    <section className="rounded-xl border border-border bg-surface p-4" data-testid="sets-overview">
      <h2 className="font-mono text-[11px] tracking-wide text-muted uppercase">{title}</h2>
      <div className="mt-3 flex flex-col gap-2">
        <Seg
          label="Step range"
          testid="sets-range"
          value={rangeId}
          options={STEP_RANGES.map((r) => ({ id: r.id, label: r.label, hint: r.id === "2-6" ? "Short Range band" : undefined }))}
          disabled={!isSets}
          note={isSets ? "TP steps of one PositionCost each" : "the range filters Sets (Normal, Exit, Trailing)"}
          onChange={(v) => {
            setRangeId(v);
            save(type, v);
          }}
        />
        <Seg
          label="Type"
          testid="sets-type"
          value={type}
          options={STRATEGY_TYPES}
          onChange={(v) => {
            setType(v);
            save(v, rangeId);
          }}
        />
      </div>
      <div className="mt-4">
        {!stats ? (
          <p className="py-6 text-center text-sm text-muted">Waiting for the engine</p>
        ) : type === "axis" ? (
          <AxisBody stats={stats} />
        ) : type === "block" ? (
          <BlockBody stats={stats} />
        ) : type === "dca" ? (
          <DcaBody stats={stats} />
        ) : (
          <SetsBody stats={stats} type={type} rangeId={rangeId} />
        )}
      </div>
    </section>
  );
}
