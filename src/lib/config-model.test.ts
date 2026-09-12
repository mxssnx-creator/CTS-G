import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DEFAULT_OVERLAY,
  DEFAULT_SYMBOL_COUNT,
  isUnlimitedSymbolBook,
  overlayFromCts,
  rankedSymbolCap,
  syncOverlayFlags,
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
      assert.equal(value[key], 1.10, key);
    assert.equal(value.maxDdTimeS, 57600);
    assert.equal(value.setMaxDdTimeS, 57600);
    assert.equal(value.positionCostFallbackPct, 0.1);
    assert.equal(value.useLivePositionCosts, true);
  }
});

test("risk and step limits preserve unlimited TP and the 0.15 percent SL floor", () => {
  for (const value of [DEFAULT_OVERLAY, overlayFromCts({})]) {
    assert.equal(value.tpMinPct, .3);
    assert.equal(value.tpMaxPct, 0);
    assert.equal(value.slMinPct, .15);
    assert.equal(value.slMaxPct, 3);
    assert.equal(value.setStepMax, 30);
  }
  const value = overlayFromCts({}, { tpMaxPct: 12, slMinPct: .15, setStepMax: 70, minPf: 2.5, setMinPf: .8 });
  assert.equal(value.tpMaxPct, 12);
  assert.equal(value.slMinPct, .15);
  assert.equal(value.setStepMax, 30);
  assert.equal(value.minPf, 1.35);
  assert.equal(value.setMinPf, 1.05);
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

test("new and legacy settings default to unlimited logical positions, independent lanes and 50 symbols", () => {
  for (const value of [DEFAULT_OVERLAY, overlayFromCts({})]) {
    assert.equal(value.normalExecutionEnabled, true);
    assert.equal(value.blockActive, true);
    assert.equal(value.setMaxActive, 0);
    assert.equal(value.maxOpen, 0);
    assert.equal(value.controlOrdersPerConfig, true);
    assert.equal(value.entryPolicyMaxCandidates, 0);
    assert.equal(value.dcaEnabled, true);
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
    assert.equal(value.symbolCap, 50);
    assert.equal(isUnlimitedSymbolBook(value), false);
    assert.equal(rankedSymbolCap(value), DEFAULT_SYMBOL_COUNT);
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
