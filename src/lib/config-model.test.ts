import assert from "node:assert/strict";
import { test } from "node:test";
import { DEFAULT_OVERLAY, overlayFromCts, syncOverlayFlags } from "./config-model.ts";

test("new and legacy settings default to adjusted execution and 50 Sets", () => {
  for (const value of [DEFAULT_OVERLAY, overlayFromCts({})]) {
    assert.equal(value.normalExecutionEnabled, false);
    assert.equal(value.blockActive, true);
    assert.equal(value.setMaxActive, 50);
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
