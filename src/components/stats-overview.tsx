import type { ReactNode } from "react";
import type { CostPfMetric } from "@/lib/analytics";
import { formatDuration, type buildOverview } from "@/lib/analytics";
import { liftRows, relationFigures, type LiveStats, type SimDiffRow } from "@/lib/live-stats";
import { formatPf, stageFigures, windowFigures } from "@/lib/sets-view";
import { executionScopeText, scopeStatus } from "@/lib/execution-scope";
import { evidencePfText } from "@/lib/evidence-view";

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
        {live?.compactFallback ? <p role="status" className="col-span-full text-sm text-muted">Compact connection snapshot: only explicitly attributed figures are shown. Detailed progress, settings and execution evidence require this connection's full statistics.</p> : null}
        <Hero
          k={`Last ${cost.n || 15} PF`}
          v={evidencePfText(live?.pfCost ?? cost, cost.count)}
          s={`Net gains ÷ net losses · R ${cost.avgR.toFixed(2)} · net ${cost.netSumPct >= 0 ? "+" : ""}${cost.netSumPct.toFixed(2)}% (${cost.netPct >= 0 ? "+" : ""}${cost.netPct.toFixed(3)}% per position) · n ${cost.count} · ${cost.count < 8 ? "insufficient evidence" : cost.pass ? "pass" : "block"}`}
          tone={cost.count < 8 ? "ok" : cost.pass ? "good" : "bad"}
        />
        {live?.sets?.pfBase ? (
          <Hero
            k="Base-window PF · all-stage validated"
            v={evidencePfText(live.sets.pfBase.base, live.sets.pfBase.base.n)}
            s={`${live.sets.pfBase.base.sets} PF-validated Sets${live.sets.pfBase.validatedPct != null ? ` (${live.sets.pfBase.validatedPct.toFixed(1)}%${live.sets.pfBase.validatedBasePct != null && live.sets.pfBase.main?.on ? `, Base alone ${live.sets.pfBase.validatedBasePct.toFixed(1)}%` : ""})` : ""} · n ${live.sets.pfBase.base.n}${live.sets.pfBase.main?.on ? ` · Main last ${live.sets.pfBase.main.lastN}: PF ${formatPf(live.sets.pfBase.main.n ? live.sets.pfBase.main.pf : null)} (${live.sets.pfBase.main.n} orders)` : ""}${live.sets.pfBase.baseEntry && live.sets.pfBase.baseEntry.entries ? ` · per entry PF ${formatPf(live.sets.pfBase.baseEntry.pf)} (${live.sets.pfBase.baseEntry.entries} entries, ${live.sets.pfBase.baseEntry.setsPerEntry} Sets each)` : ""}${live.sets.pfBase.real ? ` · Real Sets PF ${formatPf((live.sets.pfBase.real.n ?? live.sets.pfBase.real.orders) ? live.sets.pfBase.real.pf : null)} (${live.sets.pfBase.real.orders} orders)` : ""} · before Base ${formatPf(live.sets.pfBase.unfiltered.n ? live.sets.pfBase.unfiltered.pf : null)}`}
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
        {live?.execution?.overall && Object.keys(live.execution.overall.scopes).length ? (
          <Hero
            k="Real · overall strategies by scope"
            v={`${Object.values(live.execution.overall.scopes).filter((s) => s.allow === true).length}/${Object.values(live.execution.overall.scopes).filter((s) => typeof s.allow === "boolean").length} evaluated scopes open`}
            s={Object.entries(live.execution.overall.scopes)
              .map(([k, s]) => `${k.replace("pack:", "").replace("lane:", "lane ")} ${scopeStatus(s.allow)} · paper PF ${(s.n ?? 0) > 0 ? formatPf(s.pf) : "—"} (${s.n ?? 0}) · ${executionScopeText(s.executed)}`)
              .join(" | ") + (live.execution.overall.blocked ? ` · ${live.execution.overall.blocked} entries held by a scope` : "")}
            tone={Object.values(live.execution.overall.scopes).some((s) => s.allow === false) ? "bad" : "ok"}
          />
        ) : null}
        {live?.execution ? (
          <Hero
            k="Executed · after all gates"
            v={`${live.execution.open.positions} open`}
            s={`${live.execution.sets.valid}/${live.execution.sets.configured} Sets ready · ${live.execution.open.sets} Sets running${live.execution.open.underInvalidSet ? ` (${live.execution.open.underInvalidSet} under a Set not ready for a new entry, handled to close)` : ""} · ${live.execution.closed.orders} orders closed (Set ${live.execution.closed.setLane} · Exit ${live.execution.closed.exitLane}) · ${live.execution.closed.setsThatTraded} Sets traded${live.execution.closed.droppedNoRecord ? ` · ${live.execution.closed.droppedNoRecord} dropped without a record` : ""}`}
            tone="ok"
          />
        ) : null}
        <Hero
          k="Drawdown time avg"
          v={dd.samples ? formatDuration(dd.averageDurationMs) : "—"}
          s={`${dd.episodes} episodes · ${dd.samples} recorded closes · 3d`}
          tone={dd.inDrawdown ? "bad" : "ok"}
        />
        <Hero
          k="Current DD time"
          v={dd.samples ? formatDuration(dd.currentDurationMs) : "—"}
          s={!dd.samples ? "no recorded closes in 3d" : dd.inDrawdown ? `depth ${dd.currentDepth.toFixed(4)}` : "at peak of available 3d tape"}
          tone={!dd.samples ? "ok" : dd.inDrawdown ? "bad" : "good"}
        />
        <Hero
          k="Max DD episode"
          v={dd.samples ? formatDuration(dd.maxDurationMs) : "—"}
          s={`depth ${dd.maxDepth.toFixed(4)} · available 3d tape`}
        />
      </div>
  );
  if (compact) return heroes;
  return (
    <section className="grid gap-3">
      {heroes}

      <StagesCard live={live} />
      <LiftCard live={live} />
      <SimDiffCard live={live} />

      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="Profit factor after costs">
          <p className="mb-3 text-xs text-muted">Recorded net returns are used unchanged. Gross-only rows use a {data.costPf.costPct}% round-trip estimate; this is not proof of actual fill fees.</p>
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
            <th className="pb-2 text-right font-medium" title="Net gains over net losses of the valid Sets' window samples">PF (valid)</th>
            <th className="pb-2 text-right font-medium" title="Sum of net positive and absolute negative returns; not account cash">Net gain / loss</th>
            <th className="pb-2 text-right font-medium">Window orders</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-t border-border font-mono text-xs">
              <td className="py-1.5">{r.label}</td>
              <td className="py-1.5 text-right">{r.sets}</td>
              <td className="py-1.5 text-right">{r.pct.toFixed(1)}%</td>
              <td className={`py-1.5 text-right ${r.pf === null ? "" : r.pf >= 1.1 ? "text-primary" : r.pf < 1 ? "text-danger" : ""}`}>{evidencePfText(live?.sets?.pfBase?.stages?.[r.key], r.orders)}</td>
              <td className="py-1.5 text-right">{live?.sets?.pfBase?.stages?.[r.key]?.netGain?.toFixed(4) ?? "—"} / {live?.sets?.pfBase?.stages?.[r.key]?.netLoss?.toFixed(4) ?? "—"}</td>
              <td className="py-1.5 text-right">{r.orders}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted">
        Each row averages only the Sets that pass that stage (never every Set); the share is against all configured Sets. Base is in-sample (the Sets were selected on those orders): read it next to Main and Real.
        Every stage PF is measured on the window the stage judged, so it is at or above the minimum PF by construction; the PF of orders opened AFTER a decision is the executed figure below.
      </p>
      <WindowsTable live={live} />
      <RelationsTable live={live} />
    </Card>
  );
}

function liftPf(p: { pf: number | null }): string {
  return p.pf === null ? "—" : p.pf >= 99 ? "no loss" : p.pf.toFixed(2);
}

/** Executed Set parents without and with each stack type (Block adds, relation raises): the same parents on both sides, so the difference is the stack's own. */
function LiftCard({ live }: { live: LiveStats | null | undefined }) {
  const lift = live?.execution?.lift;
  const rows = liftRows(lift);
  if (!lift || !rows.length) return null;
  return (
    <Card title="Executed · with against without">
      <table className="w-full text-sm" data-testid="lift-figures">
        <thead className="font-mono text-[11px] text-muted">
          <tr>
            <th className="pb-2 text-left font-medium">Stack type</th>
            <th className="pb-2 text-right font-medium">Parents</th>
            <th className="pb-2 text-right font-medium">PF without</th>
            <th className="pb-2 text-right font-medium">PF with</th>
            <th className="pb-2 text-right font-medium" title="Sum of net returns, percent of one base position">Net without %</th>
            <th className="pb-2 text-right font-medium">Net with %</th>
            <th className="pb-2 text-left font-medium pl-3">Basis</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const better = r.with.pf !== null && r.without.pf !== null && r.with.pf > r.without.pf;
            const worse = r.with.pf !== null && r.without.pf !== null && r.with.pf < r.without.pf;
            return (
              <tr key={r.key} className="border-t border-border font-mono text-xs">
                <td className="py-1.5">{r.label}</td>
                <td className="py-1.5 text-right">{r.n}</td>
                <td className="py-1.5 text-right">{liftPf(r.without)}</td>
                <td className={`py-1.5 text-right ${better ? "text-primary" : worse ? "text-danger" : ""}`}>{liftPf(r.with)}</td>
                <td className="py-1.5 text-right">{r.without.netPct.toFixed(3)}</td>
                <td className={`py-1.5 text-right ${r.with.netPct > r.without.netPct ? "text-primary" : r.with.netPct < r.without.netPct ? "text-danger" : ""}`}>{r.with.netPct.toFixed(3)}</td>
                <td className="py-1.5 pl-3 text-muted">{r.note}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted">
        Settled Set parents only, every return on the parent's original base notional (per-order figures, not USDT).{lift.unknown ? ` ${lift.unknown} parents with an unknown base result or cash are in no sum.` : ""}
        {" "}A stack type that lowers the PF of the same parents is costing money, however its own selection window reads.
        {live?.execution?.overall?.blocked ? ` Axis held ${live.execution.overall.blocked} entries; what those would have made is measured in the walk-forward study, not here.` : ""}
      </p>
    </Card>
  );
}

function pfText(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v >= 99 ? "no loss" : v.toFixed(2);
}

/** Replay against exchange: the same settled closes booked once at the planned stop / target level (the simulator's rule) and once as the exchange filled them. */
function SimDiffCard({ live }: { live: LiveStats | null | undefined }) {
  const d = live?.execution?.simDiff;
  if (!d || !d.closes) return null;
  const rows: Array<[string, SimDiffRow]> = [["All settled closes", d.total], ...Object.entries(d.byExit).map(([k, v]) => [k === "tp" ? "Target exits" : k === "sl" ? "Stop exits" : "Other exits", v] as [string, SimDiffRow]), ...Object.entries(d.byPack).map(([k, v]) => [`Pack · ${k}`, v] as [string, SimDiffRow])];
  const gapClass = (v: number) => (v < 0 ? "text-danger" : v > 0 ? "text-primary" : "");
  return (
    <Card title="Simulated against live">
      <table className="w-full text-sm" data-testid="sim-diff-figures">
        <thead className="font-mono text-[11px] text-muted">
          <tr>
            <th className="pb-2 text-left font-medium">Group</th>
            <th className="pb-2 text-right font-medium">Closes</th>
            <th className="pb-2 text-right font-medium">PF sim</th>
            <th className="pb-2 text-right font-medium">PF live</th>
            <th className="pb-2 text-right font-medium" title="Sum of net returns, percent of one position">Net sim %</th>
            <th className="pb-2 text-right font-medium">Net live %</th>
            <th className="pb-2 text-right font-medium" title="Live fill against the planned stop / target level">Fill gap %</th>
            <th className="pb-2 text-right font-medium" title="Configured PositionCost against the fees actually paid">Cost gap %</th>
            <th className="pb-2 text-right font-medium">Net gap %</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, r]) => (
            <tr key={label} className="border-t border-border font-mono text-xs">
              <td className="py-1.5">{label}</td>
              <td className="py-1.5 text-right">{r.n}</td>
              <td className="py-1.5 text-right">{pfText(r.simPf)}</td>
              <td className="py-1.5 text-right">{pfText(r.livePf)}</td>
              <td className="py-1.5 text-right">{r.simNetPct.toFixed(3)}</td>
              <td className="py-1.5 text-right">{r.liveNetPct.toFixed(3)}</td>
              <td className={`py-1.5 text-right ${gapClass(r.fillGapPct)}`}>{r.fillGapPct.toFixed(3)}</td>
              <td className={`py-1.5 text-right ${gapClass(r.costGapPct)}`}>{r.costGapPct.toFixed(3)}</td>
              <td className={`py-1.5 text-right ${gapClass(r.netGapPct)}`}>{r.netGapPct.toFixed(3)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted" data-testid="sim-diff-factors">
        Stop fill factor (live loss over planned stop; the replay assumes 1.00): median {d.stopFactor.median ?? "—"}, mean {d.stopFactor.mean ?? "—"}, 90th percentile {d.stopFactor.p90 ?? "—"} on {d.stopFactor.n} stops.
        {" "}Target fill factor: median {d.targetFactor.median ?? "—"} on {d.targetFactor.n} targets.
        {" "}A stop factor well above 1 means live stops cost more than the replay shows: raise the minimum stop distance or drop thin symbols rather than trust the simulated PF.
        {d.unknown ? ` ${d.unknown} closes without a verified exit or net result are in no sum.` : ""}
      </p>
    </Card>
  );
}

const RELATION_NAME: Record<string, string> = { overall: "Overall", indication: "Indication", type: "Type", range: "TP range", step: "Step" };

/** Relation lanes: per relation the lanes built and passing, and the pooled PF of every observation their windows read (one observation = one entry). */
export function RelationsTable({ live }: { live: LiveStats | null | undefined }) {
  const report = live?.sets?.relations;
  const rows = relationFigures(report);
  if (!rows.length) return null;
  const lanes = (report?.rows ?? []).filter((r) => r.ok).slice(0, 12);
  const cfg = report?.config;
  return (
    <div className="mt-4 overflow-x-auto" data-testid="relation-lanes">
      <div className="mb-1 font-mono text-[11px] text-muted">
        Relation lanes · post-Base orders by relation, each entry counted once · Main last {cfg?.mainN}, Real last {cfg?.realN || "off"}, minimum PF {cfg?.minPf?.toFixed(2)} ·
        {" "}{report?.passing ?? 0} of {report?.lanes ?? 0} lanes pass · volume ×1 to ×{cfg?.maxMult?.toFixed(2)}
      </div>
      <table className="w-full min-w-[40rem] text-sm">
        <thead className="font-mono text-[11px] text-muted">
          <tr>
            <th className="pb-2 text-left font-medium">Relation</th>
            <th className="pb-2 text-left font-medium">Lane type</th>
            <th className="pb-2 text-right font-medium">Lanes</th>
            <th className="pb-2 text-right font-medium" title="Lanes whose Main window passes">Main pass</th>
            <th className="pb-2 text-right font-medium" title="Lanes passing Main and Real: these raise the volume">Pass</th>
            <th className="pb-2 text-right font-medium" title="Gains over losses of every observation the Main windows read, all lanes of the relation">Main PF</th>
            <th className="pb-2 text-right font-medium" title="The same over the Real windows of the passing lanes (in-sample: the lanes were selected on it)">PF (passing)</th>
            <th className="pb-2 text-right font-medium">Entries</th>
            <th className="pb-2 text-right font-medium" title="Volume added to an entry while the lane passes">Ratio</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className={`border-t border-border font-mono text-xs ${r.on ? "" : "text-muted"}`}>
              <td className="py-1.5">{RELATION_NAME[r.key] ?? r.key}</td>
              <td className="py-1.5">{r.role}</td>
              <td className="py-1.5 text-right">{r.lanes}</td>
              <td className="py-1.5 text-right">{r.mainOk}</td>
              <td className="py-1.5 text-right">{r.ok}</td>
              <td className={`py-1.5 text-right ${r.mainObs < 1 ? "" : r.mainPf >= 1.1 ? "text-primary" : r.mainPf < 1 ? "text-danger" : ""}`}>{r.mainObs < 1 ? "—" : r.mainPf >= 99 ? "no loss" : r.mainPf.toFixed(2)}</td>
              <td className="py-1.5 text-right">{r.passObs < 1 ? "—" : r.passPf >= 99 ? "no loss" : r.passPf.toFixed(2)}</td>
              <td className="py-1.5 text-right">{r.entries}</td>
              <td className="py-1.5 text-right">{r.ratio > 0 ? `+${r.ratio.toFixed(2)}` : "observe"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {lanes.length ? (
        <p className="mt-2 font-mono text-[11px] text-muted">
          Passing now: {lanes.map((l) => `${l.pack} ${l.side} ${l.relation}${l.value ? ` ${l.value}` : ""} (${l.entries} entries, Main ${l.mainPf >= 99 ? "no loss" : l.mainPf.toFixed(2)})`).join(" · ")}
          {(report?.passing ?? 0) > lanes.length ? ` · +${(report?.passing ?? 0) - lanes.length} more` : ""}
        </p>
      ) : null}
      <p className="mt-2 text-xs text-muted">
        A relation lane is judged on the orders its Sets opened after Base, never on the raw tapes. Its windows are in-sample for the lanes that pass; what a raise is worth is the executed figure, stated per order and in USDT separately.
      </p>
    </div>
  );
}

/** Every last-N window on its own: the Sets passing that window alone, and the pooled PF and order count of the orders it reads. */
export function WindowsTable({ live }: { live: LiveStats | null | undefined }) {
  const rows = windowFigures(live?.sets?.pfBase);
  if (!rows.length) return null;
  return (
    <div className="mt-4 overflow-x-auto">
      <div className="mb-1 font-mono text-[11px] text-muted">Last-N windows · full samples and passing PF are separate. Negative requires all configured dip windows for the sample count; Main short uses its selected N.</div>
      <table className="w-full min-w-[40rem] text-sm" data-testid="window-figures">
        <thead className="font-mono text-[11px] text-muted">
          <tr>
            <th className="pb-2 text-left font-medium">Window</th>
            <th className="pb-2 text-right font-medium">Setting</th>
            <th className="pb-2 text-right font-medium" title="Sets judged by this window's stage">Judged</th>
            <th className="pb-2 text-right font-medium" title="Full requested N in this Set's own tape, including losing windows. Empty and unmeasured Sets remain in the denominator.">Enough samples</th>
            <th className="pb-2 text-right font-medium">Pass</th>
            <th className="pb-2 text-right font-medium" title="Share of the Sets its stage judges">% of judged</th>
            <th className="pb-2 text-right font-medium" title="Net gains over net losses of the samples this window reads, over the Sets that pass it">PF</th>
            <th className="pb-2 text-right font-medium" title="Orders behind that PF">Orders</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className={`border-t border-border font-mono text-xs ${r.on ? "" : "text-muted"}`}>
              <td className="py-1.5">{r.stage} · {r.label}{r.chosen ? <span className="ml-2 text-muted">({r.chosen})</span> : null}</td>
              <td className="py-1.5 text-right">{r.setting}</td>
              <td className="py-1.5 text-right">{r.on ? r.pop : "—"}</td>
              <td className="py-1.5 text-right">{(() => {
                const s = live?.sets?.pfBase?.windows?.[r.key]?.samples;
                return !r.on ? "Disabled" : !s || s.pct == null ? "Unavailable" : `${s.full}/${s.population} (${s.pct.toFixed(1)}%)`;
              })()}</td>
              <td className="py-1.5 text-right">{r.on ? r.sets : "—"}</td>
              <td className="py-1.5 text-right">{r.on ? `${r.pct.toFixed(1)}%` : "—"}</td>
              <td className={`py-1.5 text-right ${r.pf === null ? "" : r.pf >= 1.1 ? "text-primary" : r.pf < 1 ? "text-danger" : ""}`}>{!r.on ? "Disabled" : live?.sets?.pfBase?.windows?.[r.key]?.role === "coordination" ? "Sign condition" : evidencePfText(live?.sets?.pfBase?.windows?.[r.key], r.orders)}</td>
              <td className="py-1.5 text-right">{r.on ? r.orders : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
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
        {evidencePfText(m, m.count)}
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
