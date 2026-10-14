import assert from "node:assert/strict";
import { test } from "node:test";
import { INDICATION_KINDS, IND_TYPE_KEYS, KIND_HINT, KIND_SHORT, indTypeKey, kindShort, mergeKindTypes } from "./indication-kinds.ts";
import { DEFAULT_CALC_OPTIONS } from "./hist-calc.ts";
import { EVALUATION_WINDOWS, evaluationWindowKeys } from "./stage-windows.ts";

test("KIND_HINT and KIND_SHORT cover exactly the indication kinds", () => {
  assert.deepEqual(Object.keys(KIND_HINT), [...INDICATION_KINDS]);
  assert.deepEqual(Object.keys(KIND_SHORT), [...INDICATION_KINDS]);
  for (const kind of INDICATION_KINDS) assert.ok(KIND_HINT[kind].length > 0, kind);
});

test("short codes are unique 2-3 letter codes", () => {
  const codes = INDICATION_KINDS.map(kindShort);
  assert.equal(new Set(codes).size, codes.length, codes.join(","));
  for (const code of codes) assert.match(code, /^[A-Za-z]{2,3}$/);
  assert.equal(kindShort("state") === kindShort("signals"), false);
});

test("historic calc toggles are generated for every kind; coord window default matches the engine", () => {
  assert.equal(indTypeKey("break"), "indTypeBreak");
  assert.equal(IND_TYPE_KEYS.length, INDICATION_KINDS.length);
  for (const key of IND_TYPE_KEYS) {
    assert.equal((DEFAULT_CALC_OPTIONS as unknown as Record<string, unknown>)[key], true, key);
  }
  assert.equal(DEFAULT_CALC_OPTIONS.coordOptimizationN, 50);
});

test("coverage flags override the scan blob so an off kind never reads as on", () => {
  const merged = mergeKindTypes({ state: true, trend: true }, { state: false });
  assert.equal(merged.state, false);
  assert.equal(merged.trend, true);
  assert.deepEqual(mergeKindTypes(null, undefined), {});
});

test("evaluation window keys come from the payload, else the engine windows", () => {
  assert.deepEqual(evaluationWindowKeys({ last50: {}, last5: {}, last30: {}, all: {} }), ["last5", "last30", "last50"]);
  assert.deepEqual(evaluationWindowKeys(undefined), EVALUATION_WINDOWS.map((n) => `last${n}`));
  assert.ok(evaluationWindowKeys(undefined).includes("last30"));
});
