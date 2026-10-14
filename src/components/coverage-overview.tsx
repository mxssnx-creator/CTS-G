import type { LiveStats } from "@/lib/live-stats";
import { posOrdersCounts, formatEffectiveSets } from "@/lib/live-stats";
import { formatDuration } from "@/lib/analytics";
import { HistTestStatus } from "@/components/hist-test-controls";
import { histTestIsEnabled } from "@/lib/hist-test";
import { PosOrdersLine } from "@/components/pos-orders";
import { INDICATION_KINDS, mergeKindTypes } from "@/lib/indication-kinds";
import { POSITIVE_PF } from "@/lib/config-model";

const PACKS = ["indications", "general", "block", "trailing", "dca", "exits", "coord", "sets", "rearrange", "trailRecalc"] as const;
const TYPES = INDICATION_KINDS;
const STAGE_ROWS = ["base", "main", "real"] as const;

type StageBlob = { pf?: number; n?: number; open?: boolean; minPf?: number };

/** `pf/n ≥ minPf` for one coordination stage. */
export function stageLabel(stage?: StageBlob): string {
  if (!stage) return "—";
  const floor = stage.minPf != null ? ` ≥ ${Number(stage.minPf).toFixed(2)}` : "";
  return `${Number(stage.pf ?? 0).toFixed(2)}/${Math.round(Number(stage.n ?? 0))}${floor}`;
}

function stageOk(stage?: StageBlob): boolean | undefined {
  if (!stage || stage.minPf == null || !Number(stage.n ?? 0)) return undefined;
  return Number(stage.pf ?? 0) >= Number(stage.minPf);
}

type CoverageExtras = {
  microCount?: number;
  microMinPf?: number;
};

type AxisAggregate = {
  parentCount?: number;
  parentSideCount?: number;
  childCount?: number;
  qualifiedChildren?: number;
};

function stageFlowLine(live: LiveStats | null): string {
  const flow = live?.coverage?.stageFlow;
  const stages = flow?.stages ?? {};
  const order = flow?.stageOrder?.length ? flow.stageOrder : Object.keys(stages);
  return order
    .filter((name) => stages[name])
    .map((name) => `${name} ${stages[name]?.qualified ?? 0}/${stages[name]?.evaluated ?? 0}`)
    .join(" → ");
}

function axisLine(live: LiveStats | null): string {
  const ax = (live?.coverage?.coord?.variants ?? {}) as AxisAggregate;
  if (ax.parentCount == null && ax.childCount == null) return "";
  return `parents ${ax.parentCount ?? 0} · set×side ${ax.parentSideCount ?? "—"} · children ${ax.childCount ?? 0}${ax.qualifiedChildren != null ? ` (${ax.qualifiedChildren} qualified)` : ""}`;
}

export function CoverageBar({ live }: { live: LiveStats | null }) {
  if (!live) return null;
  const cov = live.coverage;
  const scan = cov?.scan;
  const strat = cov?.strategies ?? {};
  const types = mergeKindTypes(live.indications?.types, cov?.indicationTypes);
  const hits = cov?.indicationHits ?? live.indications?.typeHits ?? {};
  const sets = (cov?.sets ?? {}) as CoverageExtras & {
    setCount?: number;
    activeCount?: number;
    validatedCount?: number;
    histFills?: number;
    liveFills?: number;
    liveProcessed?: number;
    liveActive?: number;
    livePf?: number;
    liveNetAvg?: number;
    entryCandidateCount?: number;
    entryQueue?: { eligible: number; opened: number; pending: number; remaining: number; updatedAt?: number };
    entryCandidateCap?: number;
    costSubtracted?: boolean;
    families?: { base?: number; trail?: number };
    trailCover?: boolean;
    product?: number;
  };
  const ctrl = cov?.controls;
  const recon = cov?.recon;
  const stages = (cov?.coord?.stages ?? live.coord?.stages ?? {}) as Record<string, StageBlob>;
  const track = cov?.tracking;
  const load = cov?.load ?? live.engine?.load;
  const px = cov?.px ?? scan?.px ?? 0;
  const n = cov?.symbols ?? scan?.universe ?? live.symbolCount ?? 0;
  const full = n > 0 && px >= n;
  const perConfig = (live.pulse as { controlOrdersPerConfig?: unknown } | undefined)?.controlOrdersPerConfig !== false;
  const controlMode = ctrl?.mode ?? (perConfig ? "per-config" : "aggregate");
  const groupCount = ctrl?.groupCount ?? ctrl?.open ?? live.openCount ?? 0;
  const pairCount = ctrl?.pairCount ?? (live.pulse?.controlOrders === false ? 0 : groupCount);
  const mergedMembers = ctrl?.mergedMembers;
  const laneFlags = (live.pulse?.strategyLanes ?? {}) as Record<string, boolean>;
  const miss = ctrl?.missing ?? 0;
  return (
    <div className="min-w-0 rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs" data-testid="coverage-strip">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={full ? "text-primary" : "text-warn"}>
          coverage · px {px}/{n || "—"} · 1m {scan?.kl1m ?? "—"} · 5m {scan?.kl5m ?? "—"} · 15m {scan?.kl15m ?? "—"} · ind {scan?.indications ?? "—"}
        </span>
        <span className="min-w-0 text-muted [overflow-wrap:anywhere]">
          {formatEffectiveSets(live)}
          {sets.families ? ` · base ${sets.families.base ?? 0}/trail ${sets.families.trail ?? 0}` : ""}
          {sets.liveProcessed != null ? ` · live ${sets.liveActive ?? 0}/${sets.liveProcessed} PF ${Number(sets.livePf ?? 0).toFixed(2)}` : ""}
          {sets.histFills != null ? ` · hist ${sets.histFills}` : ""}
          {sets.entryCandidateCount != null ? ` · candidates ${sets.entryCandidateCount}${sets.entryCandidateCap === 0 ? " (unlimited)" : ""}` : ""}
          {sets.entryQueue ? ` · opened ${sets.entryQueue.opened} · pending ${sets.entryQueue.pending} · remaining ${sets.entryQueue.remaining}` : ""}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-2">
        {PACKS.map((k) => (
          <span key={k} className={strat[k] ? "text-fg" : "text-faint"}>
            {k} {strat[k] ? "on" : "off"}
          </span>
        ))}
      </div>
      <div className="mt-1 flex flex-wrap gap-2 text-muted">
        {TYPES.map((k) => {
          const gate = (cov?.indicationGate || {})[k];
          const pf = gate?.pf;
          const n = gate?.n;
          return (
            <span key={k} className={types[k] === false ? "text-faint" : "text-fg"}>
              {k} {types[k] === false ? "off" : hits[k] ?? 0}
              {n ? ` · ${Number(pf ?? 0).toFixed(2)}` : ""}
            </span>
          );
        })}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-muted">
        <PosOrdersLine stats={live} className={miss ? "text-danger" : "text-primary"} />
        <span className="whitespace-nowrap">controls {controlMode}</span>
        <span className="whitespace-nowrap">{ctrl?.ok ?? 0}/{ctrl?.open ?? live.openCount ?? 0} logical SL+TP</span>
        <span className="whitespace-nowrap">{ctrl?.exchangePositionGroups ?? recon?.exchangePositionGroups ?? "—"} exchange groups</span>
        <span className="whitespace-nowrap">{pairCount} pairs</span>
        <span className="whitespace-nowrap">{ctrl?.security ?? 0} sec</span>
        {mergedMembers != null ? <span className="whitespace-nowrap">{mergedMembers} members</span> : null}
        {Object.keys(laneFlags).length ? <span className="min-w-0 [overflow-wrap:anywhere]">lanes {Object.entries(laneFlags).filter(([, enabled]) => enabled).map(([name]) => name).join(",") || "none"}</span> : null}
        <span className={recon?.ok === false ? "text-danger" : recon?.pending ? "text-warn" : "text-primary"}>
          recon {recon?.ok === false ? recon.detail || "gap" : recon?.pending ? recon.detail || "pending" : "ok"}
        </span>
        <span className="whitespace-nowrap">
          block {cov?.block?.enabled ? "on" : "off"} · {cov?.block?.countN ?? 0} counts · {cov?.block?.liveLanes ?? 0} lanes
          {cov?.block?.overall !== false ? " · overall Real" : ""}
        </span>
        {sets.trailCover === false ? <span className="text-warn">trail cover gap</span> : null}
        {stages.base || stages.main || stages.real ? (
          <span className="whitespace-nowrap">
            {STAGE_ROWS.map((name) => `${name} ${stageLabel(stages[name])}`).join(" · ")}
            {stages.intern?.open ? " · intern open" : ""}
          </span>
        ) : null}
        {sets.microCount != null ? (
          <span className="whitespace-nowrap">
            micro {sets.microCount}
            {sets.microMinPf != null ? ` ≥ ${Number(sets.microMinPf).toFixed(2)}` : ""}
          </span>
        ) : null}
        {track ? (
          <span className="whitespace-nowrap">
            track {track.withCid ?? 0}/{track.ours ?? 0} cid · {track.withSet ?? 0} set · foreign {track.foreign ?? 0}
          </span>
        ) : null}
        {load ? (
          <span className={load.level === "critical" || load.level === "overload" ? "text-danger" : "text-muted"}>
            load {load.level ?? "—"} · chunk {load.scanChunk ?? "—"} · rss {load.rssMb ?? "—"}MB
            {load.shed?.length ? ` · shed ${load.shed.join(",")}` : ""}
          </span>
        ) : null}
      </div>
      {scan?.missingInd?.length ? (
        <p className="mt-1 text-warn">ind gap {scan.missingInd.slice(0, 8).join(" · ")}</p>
      ) : null}
      <HistTestStatus histTest={live.histTest} compact />
    </div>
  );
}

export function CoveragePanel({ live }: { live: LiveStats | null }) {
  const cov = live?.coverage;
  const strat = cov?.strategies ?? {};
  const types = mergeKindTypes(live?.indications?.types, cov?.indicationTypes);
  const hits = cov?.indicationHits ?? live?.indications?.typeHits ?? {};
  const blk = cov?.block;
  const stages = (cov?.coord?.stages ?? live?.coord?.stages ?? {}) as Record<string, StageBlob>;
  const blockFloor = (row: { symbol?: string; side?: string; minPf?: number }) => {
    const lane = (live?.block?.lanes ?? []).find((ln) => ln.symbol === row.symbol && ln.side === row.side);
    const laneFloor = lane?.counts?.length ? Math.min(...lane.counts.map((c) => Number(c.minPF))) : undefined;
    return Number(row.minPf ?? laneFloor ?? stages.real?.minPf ?? live?.block?.defaultMinPF ?? POSITIVE_PF);
  };
  const flowLine = stageFlowLine(live ?? null);
  const axisAgg = axisLine(live ?? null);
  const counts = blk?.allCounts ?? live?.block?.allCounts ?? [];
  const scan = cov?.scan;
  const sets = (cov?.sets ?? {}) as CoverageExtras & {
    setCount?: number;
    activeCount?: number;
    validatedCount?: number;
    histFills?: number;
    liveFills?: number;
    liveProcessed?: number;
    liveActive?: number;
    livePf?: number;
    liveNetAvg?: number;
    entryCandidateCount?: number;
    entryQueue?: { eligible: number; opened: number; pending: number; remaining: number; updatedAt?: number };
    entryCandidateCap?: number;
    costSubtracted?: boolean;
    families?: { base?: number; trail?: number };
    trailCover?: boolean;
    independentTrail?: boolean;
    product?: number;
    packs?: string[];
    slRatios?: number[];
    trails?: string[];
    steps?: number[];
    dims?: { pack?: number; sl?: number; trail?: number; step?: number };
  };
  const liveSets = live?.sets?.liveOverview;
  const ctrl = cov?.controls;
  const recon = cov?.recon;
  const px = cov?.px ?? scan?.px ?? 0;
  const n = cov?.symbols ?? scan?.universe ?? live?.symbolCount ?? 0;
  const open = live?.open ?? [];
  const perConfig = (live?.pulse as { controlOrdersPerConfig?: unknown } | undefined)?.controlOrdersPerConfig !== false;
  const controlMode = ctrl?.mode ?? (perConfig ? "per-config" : "aggregate");
  const groupCount = ctrl?.groupCount ?? ctrl?.open ?? open.length;
  const pairCount = ctrl?.pairCount ?? (live?.pulse?.controlOrders === false ? 0 : groupCount);
  const mergedMembers = ctrl?.mergedMembers;
  const po = posOrdersCounts(live);
  const groupGaps = (ctrl?.groups ?? []).filter((group) => !group.protected).slice(0, 10);
  const gaps = open.filter((p) => !p.controls || !(p.secSlOid && p.secTpOid)).slice(0, 10);
  return (
    <div className="space-y-3" data-testid="coverage-panel">
      <CoverageBar live={live} />
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <KV k="Scan universe" v={`${px}/${n || 0} px`} ok={n > 0 && px >= n} />
        <KV k="Klines 1/5/15" v={`${scan?.kl1m ?? "—"} / ${scan?.kl5m ?? "—"} / ${scan?.kl15m ?? "—"}`} ok={Boolean(scan && scan.kl1m && scan.kl5m && scan.kl15m)} />
        <KV k="Indications" v={`${scan?.indications ?? 0}${scan?.missingInd?.length ? ` · gap ${scan.missingInd.length}` : ""}`} ok={!scan?.missingInd?.length} />
        <KV k="Recon" v={String(recon?.detail || (recon?.pending ? "pending" : recon?.ok ? "ok" : "—"))} ok={recon?.ok !== false && !recon?.pending} problem={recon?.ok === false} />
        <KV k="Positions/Orders" v={`Pos R ${po.realPositions ?? "—"} L ${po.livePositions ?? "—"} · Ord R ${po.realOrders ?? "—"} L ${po.liveOrders ?? "—"}`} />
        <KV k="Controls" v={`${ctrl?.ok ?? 0}/${ctrl?.open ?? open.length} SL+TP · ${pairCount} pairs · ${ctrl?.security ?? 0} security`} ok={!(ctrl?.missing)} problem={Boolean(ctrl?.missing)} />
        <KV k="Control groups" v={`${controlMode} · ${groupCount} groups${mergedMembers != null ? ` · ${mergedMembers} members` : ""}`} ok={!(ctrl?.missing)} problem={Boolean(ctrl?.missing)} />
        <KV k="Sets" v={formatEffectiveSets(live)} ok={(sets.validatedCount ?? 0) > 0 || histTestIsEnabled(live?.histTest)} />
        <KV k="Test Historic" v={histTestIsEnabled(live?.histTest) ? `ON · ${formatEffectiveSets(live)}` : "OFF · full catalog"} ok={histTestIsEnabled(live?.histTest)} />
        <KV
          k="Live sets (cost-net)"
          v={`${sets.liveActive ?? liveSets?.active ?? 0}/${sets.liveProcessed ?? liveSets?.processed ?? 0} processed · PF ${Number(sets.livePf ?? liveSets?.last15Ratio ?? 0).toFixed(2)} · n ${sets.liveFills ?? liveSets?.fills ?? 0}`}
          ok={(sets.liveProcessed ?? liveSets?.processed ?? 0) >= 0}
        />
        <KV k="Set families" v={`base ${sets.families?.base ?? "—"} · trail ${sets.families?.trail ?? "—"}${sets.independentTrail ? " · independent" : ""}`} />
        <KV k="Block" v={`${blk?.enabled ? "on" : "off"} · stack ${blk?.maxStack ?? "—"} · ${blk?.liveLanes ?? 0} lanes${blk?.overall !== false ? " · overall Real" : ""}`} />
        <KV
          k="Overall Block Real"
          v={
            (blk?.overallReal ?? []).length
              ? (blk?.overallReal ?? [])
                  .slice(0, 4)
                  .map((row) => `${String(row.symbol || "").replace("-USDT", "")} ${row.side} ${Number(row.pf ?? 0).toFixed(2)}/${row.n ?? 0}`)
                  .join(" · ")
              : "no live parent"
          }
          ok={(blk?.overallReal ?? []).some((row) => Number(row.pf ?? 0) >= blockFloor(row))}
        />
        <KV
          k="Stages base/main/real · pf/n ≥ min"
          v={STAGE_ROWS.map((name) => stageLabel(stages[name])).join(" · ")}
          ok={STAGE_ROWS.every((name) => stageOk(stages[name]) !== false)}
        />
        {sets.microCount != null ? (
          <KV
            k="Micro sets"
            v={`${sets.microCount}${sets.microMinPf != null ? ` · PF ≥ ${Number(sets.microMinPf).toFixed(2)}` : ""}`}
          />
        ) : null}
        {flowLine ? <KV k="Stage flow (qualified/evaluated)" v={flowLine} /> : null}
        {axisAgg ? <KV k="Axis coordination" v={axisAgg} /> : null}
        <KV
          k="Tracking"
          v={`cid ${cov?.tracking?.withCid ?? 0}/${cov?.tracking?.ours ?? 0} · set ${cov?.tracking?.withSet ?? 0} · foreign ${cov?.tracking?.foreign ?? 0}`}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        {PACKS.map((k) => (
          <span
            key={k}
            className={`rounded-full border px-2 py-0.5 font-mono text-xs ${strat[k] ? "border-primary text-primary" : "border-border text-faint"}`}
          >
            {k} {strat[k] ? "ON" : "off"}
          </span>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        {TYPES.map((k) => {
          const gate = (cov?.indicationGate || {})[k];
          const pf = gate?.pf;
          const n = gate?.n;
          return (
            <span
              key={k}
              className={`rounded-full border px-2 py-0.5 font-mono text-xs ${types[k] !== false ? "border-primary text-primary" : "border-border text-faint"}`}
            >
              {k} {types[k] === false ? "off" : `ON · ${hits[k] ?? 0}`}
              {n ? ` · PF ${Number(pf ?? 0).toFixed(2)}` : ""}
            </span>
          );
        })}
      </div>
      {sets.dims ? (
        <p className="font-mono text-xs text-muted">
          set product {sets.product ?? "—"} · pack {sets.dims.pack} × sl {sets.dims.sl} × trail {sets.dims.trail} × step {sets.dims.step}
          {sets.trailCover === false ? " · trail cover gap" : " · trail cover ok"}
        </p>
      ) : null}
      {(liveSets?.rows || []).length ? (
        <div className="overflow-x-auto">
          <p className="mb-1 font-mono text-[11px] uppercase text-muted">Live on-exchange sets · cost subtracted</p>
          <table className="w-full min-w-[560px] text-left text-xs">
            <thead className="font-mono text-muted">
              <tr>
                <th className="pb-1 font-medium">Set</th>
                <th className="pb-1 font-medium">Pack</th>
                <th className="pb-1 text-right font-medium">n</th>
                <th className="pb-1 text-right font-medium">PF</th>
                <th className="pb-1 text-right font-medium">net</th>
                <th className="pb-1 text-right font-medium">DDt</th>
                <th className="pb-1 font-medium">state</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {(liveSets?.rows || []).slice(0, 12).map((r) => {
                const id = String(r.id || "");
                const on = Boolean(r.active);
                return (
                  <tr key={id} className={`border-t border-border ${on ? "text-fg" : "text-muted"}`}>
                    <td className="py-1">{id.replace("-USDT", "")}</td>
                    <td className="py-1">{String(r.pack || "")}</td>
                    <td className="py-1 text-right">{r.n ?? 0}</td>
                    <td className="py-1 text-right">{Number(r.last15Ratio ?? 0).toFixed(2)}</td>
                    <td className="py-1 text-right">{(Number(r.netAvg ?? 0) * 100).toFixed(3)}%</td>
                    <td className="py-1 text-right">{formatDuration(Number(r.maxDdS ?? 0) * 1000)}</td>
                    <td className="py-1">{on ? "on" : String(r.deactReason || "off")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
      {groupGaps.length ? (
        <p className="font-mono text-xs text-danger">
          range gap {groupGaps.map((group) => `${group.symbol?.replace("-USDT", "")} ${group.side === "LONG" ? "L" : "S"} ${group.range ?? "aggregate"}`).join(" · ")}
        </p>
      ) : gaps.length ? (
        <p className="font-mono text-xs text-danger">
          control gap {gaps.map((p) => `${p.symbol.replace("-USDT", "")} ${p.side === "LONG" ? "L" : "S"}`).join(" · ")}
        </p>
      ) : (
        <p className="font-mono text-xs text-muted">Every logical group has order SL+TP and symbol+direction security</p>
      )}
      {counts.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="font-mono text-muted">
              <tr>
                <th className="pb-1 font-medium">#</th>
                <th className="pb-1 text-right font-medium">inc</th>
                <th className="pb-1 text-right font-medium">add</th>
                <th className="pb-1 text-right font-medium">total</th>
                <th className="pb-1 text-right font-medium">min PF</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {counts.map((c) => (
                <tr key={c.n} className="border-t border-border">
                  <td className="py-1">{c.n}</td>
                  <td className="py-1 text-right">{Number(c.inc).toFixed(2)}×</td>
                  <td className="py-1 text-right">{Number(c.targetAdd).toFixed(2)}</td>
                  <td className="py-1 text-right">{Number(c.targetBlock ?? (1 + Number(c.targetAdd ?? 0))).toFixed(2)}</td>
                  <td className="py-1 text-right">{Number(c.minPF).toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function KV({ k, v, ok, problem }: { k: string; v: string; ok?: boolean; problem?: boolean }) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-bg2 px-3 py-2">
      <div className="font-mono text-xs text-muted">{k}</div>
      <div className={`mt-0.5 break-words text-sm leading-snug ${problem ? "text-danger" : ok === false ? "text-warn" : ok ? "text-primary" : ""}`}>{v || "—"}</div>
    </div>
  );
}
