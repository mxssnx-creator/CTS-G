/** Relation lanes (server: server/pulse/relation_lanes.py): last-N lanes of the post-Base orders by relation, next to the
 * per-configuration lanes. A passing lane raises the volume of a member Set's positive-lane entry by its ratio. */
export const RELATIONS = ["overall", "indication", "type", "range", "step"] as const;
export type Relation = (typeof RELATIONS)[number];
export const RELATION_ROLE: Record<Relation, "overall" | "major" | "minor"> = { overall: "overall", indication: "major", type: "major", range: "minor", step: "minor" };
export const RELATION_LABEL: Record<Relation, string> = {
  overall: "Overall (every indication, per direction)",
  indication: "Indication (per direction)",
  type: "Type (base / exit / trail)",
  range: "TP range (micro … wide)",
  step: "Step (target distance)",
};
export type RelationSetting = { on: boolean; ratio: number };
export type RelationLanesConfig = {
  enabled: boolean;
  relations: Record<Relation, RelationSetting>;
  mainN: number;
  realN: number;
  minPf: number;
  maxMult: number;
  fullWindow: boolean;
};
export const RELATION_RATIO_MAX = 2;
export const RELATION_MULT_MAX = 5;
/** Kept equal to position_cost.MIN_PF_FLOOR. */
export const RELATION_PF_FLOOR = 1.02;
export const DEFAULT_RELATION_LANES: RelationLanesConfig = {
  enabled: false,
  relations: { overall: { on: true, ratio: 0.25 }, indication: { on: true, ratio: 0.25 }, type: { on: true, ratio: 0.25 }, range: { on: true, ratio: 0 }, step: { on: true, ratio: 0 } },
  mainN: 25,
  realN: 15,
  minPf: 1.1,
  maxMult: 2,
  fullWindow: false,
};

function bounded(v: unknown, lo: number, hi: number, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : fallback;
}

/** The overlay value -> a complete config; unreadable parts take their defaults (the server parses the same way). */
export function relationLanesOf(raw: unknown): RelationLanesConfig {
  const d = DEFAULT_RELATION_LANES;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return structuredClone(d);
  const r = raw as Record<string, unknown>;
  const rel = r.relations && typeof r.relations === "object" && !Array.isArray(r.relations) ? (r.relations as Record<string, unknown>) : {};
  const relations = {} as Record<Relation, RelationSetting>;
  for (const name of RELATIONS) {
    const item = rel[name];
    if (item && typeof item === "object" && !Array.isArray(item)) {
      const o = item as Record<string, unknown>;
      relations[name] = { on: o.on !== false, ratio: Math.round(bounded(o.ratio, 0, RELATION_RATIO_MAX, d.relations[name].ratio) * 10000) / 10000 };
    } else if (item === false) relations[name] = { on: false, ratio: 0 };
    else relations[name] = { on: true, ratio: typeof item === "number" ? Math.round(bounded(item, 0, RELATION_RATIO_MAX, d.relations[name].ratio) * 10000) / 10000 : d.relations[name].ratio };
  }
  return {
    enabled: r.enabled === true,
    relations,
    mainN: Math.trunc(bounded(r.mainN, 1, 200, d.mainN)),
    realN: Math.trunc(bounded(r.realN, 0, 200, d.realN)),
    minPf: Math.max(RELATION_PF_FLOOR, bounded(r.minPf, 1, 100, d.minPf)),
    maxMult: bounded(r.maxMult, 1, RELATION_MULT_MAX, d.maxMult),
    fullWindow: r.fullWindow === true,
  };
}

/** The multiple an entry gets when exactly these relations pass (the server's relation_lanes.multiple). */
export function relationMultiple(cfg: RelationLanesConfig, passing: readonly Relation[]): number {
  if (!cfg.enabled) return 1;
  const add = RELATIONS.reduce((sum, name) => sum + (cfg.relations[name].on && passing.includes(name) ? cfg.relations[name].ratio : 0), 0);
  return Math.min(cfg.maxMult, 1 + add);
}
