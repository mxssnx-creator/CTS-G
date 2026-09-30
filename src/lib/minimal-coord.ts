/**
 * Minimal Coord.
 *
 * The short range and the minimal range, kept apart from the wide protect grid.
 * A cell is one take-profit, one stop multiple, one trail and one hold.
 * Trailing cells force the stop to at least `trailSlOfTp` times the target,
 * so the stop stays further out than the trail. Distances are fractions of price.
 */

export const MINIMAL_COORD = "Minimal Coord.";

/** Round-trip cost this range is built on: 0.1% per side. */
export const COORD_COST = 0.002;

export interface CoordRange {
  /** Take-profit distances, fractions of price. */
  tp: readonly number[];
  /** Stop as a multiple of the take-profit. */
  slOfTp: readonly number[];
  /** Trail as a fraction of the take-profit. 0 keeps the target and does not trail. */
  trailOfTp: readonly number[];
  /** Trailing cells use at least this stop multiple. */
  trailSlOfTp: number;
  /** Stop is never closer than this. */
  minSl: number;
  /** Trail is never closer than this. */
  minTrail: number;
}

export interface CoordProtect {
  tp: number;
  sl: number;
  trail: number;
  hold: number;
  trailStep?: number;
  trailFree?: boolean;
}

export interface CoordGrid {
  holdH: readonly number[];
  minSl: number;
  minTrail: number;
  trailStep?: number;
  trailFree?: boolean;
  short?: CoordRange | false;
  minimal?: CoordRange | false;
}

/** 1×–3× cost, under the short range. Stops 1–2×, two trails. */
export const MINIMAL_RANGE: CoordRange = {
  tp: [2, 2.25, 2.5, 2.75, 3].map((n) => +(COORD_COST * n).toFixed(4)),
  slOfTp: [1, 1.25, 1.5, 1.75, 2],
  trailOfTp: [0, 0.75],
  trailSlOfTp: 2,
  minSl: +COORD_COST.toFixed(4),
  minTrail: +(COORD_COST * 0.5).toFixed(4),
};

/** 3×–6× cost (0.6–1.2%). Stops 1–3× in 0.25 steps. */
export const SHORT_RANGE: CoordRange = {
  tp: [3, 4, 5, 6].map((n) => +(COORD_COST * n).toFixed(4)),
  slOfTp: Array.from({ length: 9 }, (_, i) => +(1 + i * 0.25).toFixed(2)),
  trailOfTp: [0, 0.5, 0.75],
  trailSlOfTp: 2,
  minSl: +(COORD_COST * 3).toFixed(4),
  minTrail: +COORD_COST.toFixed(4),
};

const round4 = (x: number) => +x.toFixed(4);

/** How many cells a range adds before identical distances are collapsed. */
export function coordVariants(holdN: number, range: CoordRange | false | undefined): number {
  if (!range) return 0;
  const hold = holdN || 1;
  return range.tp.length * range.slOfTp.length * range.trailOfTp.length * hold;
}

export function coordVariantTotal(g: CoordGrid): number {
  const hold = g.holdH.length || 1;
  return coordVariants(hold, g.short) + coordVariants(hold, g.minimal);
}

/**
 * Protects of the short range and the minimal range only.
 * The wide grid is not built here. Same cell rule the engine uses:
 * stop = max(minSl, tp × ratio), trail = max(minTrail, tp × trail) or 0,
 * and a trailing cell's stop ratio is at least trailSlOfTp.
 */
export function coordProtects(tfMin: number, g: CoordGrid): CoordProtect[] {
  const out: CoordProtect[] = [];
  const seen = new Set<string>();
  const ranges = [g.short, g.minimal];
  for (const range of ranges) {
    if (!range) continue;
    const minSl = range.minSl ?? g.minSl;
    const minTrail = range.minTrail ?? g.minTrail;
    const trailStop = range.trailSlOfTp ?? 2;
    for (const tp of range.tp) {
      for (const k of range.slOfTp) {
        for (const tr of range.trailOfTp) {
          for (const h of g.holdH) {
            const ratio = tr > 0 ? Math.max(k, trailStop) : k;
            const p: CoordProtect = {
              tp,
              sl: round4(Math.max(minSl, tp * ratio)),
              trail: tr > 0 ? round4(Math.max(minTrail, tp * tr)) : 0,
              hold: Math.max(2, Math.round((h * 60) / tfMin)),
            };
            if (p.trail > 0) {
              p.trailStep = g.trailStep ?? 1;
              p.trailFree = g.trailFree ?? false;
            }
            const key = `${p.tp}|${p.sl}|${p.trail}|${p.hold}`;
            if (seen.has(key)) continue;
            seen.add(key);
            out.push(p);
          }
        }
      }
    }
  }
  return out;
}

/** Which named range an unscaled take-profit belongs to. `tp` is a fraction of price. */
export function rangeOfTp(tp: number): "minimal" | "short" | "wide" {
  if (tp <= MINIMAL_RANGE.tp[MINIMAL_RANGE.tp.length - 1] + 1e-6) return "minimal";
  if (tp <= SHORT_RANGE.tp[SHORT_RANGE.tp.length - 1] + 1e-6) return "short";
  return "wide";
}
