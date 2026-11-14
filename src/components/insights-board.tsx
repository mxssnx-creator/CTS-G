import { useMemo, useState, type ReactNode } from "react";
import { CartesianGrid, Legend, Line, LineChart, Tooltip, XAxis, YAxis } from "recharts";
import { ClientChart } from "@/components/visual-stats";
import type { LiveStats } from "@/lib/live-stats";
import {
  fmtPf,
  groupCells,
  historySeries,
  packRows,
  pfTone,
  signalGrid,
  splitClosed,
  stageFunnel,
  type PackKind,
} from "@/lib/insights";

type Section = "indications" | "strategies" | "stages" | "live" | "signals";
const SECTIONS: Array<{ id: Section; label: string }> = [
  { id: "indications", label: "Indications" },
  { id: "strategies", label: "Strategies" },
  { id: "stages", label: "Stages" },
  { id: "live", label: "Live" },
  { id: "signals", label: "Signals" },
];

const TONE: Record<ReturnType<typeof pfTone>, string> = {
  bad: "text-danger",
  flat: "text-fg",
  good: "text-primary",
  none: "text-muted",
};

function Panel({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="rounded-radius border border-border bg-surface p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium tracking-wide text-muted uppercase">{title}</h3>
        {hint && <p className="text-xs text-muted">{hint}</p>}
      </div>
      {children}
    </div>
  );
}

function Stat({ k, v, s, tone }: { k: string; v: string; s?: string; tone?: string }) {
  return (
    <div className="rounded-radius border border-border bg-bg2/40 p-3">
      <p className="font-mono text-[11px] tracking-wide text-muted uppercase">{k}</p>
      <p className={`mt-0.5 font-mono text-xl tabular-nums ${tone ?? "text-fg"}`}>{v}</p>
      {s && <p className="mt-0.5 text-xs text-muted">{s}</p>}
    </div>
  );
}

function Pf({ v }: { v: number | null | undefined }) {
  return <span className={`font-mono tabular-nums ${TONE[pfTone(v)]}`}>{fmtPf(v)}</span>;
}

function Bar({ pct, tone = "bg-primary" }: { pct: number; tone?: string }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-bg2">
      <div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
    </div>
  );
}

function Table({ head, rows, empty }: { head: string[]; rows: ReactNode[][]; empty: string }) {
  if (!rows.length) return <p className="text-sm text-muted">{empty}</p>;
  return (
    <div className="-mx-1 overflow-x-auto">
      <table className="w-full min-w-[420px] text-sm">
        <thead>
          <tr className="text-left font-mono text-[11px] tracking-wide text-muted uppercase">
            {head.map((h) => (
              <th key={h} className="px-1 py-1.5 font-normal">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-border/60">
              {r.map((c, j) => (
                <td key={j} className="px-1 py-1.5 align-middle">{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const KINDS: Array<PackKind | "all"> = ["all", "indications", "general", "shortrange", "signal pack", "extra", "signals", "other"];

function IndicationsSection({ stats }: { stats: LiveStats | null }) {
  const [kind, setKind] = useState<PackKind | "all">("all");
  const rows = useMemo(() => packRows(stats?.sets?.pfBase, stats?.execution), [stats]);
  const series = useMemo(() => historySeries(stats?.sets?.pfBase), [stats]);
  const shown = rows.filter((r) => kind === "all" || r.kind === kind);
  const fmtT = (t: number) => new Date(t * 1000).toISOString().slice(11, 16);
  return (
    <div className="space-y-3">
      <Panel title="Effective PF and validated share over passes" hint="PF of the Sets valid after every gate (selection PF, in-sample) and per stage population">
        {series.length ? (
          <ClientChart height={240}>
            <LineChart data={series} margin={{ left: -12, right: 8, top: 6 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
              <XAxis dataKey="t" tickFormatter={fmtT} tick={{ fontSize: 11 }} />
              <YAxis yAxisId="pf" tick={{ fontSize: 11 }} domain={[0, "auto"]} />
              <YAxis yAxisId="pct" orientation="right" tick={{ fontSize: 11 }} unit="%" />
              <Tooltip labelFormatter={(t) => fmtT(Number(t))} contentStyle={{ fontSize: 12 }} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Line yAxisId="pf" type="monotone" dataKey="pf" name="valid PF" stroke="var(--color-primary)" dot={false} strokeWidth={2} />
              <Line yAxisId="pf" type="monotone" dataKey="base" name="Base" stroke="#8b9bb4" dot={false} />
              <Line yAxisId="pf" type="monotone" dataKey="real" name="Real" stroke="#c48ee8" dot={false} />
              <Line yAxisId="pct" type="monotone" dataKey="validatedPct" name="validated %" stroke="#e8b35a" dot={false} strokeDasharray="4 3" />
            </LineChart>
          </ClientChart>
        ) : (
          <p className="text-sm text-muted">No scoring pass recorded yet.</p>
        )}
      </Panel>
      <Panel title="Per indication pack" hint="Sets, validated share, PF after Base (selection) and before it (unfiltered), open positions">
        <div className="mb-2 flex flex-wrap gap-1.5">
          {KINDS.map((k) => (
            <button key={k} type="button" onClick={() => setKind(k)}
              className={`rounded-full border px-2.5 py-0.5 text-xs ${kind === k ? "border-primary text-primary" : "border-border text-muted"}`}>
              {k}
            </button>
          ))}
        </div>
        <Table
          head={["Pack", "Sets", "Validated", "", "PF after Base", "Orders", "Unfiltered", "Open"]}
          empty="No pack figures yet (the replay is still building)."
          rows={shown.slice(0, 80).map((r) => [
            <span className="font-mono text-xs">{r.pack}</span>,
            r.total,
            `${r.valid} · ${r.validatedPct.toFixed(1)}%`,
            <div className="w-20"><Bar pct={r.validatedPct} /></div>,
            <Pf v={r.basePf} />,
            r.baseOrders,
            <Pf v={r.unfilteredPf} />,
            r.open || "",
          ])}
        />
      </Panel>
    </div>
  );
}

function StrategiesSection({ stats }: { stats: LiveStats | null }) {
  const cells = useMemo(() => groupCells(stats?.execution), [stats]);
  const byPack = useMemo(() => splitClosed(stats?.closed, (r) => r.pack ?? "-"), [stats]);
  const byReason = useMemo(() => splitClosed(stats?.closed, (r) => String(r.reason || "-").split(/[- ]/)[0]), [stats]);
  const g = stats?.execution?.groups;
  return (
    <div className="space-y-3">
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Executed by pack" hint="Settled closes of this view (unscored downtime closes left out); PF of the net returns">
          <Table head={["Pack", "Orders", "Wins", "PF", "Net %"]} empty="No settled close yet."
            rows={byPack.slice(0, 40).map((r) => [<span className="font-mono text-xs">{r.key}</span>, r.orders, r.wins, <Pf v={r.pf} />, (100 * r.netPct).toFixed(2)])} />
        </Panel>
        <Panel title="Executed by exit" hint="Stops against targets, trailing, time">
          <Table head={["Exit", "Orders", "Wins", "PF", "Net %"]} empty="No settled close yet."
            rows={byReason.map((r) => [r.key, r.orders, r.wins, <Pf v={r.pf} />, (100 * r.netPct).toFixed(2)])} />
        </Panel>
      </div>
      <Panel title="Strategy groups (pack x type x range x lane x side)"
        hint={g ? `group gate ${g.enabled ? "on" : "off"} · newest ${g.n} results of ${g.hours} h at PF ${g.pf}` : "group gate figures not sent"}>
        <Table head={["Pack", "Type", "Range", "Lane", "Side", "Orders", "PF", "Gate"]} empty="No group has executed results in the gate window."
          rows={cells.slice(0, 120).map((c) => [
            <span className="font-mono text-xs">{c.pack}</span>, c.kind, c.range, c.lane, c.side || "both", c.orders, <Pf v={c.pf} />,
            <span className={c.allow ? "text-primary" : "text-danger"}>{c.allow ? "open" : "closed"}</span>,
          ])} />
      </Panel>
    </div>
  );
}

function StagesSection({ stats }: { stats: LiveStats | null }) {
  const pb = stats?.sets?.pfBase;
  const funnel = useMemo(() => stageFunnel(pb), [pb]);
  const floors = stats?.sets?.setFloors;
  const stepRows = (() => {
    const by = new Map<number, { tp: number; manual: number; L?: number; S?: number; orders: number; src: string[] }>();
    for (const s of stats?.sets?.stepPf?.steps ?? []) {
      const r = by.get(s.step) ?? { tp: s.tpPct, manual: s.manual, orders: 0, src: [] };
      if (s.side === -1) r.S = s.used; else r.L = s.used;
      r.orders += s.orders;
      if (s.source && !r.src.includes(s.source)) r.src.push(s.source);
      by.set(s.step, r);
    }
    return [...by.values()].filter((r) => r.orders > 0).sort((a, b) => a.tp - b.tp);
  })();
  const ex = stats?.execution;
  const executedAll = splitClosed(stats?.closed, () => "all")[0];
  const executedPf = executedAll ? executedAll.pf : null;
  const maxSets = Math.max(1, ...funnel.map((f) => f.sets));
  const windows = Object.entries(pb?.windows ?? {});
  return (
    <div className="space-y-3">
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat k="Configured Sets" v={String(ex?.sets?.configured || pb?.total || 0)} s={`${ex?.sets?.valid || pb?.valid || 0} valid through Real`} />
        <Stat k="Selection PF (valid)" v={fmtPf(pb?.base?.pf)} s={`${pb?.base?.n ?? 0} window orders · in-sample`} tone={TONE[pfTone(pb?.base?.pf)]} />
        <Stat k="Executed PF" v={fmtPf(executedPf)} s={`${executedAll?.orders ?? 0} settled closes in this view`} tone={TONE[pfTone(executedPf)]} />
        <Stat k="Auto floor per Set" v={floors ? `${floors.learned ?? 0}` : "–"} s={floors ? `${floors.scope} scope · ${floors.defaults ?? 0} on the defaults` : "not sent"} />
      </section>
      <Panel title="Stage funnel" hint="Sets passing each stage and the PF of that stage's own population (read next to the executed PF)">
        <div className="space-y-2">
          {funnel.map((f) => (
            <div key={f.stage} className="grid grid-cols-[4.5rem_1fr_auto_3.5rem] items-center gap-2 text-sm">
              <span className="font-mono text-xs uppercase text-muted">{f.stage}</span>
              <Bar pct={(100 * f.sets) / maxSets} tone={f.stage === "realNeg" ? "bg-danger/70" : "bg-primary"} />
              <span className="whitespace-nowrap text-right font-mono text-xs tabular-nums">{f.sets} · {f.pct.toFixed(1)}%</span>
              <span className="text-right"><Pf v={f.pf} /></span>
            </div>
          ))}
          {!funnel.length && <p className="text-sm text-muted">No stage figures yet.</p>}
        </div>
      </Panel>
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Last-N windows" hint="Each window judged on its own: Sets passing it and the PF of the orders it reads">
          <Table head={["Window", "N", "Sets", "Share", "PF", "Orders"]} empty="No window figures yet."
            rows={windows.map(([k, w]) => [k, Array.isArray(w.n) ? w.n.join("/") : String(w.n ?? "–"), w.sets, `${w.pct.toFixed(1)}%`, <Pf v={w.pf} />, w.orders])} />
        </Panel>
        <Panel title="Minimum PF per TP step" hint="Manual floor and the floor used per direction (Auto where learned); a Set's own floor wins with scope 'set'">
          <div className="max-h-80 overflow-y-auto">
            <Table head={["TP %", "Manual", "Used L", "Used S", "Source", "Orders"]} empty="No step floor learned yet."
              rows={stepRows.map((r) => [r.tp.toFixed(1), r.manual.toFixed(2), r.L == null ? "–" : r.L.toFixed(2), r.S == null ? "–" : r.S.toFixed(2), r.src.join("/"), r.orders])} />
          </div>
        </Panel>
      </div>
    </div>
  );
}

function LiveSection({ stats }: { stats: LiveStats | null }) {
  const ex = stats?.execution;
  const sd = ex?.simDiff;
  const lanes = ex?.lanes;
  return (
    <div className="space-y-3">
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat k="Open positions" v={String(ex?.open?.positions ?? stats?.openCount ?? 0)} s={`${ex?.open?.sets ?? 0} Sets · ${ex?.open?.underInvalidSet ?? 0} under a Set no longer valid`} />
        <Stat k="Settled closes" v={String(ex?.closed?.orders ?? 0)} s={`${ex?.closed?.wins ?? 0} wins · ${ex?.closed?.losses ?? 0} losses`} />
        <Stat k="Not scored" v={String((ex?.closed?.skippedNotScored ?? 0) + (ex?.closed?.downtimeCloses ?? 0))} s={`${ex?.closed?.downtimeCloses ?? 0} closed while the engine was down`} />
        <Stat k="Entries skipped" v={String((ex?.entriesSkipped?.thinBook ?? 0) + (ex?.entriesSkipped?.scopeBlocked ?? 0))} s={`${ex?.entriesSkipped?.thinBook ?? 0} thin book · ${ex?.entriesSkipped?.scopeBlocked ?? 0} scope`} />
      </section>
      {lanes && (
        <Panel title="Lanes" hint="Positive lane (Real) and negative lane (Real Negative), each its own window">
          <Table head={["Lane", "Open", "Closed", "Wins", "Losses", "Win %"]} empty=""
            rows={[
              ["positive", lanes.openPos, lanes.closedPos, lanes.winsPos, lanes.lossesPos, lanes.closedPos ? `${((100 * lanes.winsPos) / lanes.closedPos).toFixed(0)}%` : "–"],
              ["negative", lanes.openNeg, lanes.closedNeg, lanes.winsNeg, lanes.lossesNeg, lanes.closedNeg ? `${((100 * lanes.winsNeg) / lanes.closedNeg).toFixed(0)}%` : "–"],
            ]} />
        </Panel>
      )}
      <Panel title="Simulated against live" hint="The same settled closes: planned level and configured cost against the fill and the real fees">
        {sd ? (
          <div className="space-y-3">
            <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Stat k="Live PF" v={fmtPf(sd.total.livePf)} s={`${sd.closes} closes`} tone={TONE[pfTone(sd.total.livePf)]} />
              <Stat k="Simulated PF" v={fmtPf(sd.total.simPf)} tone={TONE[pfTone(sd.total.simPf)]} />
              <Stat k="Net gap" v={`${sd.total.netGapPct.toFixed(2)}%`} s={`fill ${sd.total.fillGapPct.toFixed(2)}% · cost ${sd.total.costGapPct.toFixed(2)}%`} tone={sd.total.netGapPct < 0 ? "text-danger" : "text-primary"} />
              <Stat k="Stop fill factor" v={sd.stopFactor.median == null ? "–" : `${sd.stopFactor.median.toFixed(2)}x`} s={`mean ${sd.stopFactor.mean?.toFixed(2) ?? "–"} · p90 ${sd.stopFactor.p90?.toFixed(2) ?? "–"} · n ${sd.stopFactor.n}`} />
            </section>
            <Table head={["Exit", "Closes", "Live PF", "Sim PF", "Fill gap %", "Cost gap %"]} empty=""
              rows={Object.entries(sd.byExit).map(([k, r]) => [k, r.n, <Pf v={r.livePf} />, <Pf v={r.simPf} />, r.fillGapPct.toFixed(2), r.costGapPct.toFixed(2)])} />
          </div>
        ) : (
          <p className="text-sm text-muted">No settled Set close yet.</p>
        )}
      </Panel>
    </div>
  );
}

function SignalsSection({ stats }: { stats: LiveStats | null }) {
  const rows = useMemo(() => packRows(stats?.sets?.pfBase, stats?.execution).filter((r) => r.kind === "signals"), [stats]);
  const grid = useMemo(() => signalGrid(rows), [rows]);
  const rep = stats?.sets?.signalConfigs;
  const executed = useMemo(() => splitClosed((stats?.closed ?? []).filter((c) => String(c.pack ?? "").startsWith("sig-")), (r) => r.pack ?? "-"), [stats]);
  return (
    <div className="space-y-3">
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat k="Signals packs" v={String(rep?.packs ?? rows.length)} s={rep?.enabled ? "on (stratSignals)" : "off"} />
        <Stat k="Sets" v={String(rows.reduce((n, r) => n + r.total, 0))} s={`${rows.reduce((n, r) => n + r.valid, 0)} valid through Real`} />
        <Stat k="Floor" v={fmtPf(stats?.sets?.minPfSignals ?? 2.2)} s="minPfSignals, every window and stage" />
        <Stat k="Executed" v={String(executed.reduce((n, r) => n + r.orders, 0))} s="settled Signals closes" />
      </section>
      <Panel title="Source x range x timeframe" hint="Each cell is its own pack: validated Sets / Sets and PF after Base">
        {grid.sources.length ? (
          <Table head={["Source", ...grid.cols]} empty=""
            rows={grid.sources.map((s) => [
              <span className="font-mono text-xs">{s}</span>,
              ...grid.cols.map((c) => {
                const r = grid.cell[`${s}|${c}`];
                return r ? <span className="font-mono text-xs">{r.valid}/{r.total} · <Pf v={r.basePf} /></span> : <span className="text-muted">–</span>;
              }),
            ])} />
        ) : (
          <p className="text-sm text-muted">No Signals pack is configured or the replay has not scored one yet.</p>
        )}
      </Panel>
      <Panel title="Signals executed" hint="Settled closes of Signals packs">
        <Table head={["Pack", "Orders", "Wins", "PF", "Net %"]} empty="No Signals close yet."
          rows={executed.map((r) => [<span className="font-mono text-xs">{r.key}</span>, r.orders, r.wins, <Pf v={r.pf} />, (100 * r.netPct).toFixed(2)])} />
      </Panel>
    </div>
  );
}

/** Detailed insight sections of the results page: indications history, strategies, stages, live and Signals. */
export function InsightsBoard({ stats }: { stats: LiveStats | null }) {
  const [section, setSection] = useState<Section>("indications");
  return (
    <section className="space-y-3" data-testid="insights-board">
      <div className="flex flex-wrap items-center gap-1 rounded-radius border border-border bg-surface p-1">
        {SECTIONS.map((s) => (
          <button key={s.id} type="button" onClick={() => setSection(s.id)}
            className={`min-h-9 flex-1 rounded-md px-3 text-sm transition ${section === s.id ? "bg-primary text-bg" : "text-muted hover:text-fg"}`}>
            {s.label}
          </button>
        ))}
      </div>
      {section === "indications" && <IndicationsSection stats={stats} />}
      {section === "strategies" && <StrategiesSection stats={stats} />}
      {section === "stages" && <StagesSection stats={stats} />}
      {section === "live" && <LiveSection stats={stats} />}
      {section === "signals" && <SignalsSection stats={stats} />}
    </section>
  );
}
