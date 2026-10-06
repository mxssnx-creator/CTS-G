/**
 * Engine stage window defaults (server/pulse set_engine PF_N_DEFAULT and the
 * Main / Real eval counts). One place so fallbacks never drift to a stale 15.
 */
export const BASE_EVAL_DEFAULT = 50;
export const MAIN_EVAL_DEFAULT = 30;
export const REAL_EVAL_DEFAULT = 30;
/** position_cost.EVALUATION_WINDOWS */
export const EVALUATION_WINDOWS = [5, 10, 15, 25, 30, 50, 75] as const;

/**
 * `lastN` keys the payload actually carries, numerically ordered; falls back
 * to the engine's EVALUATION_WINDOWS when the payload has none.
 */
export function evaluationWindowKeys(windows: Record<string, unknown> | null | undefined): string[] {
  const keys = Object.keys(windows || {}).filter((key) => /^last\d+$/.test(key));
  const source = keys.length ? keys : EVALUATION_WINDOWS.map((n) => `last${n}`);
  return [...new Set(source)].sort((a, b) => Number(a.slice(4)) - Number(b.slice(4)));
}
