import { test } from "node:test";
import assert from "node:assert/strict";
import {
  relationalVolumeFactor,
  relationalVolumeSettings,
  DEFAULT_RELATIONAL_VOLUME,
} from "./ratios.ts";
test("neutral defaults equalize every type/range; matching independent weights multiply once and isolate scopes", () => {
  const context = {
    indicationType: "Tactic",
    indication: "tactic-dca-bounce-8-percent-0.5-1-0.25",
    strategy: "axis",
    range: "Micro",
    symbol: "SOL-USDT",
  };
  for (let i = 0; i < 50; i++) {
    const v = (i + 1) / 10;
    const p = relationalVolumeSettings(
      { ...DEFAULT_RELATIONAL_VOLUME, indicationTypes: { tactic: v } },
      true,
    );
    assert.equal(relationalVolumeFactor(p, context), v);
    assert.equal(relationalVolumeFactor(p, { ...context, indicationType: "inverse" }), 1);
  }
  const p = relationalVolumeSettings(
    {
      overall: 1.2,
      indicationTypes: { tactic: 2 },
      strategies: { axis: 0.5 },
      ranges: { micro: 1.5 },
      symbols: { "SOL-USDT": 0.5 },
    },
    true,
  );
  assert.ok(Math.abs(relationalVolumeFactor(p, context) - 0.9) < 1e-12);
  assert.equal(relationalVolumeFactor(undefined, context), 1);
  assert.equal(
    relationalVolumeFactor(
      { ranges: { short: 2, atr: 0.5 } },
      { range: ["short", "atr", "short"] },
    ),
    1,
  );
  assert.deepEqual(relationalVolumeSettings(JSON.stringify(p), true), p);
  assert.equal(relationalVolumeFactor({ ...p, enabled: false }, context), 1);
  assert.equal(
    relationalVolumeFactor(
      { ...p, overall: 5, strategies: { axis: 5 }, maxMultiplier: 2 },
      context,
    ),
    2,
  );
  const q = structuredClone(p);
  q.ranges.micro = 0.1;
  assert.notDeepEqual(q, p);
  assert.equal(DEFAULT_RELATIONAL_VOLUME.overall, 1);
});
test("invalid weights and dangerous keys are rejected; optional legacy absence is neutral", () => {
  for (const raw of [
    { overall: NaN },
    { overall: Infinity },
    { overall: "2" },
    { overall: 0 },
    { overall: 5.1 },
    { overall: 1.25 },
    { enabled: "false" },
    { ranges: { unknown: -1 } },
    { ranges: { constructor: 2 } },
    { ranges: { Micro: 2, micro: 3 } },
    { extra: 2 },
    [],
  ])
    assert.throws(() => relationalVolumeSettings(raw, true));
  assert.equal(relationalVolumeFactor("{broken", { range: "Micro" }), 1);
  const observed = [-0.02, 0.01, -0.005, 0.03],
    factor = relationalVolumeFactor({ ranges: { micro: 2 } }, { range: "Micro" });
  const cash = observed.map((r) => 100 * factor * r),
    unit = cash.map((r) => r / (100 * factor));
  assert.deepEqual(unit, observed, "sizing never changes unit outcome or its sign");
});
