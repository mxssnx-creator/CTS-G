import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { indicationConfigList, defaultOverlay, manualStepPf, overlayFromCts, pfRangeFloorsOf, stepPfRatio, syncOverlayFlags, PF_TYPE_DEFAULTS, type CtsSettings } from "./config-model.ts";
import { secureMinimalPreset } from "./presets.ts";

const cts = (extra: Record<string, unknown> = {}) => extra as unknown as CtsSettings;

test("global and per-range Last-N retain the lowest requested windows", () => {
  for (const n of [1,2,3,5,8,10,200]) {
    const loaded=overlayFromCts(cts({setGateWindow:n,mainGateN:n,lastNByRange:{micro:{baseLong:n,mainLong:n}}}));
    assert.equal(loaded.setGateWindow,n);
    assert.equal(loaded.mainGateN,n);
    assert.deepEqual(loaded.lastNByRange.micro,{baseLong:n,mainLong:n});
    const saved=overlayFromCts(cts(),syncOverlayFlags(loaded));
    assert.equal(saved.setGateWindow,n);
    assert.equal(saved.lastNByRange.micro.baseLong,n);
  }
});

test("Micro range PF survives loading and saving independently of window-size floors", () => {
  assert.deepEqual(defaultOverlay().minPfByRange, { micro: 1.01 });
  const loaded = overlayFromCts(cts({ minPfByRange: { micro: 1.001, short: 1.2 } }));
  assert.deepEqual(loaded.minPfByRange, { micro: 1.001, short: 1.2 });
  assert.equal(loaded.minPfMicro, PF_TYPE_DEFAULTS.minPfMicro);
  const saved = syncOverlayFlags(loaded);
  assert.deepEqual(overlayFromCts(cts(), saved).minPfByRange, loaded.minPfByRange);
  assert.deepEqual(overlayFromCts(cts(), { minPfByRange: {} }).minPfByRange, {}, "explicit disable survives reload");
  assert.deepEqual(pfRangeFloorsOf({ Micro: "1.001", wide: Infinity, short: 9, normal: null, bogus: 1.3, long: "bad" }),
    { micro: 1.001, short: 2 });
});

test("SL grids normalize legacy ratios once and empty grids retain the ten defaults", () => {
  const all = Array.from({length:10},(_,i)=>(i+1)/4);
  for (const v of [undefined, [], [NaN, Infinity, "wrong"]]) assert.deepEqual(overlayFromCts(cts({setSlRatios:v})).setSlRatios, all);
  assert.deepEqual(overlayFromCts(cts({setSlRatios:[.1,.3,.6,1,3,5,1]})).setSlRatios,[.25,.5,1,2.5]);
});

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
  assert.equal(defaults.histLookbackBars, 4320, "three days of pre-history by default");
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

test("Main long N is independently limited to 1 through 200", () => {
  assert.equal(overlayFromCts(cts({ setGateWindow: 10, mainGateN: 200 })).mainGateN, 200);
  assert.equal(overlayFromCts(cts({ mainGateN: 3 })).mainGateN, 3);
  assert.equal(overlayFromCts(cts({ mainGateN: 250 })).mainGateN, 200);
});

test("the Main short window and the Main minimal window default to the owner's settings", () => {
  const o = overlayFromCts(cts());
  assert.equal(o.mainShortEnabled, true);
  assert.deepEqual(o.mainShortWindows, [5, 10, 15]);
  assert.equal(o.mainShortMaxRatio, 0.5);
  assert.equal(o.mainShortMin, 1, "no minimum-orders gate (owner 2026-11-04)");
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

test("the Main minimal window is held to 0..5 and to a mode of positive or negative", () => {
  assert.equal(overlayFromCts(cts({ mainMinN: 9 })).mainMinN, 5);
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

test("pause after success: mode, minutes, candidates and per-indication minutes are bounded and survive a settings load", () => {
  const loaded = overlayFromCts(
    cts({ pauseMode: "auto", pauseFixedMin: 9999, pauseCandidates: "30, 5; 5, 0, -3, 9999", pauseAutoMax: 5, pauseAutoMin: 2, pausePack: { general: 15, bad: -4, z: "q" } }),
  );
  assert.equal(loaded.pauseMode, "auto");
  assert.equal(loaded.pauseFixedMin, 240, "a fixed pause is at most four hours");
  assert.deepEqual(loaded.pauseCandidates, [5, 30, 240], "candidates are unique whole minutes, ascending, 1-240");
  assert.equal(loaded.pauseAutoMax, 10);
  assert.equal(loaded.pauseAutoMin, 8, "the sample floor the engine applies (MIN_VALIDATION_N)");
  assert.deepEqual(loaded.pausePack, { general: 15 });
  assert.equal(overlayFromCts(cts({ pauseMode: "sideways" })).pauseMode, "auto", "an unknown mode falls back to the default, auto");
  assert.equal(overlayFromCts(cts()).pauseMode, "auto", "auto is the default");
  assert.equal(overlayFromCts(cts({ pauseMode: "off" })).pauseMode, "off", "off stays selectable");
  assert.deepEqual(overlayFromCts(cts()).pauseCandidates, [2, 3, 5, 8, 12, 20, 30, 45, 60]);
});

test("indication range configurations are normalised the way the engine expands them", () => {
  const out = overlayFromCts(
    cts({
      indicationConfigs: [
        { label: "fast", families: "donch, kelt", ranges: "8, 99, 14", timeframes: [1, 7, "c", "x"], relations: ["", "T5"], stepMin: 12, stepMax: 3, slRatios: [1, 9], types: ["base", "nope"], pause: 15 },
        { enabled: false, pause: "auto" },
        "junk",
      ],
    }),
  ).indicationConfigs;
  assert.equal(out.length, 3);
  assert.deepEqual(out[0].families, ["donch", "kelt"]);
  assert.deepEqual(out[0].ranges, [8, 14], "ranges outside 2-60 are dropped");
  assert.deepEqual(out[0].timeframes, [1, "c", "x"], "only 1/5/15/30 minutes and the combined votes");
  assert.deepEqual(out[0].relations, ["", "T5"]);
  assert.ok(out[0].stepMax >= out[0].stepMin, "the step range is ordered");
  assert.deepEqual(out[0].slRatios, [1], "SL ratios outside 0.5-3.5 are dropped");
  assert.deepEqual(out[0].types, ["base"]);
  assert.equal(out[0].pause, 15);
  assert.equal(out[1].enabled, false);
  assert.equal(out[1].pause, "auto");
  assert.equal(out[2].label, "entry 3", "an unreadable entry becomes the default entry");
  assert.deepEqual(overlayFromCts(cts()).indicationConfigs, []);
});

test("the desk pause defaults equal the engine's", () => {
  const py = readFileSync(new URL("../../server/pulse/set_engine.py", import.meta.url), "utf8");
  const cands = /PAUSE_CANDIDATES_DEFAULT = \(([0-9, ]+)\)/.exec(py)?.[1].split(",").map((x) => Number(x.trim()));
  const max = Number(/PAUSE_MAX = (\d+)/.exec(py)?.[1]);
  const o = overlayFromCts(cts());
  assert.deepEqual(o.pauseCandidates, cands);
  assert.equal(o.pauseFixedMin <= max, true);
  assert.equal(overlayFromCts(cts({ pauseFixedMin: 1e6 })).pauseFixedMin, max);
  assert.equal(Number(/PAUSE_AUTO_MAX_DEFAULT = (\d+)/.exec(py)?.[1]), o.pauseAutoMax);
  assert.equal(Number(/PAUSE_AUTO_MIN_DEFAULT = (\d+)/.exec(py)?.[1]), o.pauseAutoMin);
});

test("the manual floor per TP step is 1 + TP% x ratio on the 0.1% grid rounded down, with the engine's numbers", () => {
  // the same table as scripts/regression/step_pf.py
  const table: Array<[number, number, number]> = [
    [0.2, 0.2, 1.04], [0.25, 0.2, 1.04], [0.29, 0.2, 1.04], [0.3, 0.2, 1.06], [3.2, 0.2, 1.64], [3.5, 0.2, 1.7], [9.0, 0.2, 1.7], [5.0, 0.2, 1.7], [4.2, 0.2, 1.7], [1.5, 1.5, 3.25],
    [0.1, 0.2, 1.04], [0.2, 1.1, 1.22], [3.5, 1.1, 4.85], [1.0, 0.1, 1.1], [0.5, 0.2, 1.1],
  ];
  for (const [tp, ratio, want] of table) assert.equal(manualStepPf(tp, ratio), want, `${tp}% at ratio ${ratio}`);
  const snaps: Array<[unknown, number]> = [[0.05, 0.1], [0.1, 0.1], [0.2, 0.2], [0.33, 0.35], [1.1, 1.1], [1.5, 1.5], [1.8, 1.5], ["x", 0.2], [null, 0.2], [Number.NaN, 0.2], [Infinity, 0.2]];
  for (const [raw, want] of snaps) assert.equal(stepPfRatio(raw), want, `ratio ${String(raw)}`);
});

test("the TP-step floor settings are on by default, bounded, and survive a settings load", () => {
  const d = overlayFromCts(cts());
  assert.deepEqual([d.minPfByStep, d.minPfStepAuto, d.minPfStepRatio, d.minPfStepAutoN, d.minPfStepAutoMin], [true, true, 0.2, 10, 1]);
  const o = overlayFromCts(cts({ minPfByStep: false, minPfStepAuto: false, minPfStepRatio: 0.45, minPfStepAutoN: 12, minPfStepAutoMin: 500 }));
  assert.deepEqual([o.minPfByStep, o.minPfStepAuto, o.minPfStepRatio, o.minPfStepAutoN, o.minPfStepAutoMin], [false, false, 0.45, 12, 500]);
  assert.equal(d.rankByLane, true);
  assert.equal(overlayFromCts(cts({ rankByLane: false })).rankByLane, false);
  const bad = overlayFromCts(cts({ minPfStepRatio: "x", minPfStepAutoN: 999, minPfStepAutoMin: 1 }));
  assert.deepEqual([bad.minPfStepRatio, bad.minPfStepAutoN, bad.minPfStepAutoMin], [0.2, 50, 1]);
  assert.equal(overlayFromCts(cts({ minPfStepRatio: 1.8 })).minPfStepRatio, 1.5, "the ratio ends at 1.5");
  assert.equal(overlayFromCts(cts({ minPfStepRatio: 0.01 })).minPfStepRatio, 0.1, "and starts at 0.1");
});


test("per-config lane policy, major/minor identity and quarter SL survive settings round-trip", () => {
  const entry = {label:"Micro", names:["momchg_n5_t1"], major:"trend", minor:"additional", coordination:"independent", symbols:["BTC-USDT"], slRatios:[.25,.5],
    lastNLanes:[{id:"minor",role:"minor",scope:"symbol",lastN:{baseLong:20,realN:5},minPfByStep:{"2":1.01}}]};
  const parsed = indicationConfigList([entry])[0];
  assert.deepEqual(parsed.slRatios,[.25,.5]);
  assert.equal(parsed.major,"trend"); assert.equal(parsed.minor,"additional");
  assert.equal(parsed.lastNLanes![0].lastN.realN,5);
  const ov = overlayFromCts(cts({indicationConfigs:[entry]}));
  assert.deepEqual(overlayFromCts(cts(), syncOverlayFlags(ov)).indicationConfigs,ov.indicationConfigs);
  assert.throws(()=>indicationConfigList([{...entry,lastNLanes:[{id:"bad",lastN:{realN:0}}]}]));
});


test("TP-step PF ratio decimal midpoints match native half-up normalization", () => {
  for(let k=0;k<28;k++) assert.equal(stepPfRatio(.125+.05*k), Math.round((.15+.05*k)*100)/100);
  assert.equal(stepPfRatio(1e300),1.5);
  assert.equal(stepPfRatio(-1e300),.1);
});

test("Normal and Trailing range processing survives save/reload without enabling a sibling", () => {
  const value = {micro:{normal:false,trailing:false},short:{normal:true,trailing:true}};
  const loaded=overlayFromCts(cts(),{stratNormal:false,stratTrailing:true,strategyProcessingByRange:value});
  const saved=overlayFromCts(cts(),syncOverlayFlags(loaded));
  assert.deepEqual(saved.strategyProcessingByRange,value);
  assert.equal(saved.stratNormal,false);
});
