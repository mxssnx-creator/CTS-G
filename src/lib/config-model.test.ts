import test from "node:test";
import assert from "node:assert/strict";
import { overlayFromCts, type CtsSettings } from "./config-model.ts";

const cts = (extra: Record<string, unknown> = {}) => extra as unknown as CtsSettings;

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
});
