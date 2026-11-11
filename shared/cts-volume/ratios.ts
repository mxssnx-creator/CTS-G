/** Relative execution weights. Neutral defaults preserve the existing economic basis exactly. */
export const VOLUME_BUCKETS = [
  "indicationTypes",
  "indications",
  "strategies",
  "ranges",
  "symbols",
] as const;
export type VolumeBucket = (typeof VOLUME_BUCKETS)[number];
export interface RelationalVolumeRatios {
  enabled: boolean;
  overall: number;
  maxMultiplier: number;
  indicationTypes: Record<string, number>;
  indications: Record<string, number>;
  strategies: Record<string, number>;
  ranges: Record<string, number>;
  symbols: Record<string, number>;
}
export const DEFAULT_RELATIONAL_VOLUME: RelationalVolumeRatios = {
  enabled: true,
  overall: 1,
  maxMultiplier: 25,
  indicationTypes: {},
  indications: {},
  strategies: {},
  ranges: {},
  symbols: {},
};
const plain = (v: unknown): v is Record<string, unknown> =>
  v !== null &&
  typeof v === "object" &&
  !Array.isArray(v) &&
  [Object.prototype, null].includes(Object.getPrototypeOf(v));
const keyOf = (key: string) => key.trim().toLowerCase();
const safeKey = (key: string) =>
  key.length > 0 &&
  key.length <= 200 &&
  !["__proto__", "prototype", "constructor"].includes(key) &&
  /^[a-z0-9_.:@|/#+~-]+$/.test(key);
export function relationalVolumeSettings(raw: unknown, strict = false): RelationalVolumeRatios {
  const out = structuredClone(DEFAULT_RELATIONAL_VOLUME);
  const invalid = (message: string) => {
    if (strict) throw new Error(message);
  };
  if (raw === undefined || raw === null) return out;
  if (typeof raw === "string") {
    try {
      raw = JSON.parse(raw);
    } catch {
      invalid("Invalid relational volume JSON");
      return out;
    }
  }
  if (!plain(raw)) {
    invalid("Relational volume must be an object");
    return out;
  }
  const allowed = new Set<string>(["enabled", "overall", "maxMultiplier", ...VOLUME_BUCKETS]);
  if (Object.keys(raw).some((k) => !allowed.has(k))) invalid("Unknown relational volume setting");
  if (raw.enabled !== undefined) {
    if (typeof raw.enabled === "boolean") out.enabled = raw.enabled;
    else invalid("Relational volume enabled must be boolean");
  }
  const ratio = (value: unknown, max = 5, min = 0.1) =>
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= min &&
    value <= max &&
    Math.abs(value * 10 - Math.round(value * 10)) < 1e-8;
  for (const field of ["overall", "maxMultiplier"] as const)
    if (raw[field] !== undefined) {
      if (
        ratio(raw[field], field === "maxMultiplier" ? 25 : 5, field === "maxMultiplier" ? 1 : 0.1)
      )
        out[field] = raw[field] as number;
      else invalid("Invalid " + field + " ratio");
    }
  for (const bucket of VOLUME_BUCKETS) {
    if (raw[bucket] === undefined) continue;
    const items = raw[bucket];
    if (!plain(items) || Object.keys(items).length > 4096) {
      invalid("Invalid " + bucket + " volume ratios");
      continue;
    }
    for (const [rawKey, value] of Object.entries(items)) {
      const key = keyOf(rawKey);
      if (!safeKey(key) || !ratio(value) || Object.hasOwn(out[bucket], key)) {
        invalid("Invalid or duplicate " + bucket + " volume ratio");
        continue;
      }
      out[bucket][key] = value as number;
    }
  }
  return out;
}
export interface VolumeContext {
  indicationType?: string | readonly string[];
  indication?: string;
  strategy?: string | readonly string[];
  range?: string | readonly string[];
  symbol?: string;
}
/** Apply once to entry notional. Held quantities, unit returns and qualification windows remain immutable. */
export function relationalVolumeFactor(settings: unknown, context: VolumeContext): number {
  if (settings === undefined || settings === null) return 1;
  const s = relationalVolumeSettings(settings);
  if (!s.enabled) return 1;
  const bindings: Record<VolumeBucket, string | readonly string[] | undefined> = {
    indicationTypes: context.indicationType,
    indications: context.indication,
    strategies: context.strategy,
    ranges: context.range,
    symbols: context.symbol,
  };
  let factor = s.overall;
  for (const bucket of VOLUME_BUCKETS) {
    const value = bindings[bucket];
    if (value !== undefined)
      for (const key of new Set((Array.isArray(value) ? value : [value]).map(keyOf)))
        factor *= s[bucket][key] ?? 1;
  }
  return Math.min(s.maxMultiplier, Math.max(0.000001, factor));
}

/** An additional strategy leg inherits source/range sizing from its parent; only its own extra weight is new. */
export function strategyVolumeWeight(raw: unknown, strategy: string): number {
  if (raw === undefined || raw === null) return 1;
  const s = relationalVolumeSettings(raw);
  return s.enabled ? (s.strategies[strategy.trim().toLowerCase()] ?? 1) : 1;
}
