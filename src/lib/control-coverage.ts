import type { LiveOpen } from "./live-stats.ts";

/** IDs are not proof of price/quantity coverage. Old snapshots stay unknown. */
export function controlCoverage(p: LiveOpen): "Reported covered" | "Quantity gap" | "Missing pair" | "Unverified" {
  if (p.ctrlUndercovered || (p.closePosition === false && p.ctrlQty != null && p.ctrlQty + 1e-12 < p.qty)) return "Quantity gap";
  if (!(p.slOid || p.secSlOid) || !(p.tpOid || p.secTpOid) || p.controls === false) return "Missing pair";
  const stop = p.secSl ?? p.sl, target = p.secTp ?? p.tp;
  if (![p.qty, stop, target].every(v => Number.isFinite(v) && v > 0)) return "Unverified";
  if (p.controls !== true || p.ctrlVerified !== true || p.closePosition == null ||
      (!p.closePosition && !(p.ctrlQty != null && Number.isFinite(p.ctrlQty) && p.ctrlQty >= p.qty))) return "Unverified";
  return "Reported covered";
}

export const hasControlCoverage = (p: LiveOpen) => controlCoverage(p) === "Reported covered";
