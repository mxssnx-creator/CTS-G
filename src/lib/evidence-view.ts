import type { CostModel, PfEvidence } from "./live-stats.ts";

export function evidencePfText(value: (PfEvidence & { pf?: number | null }) | null | undefined, count?: number) {
  if (!value || count === 0 || value.pfState === "empty") return "— (no sample)";
  if (value.pfState === "no-loss") return "No observed losses";
  if (value.pfState === "flat") return "Flat (PF undefined)";
  if (value.pfState === "finite" && value.pfFinite != null && Number.isFinite(value.pfFinite)) return value.pfFinite.toFixed(4);
  if (value.pf != null && Number.isFinite(value.pf) && value.pf >= 0 && value.pf !== 99 && count != null && count > 0) return `${value.pf.toFixed(4)} (legacy)`;
  return "— (evidence unavailable)";
}

export function feeRateText(rate: number | null | undefined) {
  return rate != null && Number.isFinite(rate) ? `${(100 * rate).toFixed(4)}%` : "Unknown";
}

export function feeRows(model: CostModel | null | undefined) {
  return Object.entries(model?.symbols ?? {}).sort(([a], [b]) => a.localeCompare(b))
    .map(([symbol, row]) => ({ symbol, maker: feeRateText(row.makerRate), taker: feeRateText(row.takerRate), source: row.source }));
}
