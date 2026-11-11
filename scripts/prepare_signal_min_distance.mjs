// Prepare a bounded, offline Signals sensitivity study. This file does not
// import the exchange client, contact a venue, or alter any saved desk.
// Freeze all four arms with CTS-A-O's freeze-session-study before scoring.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const [baseArg, outArg] = process.argv.slice(2);
if (!baseArg || !outArg) throw new Error("usage: node prepare_signal_min_distance.mjs BASE_DESK.json NEW_OUTPUT_DIR");
const bytes = readFileSync(resolve(baseArg));
const base = JSON.parse(bytes.toString("utf8"));
const signal = base.settings?.signals;
if (!signal?.enabled || signal.guard?.lastN !== 12 || base.wf?.signalValidLastN !== 12 ||
    signal.minTrail !== 0.002 || base.settings.cost !== 0.0015 ||
    JSON.stringify(signal.normal?.tp) !== "[0.01]" || JSON.stringify(signal.trailing?.tp) !== "[0.01]")
  throw new Error("unexpected baseline: refuse to generate a mislabeled comparison");
const dir = resolve(outArg);
mkdirSync(dir); // Existing attempts are never overwritten.
const arms = [
  { id: "control", n8: false, distance: false },
  { id: "last8", n8: true, distance: false },
  { id: "distance", n8: false, distance: true },
  { id: "last8-distance", n8: true, distance: true },
];
for (const arm of arms) {
  const desk = structuredClone(base);
  if (arm.n8) {
    desk.settings.signals.guard.lastN = 8;
    desk.wf.signalValidLastN = 8;
    if (desk.wf.signalGuardN !== undefined) desk.wf.signalGuardN = 8;
    if (desk.wf.rangeOpts?.sig?.validLastN !== undefined) desk.wf.rangeOpts.sig.validLastN = 8;
  }
  if (arm.distance) {
    desk.settings.signals.minTrail = 0.003;
    desk.settings.signals.normal.tp = [0.01, 0.015];
    desk.settings.signals.trailing.tp = [0.01, 0.015];
  }
  desk.study = { arm: arm.id, source: "CTS-A-O Signals", historyHours: 12, tradingHours: 12, symbols: 8,
    lastN: arm.n8 ? 8 : 12, minTrail: arm.distance ? 0.003 : 0.002,
    normalTp: arm.distance ? [0.01, 0.015] : [0.01], trailingTp: arm.distance ? [0.01, 0.015] : [0.01],
    cost: 0.0015, status: "research-only, retrospective; do not deploy or select on this reused window" };
  writeFileSync(resolve(dir, `${arm.id}.json`), JSON.stringify(desk, null, 2) + "\n", { flag: "wx" });
}
const plan = { schema: "signal-min-distance-12h12h-v1", baselineSha256: createHash("sha256").update(bytes).digest("hex"),
  prehistoryHours: 12, tradingHours: 12, symbols: 8, arms,
  policy: "Same source/feed/cost/account/risk gates. Separate Last-N and distance changes; no live settings or orders. Preserve insufficient, losing and held cohorts; this previously inspected window is not a holdout." };
writeFileSync(resolve(dir, "plan.json"), JSON.stringify(plan, null, 2) + "\n", { flag: "wx" });
console.log(JSON.stringify({ dir, arms: arms.map(a => a.id), baselineSha256: plan.baselineSha256 }));
