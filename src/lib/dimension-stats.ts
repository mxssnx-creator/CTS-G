import type { SetGroup, SetOverviewRow } from "./set-overview";

export const STRATEGY_COLORS: Record<string, string> = {
  normal: "#3dcf8e", trailing: "#79b8ff", axis: "#c9a2ff", block: "#e0b15a", dca: "#ef6f63",
};

export type MetricPoint = SetOverviewRow & { pf: number; ddMinutes: number; samples: number };

/** Counts describe the complete catalog. Ratios are never summed or averaged. */
export function dimensionMatrix(groups: SetGroup[]) {
  const cells = new Map<string, { indication: string; strategy: string; count: number }>();
  for (const group of groups) {
    const key = JSON.stringify([group.indicationKind, group.strategyType]);
    const cell = cells.get(key) ?? { indication: group.indicationKind, strategy: group.strategyType, count: 0 };
    cell.count += Number.isFinite(group.setCount) ? Math.max(0, Math.trunc(group.setCount)) : 0;
    cells.set(key, cell);
  }
  return [...cells.values()];
}

export function metricPoints(rows: SetOverviewRow[], limit = 350): { points: MetricPoint[]; available: number } {
  const buckets = new Map<string, MetricPoint[]>();
  let available = 0;
  for (const row of rows) {
    // A real zero is meaningful; missing, non-finite and empty measurements are not.
    if (!(row.n > 0) || !Number.isFinite(row.n) || row.last15Ratio == null || !Number.isFinite(row.last15Ratio)
      || row.maxDdS == null || !Number.isFinite(row.maxDdS) || row.maxDdS < 0) continue;
    const key = JSON.stringify([row.connection ?? "", row.indicationKind, row.strategyType]);
    const bucket = buckets.get(key) ?? [];
    bucket.push({ ...row, pf: row.last15Ratio, ddMinutes: row.maxDdS / 60, samples: row.n });
    buckets.set(key, bucket);
    available++;
  }
  const ordered = [...buckets].sort(([a], [b]) => a.localeCompare(b)).map(([, rows]) => rows.sort((a, b) => a.id.localeCompare(b.id)));
  const points: MetricPoint[] = [];
  const cap = Number.isFinite(limit) ? Math.max(0, Math.min(350, Math.trunc(limit))) : 350;
  // Round-robin preserves small indication/strategy families in a bounded plot.
  for (let index = 0; points.length < Math.min(available, cap); index++) {
    for (const bucket of ordered) {
      if (bucket[index]) points.push(bucket[index]);
      if (points.length >= cap) break;
    }
  }
  return { points, available };
}

export type DetailSort = "identity" | "pf" | "ddt" | "samples";
export function sortSetDetails(rows: SetOverviewRow[], sort: DetailSort) {
  const metric = (row: SetOverviewRow) => sort === "pf" ? row.last15Ratio : sort === "ddt" ? row.maxDdS : row.n;
  return [...rows].sort((a, b) => {
    if (sort !== "identity") {
      const av = metric(a), bv = metric(b);
      const aValid = av != null && Number.isFinite(av), bValid = bv != null && Number.isFinite(bv);
      if (aValid !== bValid) return aValid ? -1 : 1;
      if (aValid && bValid && av !== bv) return bv! - av!;
    }
    return a.id.localeCompare(b.id);
  });
}

export function detailsCsv(rows: SetOverviewRow[]) {
  const escape = (value: unknown) => {
    let text = value == null || (typeof value === "number" && !Number.isFinite(value)) ? "" : String(value);
    if (typeof value === "string" && /^[\s]*[=+@-]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  const headers = ["Source", "Connection", "Set ID", "Indication", "Indication config", "Strategy", "TP percent", "Side", "Samples", "Cost PF (last window)", "Max DDT seconds", "Avg DDT seconds", "Win rate percent", "Net expectancy fraction", "Avg hold seconds", "Calculation qualified", "Reason"];
  return [headers, ...rows.map((row) => [row.scope, row.connection, row.setId ?? row.id, row.indicationKind, row.indicationConfig, row.strategyType, row.tpPct, row.side, row.n, row.last15Ratio, row.maxDdS, row.avgDdS, row.wr, row.expectancy, row.avgHoldS, row.active, row.deactReason])]
    .map((row) => row.map(escape).join(",")).join("\r\n");
}
