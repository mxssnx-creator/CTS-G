import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DEFAULT_OVERLAY,
  DEFAULT_SYMBOL_COUNT,
  isUnlimitedSymbolBook,
  overlayFromCts,
  rankedSymbolCap,
  syncOverlayFlags,
  blockTable,
  sharedBlockVolumeRatio,
  clampHistTestRefreshHours,
  HIST_TEST_REFRESH_DEFAULT,
} from "./config-model.ts";

test("General basis cannot be disabled and Normal is independent of Block Active levels", () => {
  assert.equal(DEFAULT_OVERLAY.normalExecutionEnabled, true);
  assert.equal(DEFAULT_OVERLAY.blockActiveMinLevel, 0);
  for (const normal of [false, true]) for (const active of [false, true]) for (const level of [0, 1, 3, 6]) {
    const loaded = overlayFromCts({}, { normalExecutionEnabled: normal, blockActive: active,
      blockActiveMinLevel: level, stratGeneral: false, histEnabled: false });
    const saved = syncOverlayFlags(loaded);
    assert.equal(saved.normalExecutionEnabled, normal);
    assert.equal(saved.blockActive, active);
    assert.equal(saved.blockActiveMinLevel, level);
    assert.equal(saved.stratGeneral, true);
    assert.equal(saved.histEnabled, true);
    assert.equal(saved.modules?.["core.historic"], true);
  }
});

test("System settings survive save and use bounded values without disabling automatic memory", () => {
  const saved = syncOverlayFlags(overlayFromCts({}, { systemWorkers: 99, systemDbMaxMb: 4,
    systemStatsIntervalS: 9, systemOrderRps: 50, rssSoftMb: 0, rssHardMb: 0, blockMaxStack: 3, blockActiveMinLevel: 6 }));
  assert.equal(saved.systemWorkers, 8);
  assert.equal(saved.systemDbMaxMb, 8);
  assert.equal(saved.systemOrderRps, 2.4);
  assert.equal(saved.systemStatsIntervalS, 9);
  assert.equal(saved.rssSoftMb, 0);
  assert.equal(saved.rssHardMb, 0);
  assert.equal(saved.blockActiveMinLevel, 3);
});

test("SQLite RAM defaults and disk selection survive the complete settings roundtrip", () => {
  assert.equal(DEFAULT_OVERLAY.systemSqliteMemory, 1);
  assert.equal(DEFAULT_OVERLAY.systemSqliteCheckpointS, 10);
  for (const mode of [0, 1]) {
    const saved = syncOverlayFlags(overlayFromCts({}, { systemSqliteMemory: mode, systemSqliteCheckpointS: 3 }));
    assert.equal(saved.systemSqliteMemory, mode);
    assert.equal(saved.systemSqliteCheckpointS, 3);
    assert.equal(saved.normalExecutionEnabled, true);
    assert.equal(saved.stratGeneral, true);
  }
  assert.equal(overlayFromCts({}, { systemSqliteCheckpointS: 0 }).systemSqliteCheckpointS, 1);
  assert.equal(overlayFromCts({}, { systemSqliteCheckpointS: 1000 }).systemSqliteCheckpointS, 60);
});

test("PF, DD and dynamic cost defaults share the requested policy", () => {
  for (const value of [DEFAULT_OVERLAY, overlayFromCts({})]) {
    for (const key of ["minPf", "baseMinPf", "mainMinPf", "realMinPf", "setMinPf", "dcaMinPf", "exitMinPf"] as const)
      assert.equal(value[key], 1.15, key);
    assert.equal(value.maxDdTimeS, 57600);
    assert.equal(value.setMaxDdTimeS, 57600);
    assert.equal(value.positionCostFallbackPct, 0.1);
    assert.equal(value.useLivePositionCosts, true);
  }
});

test("risk and step limits preserve unlimited TP and the 0.4 percent SL floor", () => {
  for (const value of [DEFAULT_OVERLAY, overlayFromCts({})]) {
    assert.equal(value.tpMinPct, .3);
    assert.equal(value.tpMaxPct, 0);
    assert.equal(value.slMinPct, .4);
    assert.equal(value.slMaxPct, 3);
    assert.equal(value.setMinStep, 7);
    assert.equal(value.minStep, 7);
    assert.equal(value.trailingMinStep, 7);
    assert.equal(value.setStepMax, 30);
  }
  const value = overlayFromCts({}, { tpMaxPct: 12, slMinPct: .15, setStepMax: 70, minPf: 2.5, setMinPf: .8, minStep: 1, trailingMinStep: 1 });
  assert.equal(value.tpMaxPct, 12);
  assert.equal(value.slMinPct, .4);
  assert.equal(value.setStepMax, 30);
  assert.equal(value.setMinStep, 7);
  assert.equal(value.minStep, 7);
  assert.equal(value.trailingMinStep, 7);
  assert.equal(value.minPf, 1.35);
  assert.equal(value.setMinPf, 1.35);
});

test("saving a measured cost never overwrites the explicit fallback", () => {
  const value = syncOverlayFlags(overlayFromCts({}, {
    positionCostPct: 0.087, positionCostFallbackPct: 0.1,
    maxDdTimeS: 999999, setMaxDdTimeS: 999999,
  }));
  assert.equal(value.positionCostPct, 0.087);
  assert.equal(value.positionCostFallbackPct, 0.1);
  assert.equal(value.maxDdTimeS, 57600);
  assert.equal(value.setMaxDdTimeS, 57600);
});

test("new and legacy settings default to ranked 50, 100 opens, independent lanes", () => {
  for (const value of [DEFAULT_OVERLAY, overlayFromCts({})]) {
    assert.equal(value.normalExecutionEnabled, true);
    assert.equal(value.blockActive, true);
    assert.equal(value.blockOverall, true);
    assert.equal(value.setMaxActive, 0);
    assert.equal(value.maxOpen, 100);
    assert.equal(value.controlOrdersPerConfig, true);
    assert.equal(value.entryPolicyMaxCandidates, 0);
    assert.equal(value.dcaEnabled, false);
    assert.equal(value.stratDca, true);
    assert.equal(value.modules?.["strategy.dca"], true);
    assert.equal(value.stratGeneral, true);
    assert.equal(value.stratIndications, true);
    assert.equal(value.stratTrailing, true);
    assert.equal(value.stratBlock, true);
    for (const key of ["indTypeState", "indTypeDirection", "indTypeMove", "indTypeActive", "indTypeCommon", "indTypeSignals", "indTypeTrend", "indTypeBreak"] as const) {
      assert.equal(value[key], true, key);
    }
    for (const key of ["strategy.block", "strategy.dca", "strategy.indications", "strategy.trailing", "strategy.exits", "exec.controls"] as const) {
      assert.equal(value.modules?.[key], true, key);
    }
    assert.equal(value.symbolCap, DEFAULT_SYMBOL_COUNT);
    assert.equal(isUnlimitedSymbolBook(value), false);
    assert.equal(rankedSymbolCap(value), DEFAULT_SYMBOL_COUNT);
    assert.equal(value.minPf, 1.15);
    assert.equal(value.baseMinPf, 1.15);
    assert.equal(value.histLookbackBars, 2880);
    assert.equal(value.histTestHours, 20);
    assert.equal(value.histTestMinPf, 1.15);
    assert.equal(value.histTestEnabled, true);
    assert.equal(value.baseEvalPosCount, 30);
    assert.equal(value.setMinStep, 7);
  }
});

test("missing control mode fields default to independent per-config pairs", () => {
  const value = syncOverlayFlags(overlayFromCts({}, { controlOrdersPerConfig: undefined }));
  assert.equal(value.controlOrdersPerConfig, true);
});

test("an explicit All/* cap of 25 stays ranked and is not unlimited", () => {
  const ranked = syncOverlayFlags(overlayFromCts({}, { symbolsAll: true, symbols: ["*"], symbolCap: 25 }));
  assert.equal(ranked.symbolCap, 25);
  assert.equal(ranked.symbolsAll, true);
  assert.equal(isUnlimitedSymbolBook(ranked), false);
  const unlimited = syncOverlayFlags(overlayFromCts({}, { symbolsAll: true, symbols: ["*"], symbolCap: 0 }));
  assert.equal(unlimited.symbolCap, 0);
  assert.equal(isUnlimitedSymbolBook(unlimited), true);
});

test("execution switches survive merge and save normalization independently", () => {
  for (const normal of [false, true]) for (const active of [false, true]) {
    const value = syncOverlayFlags(overlayFromCts({}, {
      normalExecutionEnabled: normal, blockActive: active, stratGeneral: true,
    }));
    assert.equal(value.normalExecutionEnabled, normal);
    assert.equal(value.blockActive, active);
    assert.equal(value.stratGeneral, true);
  }
});

test("an explicit unlimited Set selection remains a user choice", () => {
  assert.equal(overlayFromCts({}, { setMaxActive: 0 }).setMaxActive, 0);
});

test("historic test hours stay 4–64 default 20 and min PF 1.15", () => {
  for (const value of [DEFAULT_OVERLAY, overlayFromCts({})]) {
    assert.equal(value.histTestHours, 20);
    assert.equal(value.histTestMinPf, 1.15);
    assert.equal(value.histTestEnabled, true);
  }
  const clamped = syncOverlayFlags(overlayFromCts({}, { histTestHours: 99, histTestMinPf: 0.5 }));
  assert.equal(clamped.histTestHours, 64);
  assert.equal(clamped.histTestMinPf, 1.02);
  const off = syncOverlayFlags(overlayFromCts({}, { histTestEnabled: false }));
  assert.equal(off.histTestEnabled, false);
  const low = syncOverlayFlags(overlayFromCts({}, { histTestHours: 2, histTestMinPf: 1.15 }));
  assert.equal(low.histTestHours, 4);
  const mid = syncOverlayFlags(overlayFromCts({}, { histTestHours: 20, histTestMinPf: 1.15 }));
  assert.equal(mid.histTestHours, 20);
  assert.equal(mid.histLookbackBars, 2880);
  const shifted = syncOverlayFlags(overlayFromCts({}, { histTestHours: 48, histLookbackBars: 2880, histTestMinPf: 1.15 }));
  assert.equal(shifted.histTestHours, 48);
  assert.equal(shifted.histLookbackBars, 2880);
  assert.equal(shifted.histTestMinPf, 1.15);
});

test("Control holdout defaults off and preserves zero independently of PF and last-N", () => {
  assert.equal(DEFAULT_OVERLAY.controlMinTrades, 0);
  assert.equal(overlayFromCts({}).controlMinTrades, 0);
  for (const controlMinTrades of [0, 5, 25, 100]) {
    const value = syncOverlayFlags(overlayFromCts({controlMinTrades:8}, {controlMinTrades}));
    assert.equal(value.controlMinTrades, controlMinTrades);
    assert.equal(value.baseEvalPosCount, 30);
    assert.equal(value.minPf, 1.15);
    assert.equal(value.controlOrdersPerConfig, true);
  }
});

test("Block table shares overlay ratio 1.0 across the live stack, not the 6-count preview", () => {
  assert.equal(sharedBlockVolumeRatio(1, 3, 1), 1 / 3);
  assert.equal(sharedBlockVolumeRatio(0.25, 6, 1), 0.25);
  const rows = blockTable(1, 1.1, 1.1, 1, 3, 2, [1, 2, 3, 4, 5, 6]);
  assert.equal(rows.length, 3);
  assert.ok(rows.every((row) => row.step > 0));
  assert.equal(Math.round(rows[0].inc * 1e9) / 1e9, Math.round((1 / 3) * 1e9) / 1e9);
  assert.equal(rows[2].tot, 2);
  const quarter = blockTable(0.25, 1.1, 1.1, 1, 6, 2);
  assert.equal(quarter[0].tot, 1.25);
  assert.equal(quarter[3].tot, 2);
  assert.equal(quarter[5].tot, 2);
  assert.equal(quarter[4].step, 0);
});

test("forced symbol winners survive overlay roundtrip", () => {
  const saved = syncOverlayFlags(overlayFromCts({}, {
    forcedSymbols: ["XRP-USDT", "BCH-USDT", "SOL-USDT"],
    forcedVariant: "overlay-default",
    forcedEligible: 404,
    forcedBest: {
      "XRP-USDT": { indication: "signals", direction: "LONG", tpPct: 0.6, slPct: 0.2 },
      "BCH-USDT": { indication: "break", direction: "SHORT", tpPct: 0.75, slPct: 0.25 },
      "SOL-USDT": { indication: "trend", direction: "LONG", tpPct: 0.55, slPct: 0.1 },
    },
    tpPct: 0.6,
    slPct: 0.2,
  }));
  assert.deepEqual(saved.forcedSymbols, ["XRP-USDT", "BCH-USDT", "SOL-USDT"]);
  assert.equal(saved.forcedVariant, "overlay-default");
  assert.equal(saved.forcedEligible, 404);
  assert.equal(saved.forcedBest?.["XRP-USDT"]?.tpPct, 0.6);
  assert.equal(saved.forcedBest?.["BCH-USDT"]?.slPct, 0.25);
  assert.equal(saved.forcedBest?.["SOL-USDT"]?.indication, "trend");
  assert.equal(saved.tpPct, 0.6);
  assert.equal(saved.slPct, 0.4);
});

test("historic test refresh interval is 1–8 hours default 2", () => {
  assert.equal(DEFAULT_OVERLAY.histTestRefreshHours, HIST_TEST_REFRESH_DEFAULT);
  assert.equal(overlayFromCts({}).histTestRefreshHours, 2);
  assert.equal(overlayFromCts({}, { histTestRefreshHours: 5 }).histTestRefreshHours, 5);
  assert.equal(syncOverlayFlags(overlayFromCts({}, { histTestRefreshHours: 0 })).histTestRefreshHours, 1);
  assert.equal(syncOverlayFlags(overlayFromCts({}, { histTestRefreshHours: 99 })).histTestRefreshHours, 8);
  assert.equal(clampHistTestRefreshHours(undefined), 2);
});
