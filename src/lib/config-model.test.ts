import test from "node:test";
import assert from "node:assert/strict";
import { overlayFromCts, type CtsSettings } from "./config-model.ts";

const cts = (extra: Record<string, unknown> = {}) => extra as unknown as CtsSettings;

test("pre-history settings retain three days and a separate collection target", () => {
  const defaults = overlayFromCts(cts());
  assert.equal(defaults.histLookbackBars, 1920);
  assert.equal(defaults.histLookbackMax, 4320);
  assert.equal(defaults.histLookbackTargetN, 12);
  assert.equal(defaults.setGateMinTrades, 0, "collection must not add an admission gate");
  const saved = overlayFromCts(cts({ histLookbackBars: 4320, histLookbackMax: 4320, histLookbackTargetN: 30 }));
  assert.equal(saved.histLookbackBars, 4320);
  assert.equal(saved.histLookbackMax, 4320);
  assert.equal(saved.histLookbackTargetN, 30);
  assert.equal(overlayFromCts(cts({ histLookbackBars: 5000, histLookbackMax: 6000 })).histLookbackBars, 4320);
  assert.equal(overlayFromCts(cts({ histLookbackMax: 6000 })).histLookbackMax, 4320);
});

test("Main long N is independently limited to 10 through 200", () => {
  assert.equal(overlayFromCts(cts({ setGateWindow: 10, mainGateN: 200 })).mainGateN, 200);
  assert.equal(overlayFromCts(cts({ mainGateN: 3 })).mainGateN, 10);
  assert.equal(overlayFromCts(cts({ mainGateN: 250 })).mainGateN, 200);
});

test("the Main short window and the Main minimal window default to the owner's settings", () => {
  const o = overlayFromCts(cts());
  assert.equal(o.mainShortEnabled, true);
  assert.deepEqual(o.mainShortWindows, [5, 10, 15]);
  assert.equal(o.mainShortMaxRatio, 0.5);
  assert.equal(o.mainShortMin, 8);
  assert.equal(o.mainShortMargin, 1.02);
  assert.equal(o.mainMinN, 0, "the Main minimal window is off by default");
  assert.equal(o.mainMinMode, "positive");
});

test("the earlier mainAuto keys still load, and the new key wins when both are given", () => {
  const legacy = overlayFromCts(cts({ mainAutoEnabled: false, mainAutoWindows: [4, 6], mainAutoMaxRatio: 0.3, mainAutoMin: 5, mainAutoMargin: 1.1 }));
  assert.equal(legacy.mainShortEnabled, false);
  assert.deepEqual(legacy.mainShortWindows, [4, 6]);
  assert.equal(legacy.mainShortMaxRatio, 0.3);
  assert.equal(legacy.mainShortMin, 5);
  assert.equal(legacy.mainShortMargin, 1.1);
  const both = overlayFromCts(cts({ mainShortEnabled: true, mainAutoEnabled: false, mainShortWindows: [5], mainAutoWindows: [9] }));
  assert.equal(both.mainShortEnabled, true);
  assert.deepEqual(both.mainShortWindows, [5]);
});

test("the Main minimal window is held to 0..3 and to a mode of positive or negative", () => {
  assert.equal(overlayFromCts(cts({ mainMinN: 9 })).mainMinN, 3);
  assert.equal(overlayFromCts(cts({ mainMinN: -2 })).mainMinN, 0);
  assert.equal(overlayFromCts(cts({ mainMinN: 2, mainMinMode: "NEGATIVE" })).mainMinMode, "negative");
  assert.equal(overlayFromCts(cts({ mainMinMode: "sideways" })).mainMinMode, "positive");
  assert.equal(overlayFromCts(cts({ mainMinN: 2, mainMinMode: "Both" })).mainMinMode, "both");
  assert.equal(overlayFromCts(cts({ setGateMinN: 2, setGateMinMode: "both" })).setGateMinMode, "both");
  assert.equal(overlayFromCts(cts({ setGateMinN: 2, setGateMinMode: "negative" })).setGateMinMode, "negative", "the Base minimal window reads the same way");
  assert.equal(overlayFromCts(cts({ setGateMinN: 2, setGateMinMode: "AvgNegative" })).setGateMinMode, "avgnegative", "the average rule loads");
  assert.equal(overlayFromCts(cts({ mainMinN: 3, mainMinMode: "avgboth" })).mainMinMode, "avgboth");
  assert.equal(overlayFromCts(cts({ mainMinN: 1, mainMinMode: "avgpositive" })).mainMinMode, "avgpositive");
});
