/** Conventional profit factor: equal positive gross gains and losses are neutral at one. */
export const PF_NEUTRAL = 1;

/**
 * No absolute loss cutoff: PF is dimensionless and must not change with the return unit.
 * Empty/flat totals keep the internal zero score; callers must use counts/provenance to display
 * them as unavailable/flat, never as a measured losing ratio. No-loss is an explicit convention,
 * not a finite measured ratio. Overflow and unknown amounts cannot qualify a set.
 */
/** @param {number} gp @param {number} gl @param {number} [noLoss] */
export function profitFactorRatio(gp, gl, noLoss = Infinity) {
  if (!(Number.isFinite(gp) && Number.isFinite(gl) && gp >= 0 && gl >= 0)) return NaN;
  if (gl === 0) return gp > 0 ? noLoss : 0;
  const pf = gp / gl;
  return Number.isFinite(pf) ? pf : NaN;
}
