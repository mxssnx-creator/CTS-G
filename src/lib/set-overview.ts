import type { LiveStats } from "./live-stats";

export type SetScope = "system" | "exchange";
export type SetOverviewRow = {
  id: string;
  setId?: string;
  parentSetId?: string;
  scope: SetScope;
  indicationKind: string;
  indicationConfig?: string;
  strategyType: string;
  tpRange: string;
  tpPct?: number | null;
  pack?: string;
  tf?: string;
  slRatio?: number | null;
  trailKey?: string;
  step?: number | null;
  axisKey?: string;
  relativeCount?: number;
  side?: string;
  connection?: string;
  n: number;
  last15Ratio?: number | null;
  last25AvgR?: number | null;
  maxDdS?: number | null;
  avgDdS?: number | null;
  wr?: number | null;
  expectancy?: number | null;
  avgHoldS?: number | null;
  active: boolean;
  deactReason?: string;
};

export type SetGroup = Pick<SetOverviewRow, "scope" | "indicationKind" | "strategyType" | "tpRange" | "tpPct"> & { setCount: number };
export type SetOverview = { version: number; generatedAt?: number; previewPerGroup?: number; connections?: string[]; groups: SetGroup[]; rows: SetOverviewRow[] };
export type SetSelection = { scope: SetScope; indication: string; range: string; strategy: string };
export const INITIAL_SET_SELECTION: SetSelection = { scope: "system", indication: "all", range: "all", strategy: "all" };
export const INDICATION_GROUPS = ["general", "state", "signals", "active", "direction", "move", "common", "trend", "break", "combined"];
export const STRATEGY_GROUPS = ["normal", "trailing", "axis", "block", "dca"];

export function matchesSetGroup(row: SetGroup | SetOverviewRow, selection: SetSelection) {
  return row.scope === selection.scope
    && (selection.indication === "all" || row.indicationKind === selection.indication)
    && (selection.range === "all" || row.tpRange === selection.range)
    && (selection.strategy === "all" || row.strategyType === selection.strategy);
}

export function changeSetSelection(selection: SetSelection, level: keyof SetSelection, value: string): SetSelection {
  if (level === "scope") return { ...INITIAL_SET_SELECTION, scope: value as SetScope };
  if (level === "indication") return { ...selection, indication: value, range: "all", strategy: "all" };
  if (level === "range") return { ...selection, range: value, strategy: "all" };
  return { ...selection, strategy: value };
}

export function tpRangeLabel(value: string) {
  return value === "unknown" ? "Unassigned" : `${Number(value).toLocaleString("en-US", { maximumFractionDigits: 4 })}%`;
}

/** Older snapshots must not substitute a blended PF for either source. */
export function readSetOverview(sets: LiveStats["sets"]): SetOverview {
  if (sets?.overview?.version === 1) return sets.overview;
  const rows: SetOverviewRow[] = [];
  for (const row of sets?.rows ?? []) {
    const base = {
      ...row, setId: row.id, indicationKind: row.pack === "general" ? "general" : "combined",
      strategyType: row.axisKey ? "axis" : row.kind === "trail" || Boolean(row.trailKey && !["0", "off"].includes(row.trailKey)) ? "trailing" : "normal",
      tpRange: row.tpPct && row.tpPct > 0 ? row.tpPct.toFixed(4) : "unknown",
    };
    if (!row.liveN && !row.live?.n && row.source !== "live-exchange") {
      rows.push({ ...base, id: `system:${row.id}`, scope: "system" });
    }
    if (row.live?.source === "live-exchange" && (row.live.n ?? 0) > 0) {
      rows.push({ ...base, id: `exchange:${row.id}`, scope: "exchange", n: row.live.n!,
        last15Ratio: row.live.last15Ratio, maxDdS: row.live.maxDdS, wr: row.live.wr,
        expectancy: row.live.netAvg, last25AvgR: null, avgDdS: null, avgHoldS: null });
    }
  }
  return { version: 0, rows, groups: rows.map((row) => ({ ...row, setCount: 1 })) };
}

export function setMetric(value: number | null | undefined, decimals = 2) {
  return value != null && Number.isFinite(value) ? value.toFixed(decimals) : "—";
}

export function enabledAxes(stats: Pick<LiveStats, "coord" | "coverage"> | null) {
  const axes = stats?.coord?.axes ?? stats?.coverage?.coord?.axes ?? {};
  return Object.keys(axes).filter((key) => axes[key]?.enabled === true);
}

export function setLabel(row: SetOverviewRow) {
  return [row.indicationKind, row.indicationConfig, row.strategyType, row.pack, row.tf, `TP ${tpRangeLabel(row.tpRange)}`,
    row.slRatio == null ? "" : `sl${setMetric(row.slRatio, 2)}`,
    row.step ? `st${row.step}` : "", row.trailKey ? `trail ${row.trailKey}` : "", row.axisKey, row.side, row.connection].filter(Boolean).join(" · ");
}

export function compactSetLabel(row: SetOverviewRow) {
  const name = (value: string) => value === "dca" ? "DCA" : value.charAt(0).toUpperCase() + value.slice(1);
  return `${name(row.indicationKind)} · ${name(row.strategyType)} · TP ${tpRangeLabel(row.tpRange)}`;
}
