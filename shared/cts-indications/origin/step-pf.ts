/** CTS-G manual step policy. Distances are price PERCENT, not fractions or ATR multipliers. */
export interface StepPfRules { enabled: boolean; ratio: number; max: number; byStep?: Record<string, number>; bySlRatio?: Record<string, number> }
export const DEFAULT_STEP_PF: StepPfRules = { enabled: true, ratio: 0.2, max: 2 };

/** Aliases of one numeric key cannot select different policies by insertion order. */
export function validateStepPfOverrides(rules: Partial<StepPfRules>): void {
  for (const [name, map] of [["step", rules.byStep], ["SL ratio", rules.bySlRatio]] as const) {
    if (map === undefined) continue;
    if (!map || typeof map !== "object" || Array.isArray(map)) throw new Error("Invalid step/SL PF policy");
    const seen = new Set<number>();
    for (const [key, value] of Object.entries(map)) {
      const n = Number(key);
      if (!key.trim() || !Number.isFinite(n) || (name === "step" ? !Number.isSafeInteger(n) || n < 0 : n <= 0) ||
          seen.has(n) || !Number.isFinite(value) || value <= 1 || value > 100)
        throw new Error("Invalid step/SL PF policy");
      seen.add(n);
    }
  }
}

export function stepPfFloor(legacy: number, tpPercent: number | undefined, rules?: Partial<StepPfRules>, slRatio?: number): number {
  if (rules) validateStepPfOverrides(rules);
  if (!rules?.enabled || tpPercent === undefined || !Number.isFinite(tpPercent) || tpPercent <= 0) return legacy;
  const overrides: number[] = [];
  for (const [map, key] of [[rules.byStep, String(Math.floor(tpPercent * 10 + 1e-9))], [rules.bySlRatio, slRatio == null ? "" : String(Math.round(slRatio * 1e8) / 1e8)]] as const) {
    if (!map) continue;
    const own = Object.entries(map).find(([k]) => Number(k) === Number(key) && key !== "");
    if (own) overrides.push(own[1]);
  }
  if (overrides.length) return Math.max(legacy, ...overrides);
  const ratio = typeof rules.ratio === "number" && Number.isFinite(rules.ratio)
    ? Math.max(0.1, Math.min(1.5, Math.round(rules.ratio * 20) / 20)) : DEFAULT_STEP_PF.ratio;
  const cap = typeof rules.max === "number" && Number.isFinite(rules.max)
    ? Math.max(1, Math.min(2, rules.max)) : DEFAULT_STEP_PF.max;
  const step = Math.max(0.2, Math.min(3.5, Math.floor(tpPercent * 10 + 1e-9) / 10));
  return Math.max(legacy, Math.min(cap, Math.round((1 + step * ratio) * 1e6) / 1e6));
}

export function protectStepPct(p: { tp: number; trail: number; trailOnly?: boolean }): number {
  return (p.trailOnly ? p.trail : p.tp) * 100;
}

/** The effective configured stop after cost/minimum-distance adjustment, relative to its own TP. */
export function protectSlRatio(p: { tp: number; sl: number }): number | undefined {
  return Number.isFinite(p.tp) && p.tp > 0 && Number.isFinite(p.sl) && p.sl > 0 ? p.sl / p.tp : undefined;
}
