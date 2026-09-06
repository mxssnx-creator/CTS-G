import assert from "node:assert/strict";
import { test } from "node:test";
import { DEFAULT_OVERLAY, overlayFromCts, syncOverlayFlags } from "./config-model.ts";

test("PF, DD and dynamic cost defaults share the requested policy", () => {
  for (const value of [DEFAULT_OVERLAY, overlayFromCts({})]) {
    for (const key of ["minPf", "baseMinPf", "mainMinPf", "realMinPf", "setMinPf", "dcaMinPf", "exitMinPf"] as const)
      assert.equal(value[key], 1.05, key);
    assert.equal(value.maxDdTimeS, 57600);
    assert.equal(value.setMaxDdTimeS, 57600);
    assert.equal(value.positionCostFallbackPct, 0.1);
    assert.equal(value.useLivePositionCosts, true);
  }
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

test("new and legacy settings default to adjusted execution and 80 Sets", () => {
  for (const value of [DEFAULT_OVERLAY, overlayFromCts({})]) {
    assert.equal(value.normalExecutionEnabled, false);
    assert.equal(value.blockActive, true);
  assert.equal(value.setMaxActive, 80);
    assert.equal(value.stratGeneral, true);
  }
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
