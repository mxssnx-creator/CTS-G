import type { PfBase } from "./live-stats.ts";

/** Older engine snapshots use 99 as a no-loss sentinel, so >=99 cannot be
 * distinguished from a very large finite PF without gain/loss provenance. */
export function historicalPlotPf(pf: number | undefined, n: number | undefined): number | null {
  return n != null && n > 0 && pf != null && Number.isFinite(pf) && pf < 99 ? pf : null;
}

export function basePackFigures(packs: PfBase["byPack"]) {
  return Object.entries(packs ?? {}).map(([id, v]) => ({
    id,
    effective: historicalPlotPf(v.baseOnly?.pf, v.baseOnly?.n),
    unfiltered: historicalPlotPf(v.unfiltered.pf, v.unfiltered.n),
    validated: v.validatedBasePct ?? null,
    baseValid: v.baseOnly?.sets ?? null,
    allStageValid: v.valid,
    total: v.total,
  }));
}
