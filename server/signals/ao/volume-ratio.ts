/** Common external sizing policy; never used to change unit returns or qualify a strategy. */
export const VOLUME_RATIO_DEFAULT = 1;
export function volumeRatio(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.round(Math.min(5, Math.max(0.1, value)) * 10) / 10
    : VOLUME_RATIO_DEFAULT;
}
export function validVolumeRatio(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0.1 && value <= 5 &&
    Math.abs(value * 10 - Math.round(value * 10)) < 1e-8;
}
