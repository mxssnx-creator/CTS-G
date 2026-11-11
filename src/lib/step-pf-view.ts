import { manualStepPf, STEP_PF_TP_MAX, TP_UNIT_PCT, TP_STEP_MIN, TP_STEP_MAX, TP_RANGES, type PulseOverlay } from "./config-model.ts";
import type { LiveStats } from "./live-stats.ts";

type LearnedStep = NonNullable<NonNullable<NonNullable<LiveStats["sets"]>["stepPf"]>["steps"]>[number];
/** Shared step-table defaults. Exact config policy overrides are evaluated by their own lanes. */
export function stepPfRows(overlay: PulseOverlay, learned: LearnedStep[] = []) {
  const byKey = new Map(learned.map(row => [`${row.step}:${row.side ?? 0}`, row]));
  const sides = overlay.directionMode === "split" ? [1, -1] : [0];
  const rows = [];
  for (let step = TP_STEP_MIN; step <= TP_STEP_MAX; step++) {
    const tp = Math.round(step * TP_UNIT_PCT * 1000) / 1000;
    const bucket = Math.min(step, Math.round(STEP_PF_TP_MAX / TP_UNIT_PCT));
    const manual = manualStepPf(tp, overlay.minPfStepRatio);
    const range = TP_RANGES.find(range => step >= range.lo && step <= range.hi);
    for (const side of sides) {
      const evidence = byKey.get(`${bucket}:${side}`);
      const rangeFloor = range && overlay.minPfByRange?.[range.id];
      const source = rangeFloor != null ? "Range override" : overlay.minPfStepAuto !== false && evidence?.auto != null ? "Auto" : "Manual";
      const used = rangeFloor ?? Math.min(overlay.minPfStepAutoHigh,
        overlay.minPfStepAuto !== false && evidence?.auto != null ? evidence.auto : manual);
      rows.push({ step, tp, bucket, side, manual, used, source, evidence });
    }
  }
  return rows;
}
