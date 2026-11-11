/** Block rounds (server: server/pulse/block_rounds.py): a group's positions form blocks of N; a losing block raises the group's next entries by
 * ratio x level of the original volume until the volume-weighted net is back at zero. Every block count 1..6 runs its own procedure. */
export const ROUND_RELATIONS = ["overall", "direction", "symbol", "indication", "type", "set", "step", "symbol+direction"] as const;
export type RoundRelation = (typeof ROUND_RELATIONS)[number];
export const ROUND_RELATION_LABEL: Record<RoundRelation, string> = {
  overall: "Overall (every position)",
  direction: "Direction",
  symbol: "Symbol",
  indication: "Indication × direction",
  type: "Indication × type × direction",
  set: "Set × lane",
  step: "Step (indication × TP step × lane × direction)",
  "symbol+direction": "Symbol × direction",
};
export const ROUND_COUNTS = [1, 2, 3, 4, 5, 6] as const;
export const ROUND_RATIO_MAX = 2;
export const ROUND_LEVEL_MAX = 6;
export const ROUND_PAUSE_MAX = 6;
export const ROUND_MULT_MAX = 5;
export type RoundCount = { on: boolean; ratio: number; step: boolean; maxLevel: number; pause: number };
export type BlockRoundsConfig = { enabled: boolean; relations: RoundRelation[]; maxMult: number; counts: Record<string, RoundCount> };

const OFF: RoundCount = { on: false, ratio: 0, step: false, maxLevel: 1, pause: 0 };
/** The desk default = what the shipped overlays carry: the one procedure that measured at or above "without" on all nine windows
 * (relation step, block count 5). The other counts form their blocks and keep their record, and raise nothing. */
export const DEFAULT_BLOCK_ROUNDS: BlockRoundsConfig = {
  enabled: true,
  relations: ["step"],
  maxMult: 3,
  counts: { "1": { ...OFF }, "2": { ...OFF }, "3": { ...OFF }, "4": { ...OFF }, "5": { on: true, ratio: 1, step: true, maxLevel: 2, pause: 0 }, "6": { ...OFF } },
};

function bounded(v: unknown, lo: number, hi: number, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : fallback;
}

/** The overlay value -> a complete config. An absent key is the desk default; inside an object the server's parsing rules apply
 * (enabled only when true, a count on only when true, unreadable numbers take the off values). */
export function blockRoundsOf(raw: unknown): BlockRoundsConfig {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return structuredClone(DEFAULT_BLOCK_ROUNDS);
  const r = raw as Record<string, unknown>;
  const picked = Array.isArray(r.relations) ? ROUND_RELATIONS.filter((name) => (r.relations as unknown[]).includes(name)) : [];
  const src = r.counts && typeof r.counts === "object" && !Array.isArray(r.counts) ? (r.counts as Record<string, unknown>) : {};
  const counts: Record<string, RoundCount> = {};
  for (const n of ROUND_COUNTS) {
    const c = src[String(n)];
    if (c && typeof c === "object" && !Array.isArray(c)) {
      const o = c as Record<string, unknown>;
      counts[String(n)] = {
        on: o.on === true,
        ratio: Math.round(bounded(o.ratio, 0, ROUND_RATIO_MAX, 0) * 10000) / 10000,
        step: o.step === true,
        maxLevel: Math.trunc(bounded(o.maxLevel, 1, ROUND_LEVEL_MAX, 1)),
        pause: Math.trunc(bounded(o.pause, 0, ROUND_PAUSE_MAX, 0)),
      };
    } else counts[String(n)] = { ...OFF };
  }
  return { enabled: r.enabled === true, relations: picked.length ? picked : ["step"], maxMult: bounded(r.maxMult, 1, ROUND_MULT_MAX, 2), counts };
}

/** The largest multiple one entry can get: 1 + every switched-on count at its top level, for every selected relation, under the ceiling. */
export function blockRoundsMax(cfg: BlockRoundsConfig): number {
  if (!cfg.enabled) return 1;
  let add = 0;
  for (const n of ROUND_COUNTS) {
    const c = cfg.counts[String(n)];
    if (c?.on && c.ratio > 0) add += c.ratio * (c.step ? c.maxLevel : 1);
  }
  return Math.min(cfg.maxMult, 1 + add * cfg.relations.length);
}
