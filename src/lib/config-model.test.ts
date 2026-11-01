import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { defaultOverlay, overlayFromCts, syncOverlayFlags, PF_TYPE_DEFAULTS, type CtsSettings } from "./config-model.ts";
import { secureMinimalPreset } from "./presets.ts";

const cts = (extra: Record<string, unknown> = {}) => extra as unknown as CtsSettings;

test("Normal admission is independent and a raw trailing module disable survives a settings save", () => {
  const loaded = overlayFromCts(cts(), { stratNormal: false, stratTrailing: true,
    modules: { "strategy.trailing": false } });
  assert.equal(loaded.stratNormal, false);
  assert.equal(loaded.stratGeneral, true);
  assert.equal(loaded.stratTrailing, false);
  const saved = syncOverlayFlags(loaded);
  assert.equal(saved.modules?.["strategy.trailing"], false);
  assert.equal(saved.modules?.["strategy.trailRecalc"], false);
  const enabled = syncOverlayFlags({ ...loaded, stratTrailing: true });
  assert.equal(enabled.modules?.["strategy.trailing"], true, "explicit operator toggle can enable it again");
});

test("Secure, Minimal is an isolated, bounded 50-symbol preset", () => {
  const preset = secureMinimalPreset();
  const saved = JSON.parse(readFileSync(new URL("../../presets/secure-minimal.json", import.meta.url), "utf8"));
  assert.deepEqual(preset, saved, "saved preset and Settings must stay identical");
  assert.equal(preset.symbolCap, 50);
  assert.equal(preset.symbolsAll && preset.symbolsDynamic, true);
  assert.equal(preset.symbolSort, "quoteVolume");
  assert.deepEqual([preset.useMaxLeverage, preset.leverage, preset.maxOpen], [false, 1, 2]);
  assert.deepEqual([preset.strictSizeCap, preset.sizePctOfBalance, preset.sizeMaxNotional], [true, 2, 5]);
  assert.deepEqual([preset.maxModeledStopLossPct, preset.ddHaltPct], [3, 3]);
  assert.deepEqual([preset.blockEnabled, preset.stratBlock, preset.dcaEnabled, preset.stratDca], [false, false, false, false]);
  assert.deepEqual([preset.setMinStep, preset.setStepMax, preset.shortRangeStepMin, preset.shortRangeStepMax], [2, 8, 2, 6]);
  assert.deepEqual([preset.histLookbackBars, preset.histLookbackAuto, preset.setGateMinTrades, preset.stageFullWindow], [720, false, 12, true]);
  assert.equal(preset.mainNegEnabled, false, "the unverified inverse lane cannot bypass Main in this preset");
  for (const key of Object.keys(PF_TYPE_DEFAULTS) as (keyof typeof PF_TYPE_DEFAULTS)[]) assert.equal(preset[key], PF_TYPE_DEFAULTS[key]);
  preset.symbols.push("TEST-USDT");
  assert.equal(secureMinimalPreset().symbols.includes("TEST-USDT"), false);
});

test("PF defaults, saved overrides and an independent reset agree", () => {
  const defaults = overlayFromCts(cts());
  assert.deepEqual(
    Object.fromEntries(Object.keys(PF_TYPE_DEFAULTS).map((key) => [key, defaults[key as keyof typeof PF_TYPE_DEFAULTS]])),
    PF_TYPE_DEFAULTS,
  );
  const saved = overlayFromCts(cts({ minPfShort: 1.4, minPfSignals: 1.6 }), { minPfMinimal: 1.2 });
  assert.equal(saved.minPfShort, 1.4);
  assert.equal(saved.minPfMinimal, 1.2);
  assert.equal(saved.minPfSignals, 2.2, "legacy Signal floors are raised to the owner's minimum");
  assert.equal(overlayFromCts(cts({}), { minPfSignals: 1.3 }).minPfSignals, 2.2);
  assert.equal(syncOverlayFlags({ ...saved, minPfSignals: 1.3 }).minPfSignals, 2.2);
  assert.equal(syncOverlayFlags({ ...saved, minPfSignals: 2.7 }).minPfSignals, 2.7);
  const bounds = overlayFromCts(cts(), { minPfMicroN: 9, minPfMinimalN: 3, minPfShortN: 4, minPfMediumN: 7 });
  assert.deepEqual([bounds.minPfMicroN, bounds.minPfMinimalN, bounds.minPfShortN, bounds.minPfMediumN], [9, 9, 9, 9]);
  const reset = defaultOverlay();
  reset.minPfSignals = 2;
  reset.symbols.push("TEST-USDT");
  assert.equal(defaultOverlay().minPfSignals, 2.2);
  assert.equal(defaultOverlay().symbols.includes("TEST-USDT"), false);
});

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
