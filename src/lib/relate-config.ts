export const RELATE_SOURCES = ["config", "overall", "symbol", "direction", "indication", "strategy", "coordination", "range"] as const;
export type RelateSource = (typeof RELATE_SOURCES)[number];
export type RelateConfig = {
  id: string; enabled: boolean; steps: number; ratio: number; mode: "shared" | "additive";
  sources: RelateSource[]; lastN: number; minTrades: number; minPf: number; maxStack: number; maxSlRatio: number;
};
export const DEFAULT_RELATE_CONFIG: RelateConfig = { id: "relate-12", enabled: false, steps: 12, ratio: .2,
  mode: "shared", sources: ["config", "symbol"], lastN: 250, minTrades: 60, minPf: 1.1, maxStack: 2, maxSlRatio: .5 };

/** Invalid policies are refused whole; silently changing a sizing policy would reuse the wrong evidence. */
export function relateConfigs(raw: unknown): RelateConfig[] {
  if (raw == null) return [structuredClone(DEFAULT_RELATE_CONFIG)];
  if (!Array.isArray(raw) || !raw.length || raw.length > 12) throw new Error("Relate requires 1–12 independent policies");
  const ids = new Set<string>();
  return raw.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error("Invalid Relate policy");
    const c = { ...DEFAULT_RELATE_CONFIG, ...item } as RelateConfig;
    if (typeof c.id !== "string" || !c.id || c.id.length > 64 || ids.has(c.id)) throw new Error("Relate IDs must be unique");
    ids.add(c.id);
    if (typeof c.enabled !== "boolean" || !["shared", "additive"].includes(c.mode)) throw new Error("Invalid Relate mode");
    for (const [key, lo, hi, integer] of [
      ["steps",2,15,true], ["lastN",60,250,true], ["minTrades",60,250,true],
      ["ratio",.2,6,false], ["minPf",1.000001,100,false], ["maxStack",1,8,false], ["maxSlRatio",.01,.5,false],
    ] as const) {
      const n = c[key];
      if (typeof n !== "number" || !Number.isFinite(n) || n < lo || n > hi || (integer && !Number.isInteger(n))) throw new Error(`Invalid Relate ${key}`);
    }
    if (c.minTrades > c.lastN || Math.abs(c.ratio*5-Math.round(c.ratio*5))>1e-9) throw new Error("Invalid Relate sample or ratio step");
    if (!Array.isArray(c.sources) || !c.sources.length || c.sources.some(s => !RELATE_SOURCES.includes(s))) throw new Error("Invalid Relate sources");
    return { ...c, sources: RELATE_SOURCES.filter(s => c.sources.includes(s)) };
  });
}
