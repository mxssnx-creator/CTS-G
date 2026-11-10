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

export type StageProgressPoint = { time: string; base: number | null; main: number | null; real: number | null; realNeg: number | null; relations: number | null };

/** Per scoring pass: the share (percent of all Sets) passing each stage, and the share of the relation lanes that pass.
 * A point recorded before the per-stage figures existed has no stage value (null, a gap), never a zero. */
export function stageProgress(history: PfBase["history"]): StageProgressPoint[] {
  return (history ?? []).map((h) => {
    const pct = (k: string) => (h.stages?.[k] && Number.isFinite(h.stages[k].pct) ? h.stages[k].pct : null);
    const rel = h.relations;
    return {
      time: new Date(h.t * 1000).toISOString().slice(11, 16),
      base: pct("base"), main: pct("main"), real: pct("real"), realNeg: pct("realNeg"),
      relations: rel && rel.lanes > 0 ? Math.round((10000 * rel.passing) / rel.lanes) / 100 : null,
    };
  });
}
