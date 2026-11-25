import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_RELATE_CONFIG, RELATE_SOURCES, relateConfigs } from "./relate-config.ts";

test("relate: absent key is the disabled default policy", () => {
  const [c] = relateConfigs(undefined);
  assert.deepEqual(c, DEFAULT_RELATE_CONFIG);
  assert.equal(c.enabled, false);
  assert.equal(c.maxLossRun, 0);
});

test("relate: sources include the config-wide and TP-step relations, in the server's order", () => {
  assert.ok(RELATE_SOURCES.includes("configAll"));
  assert.ok(RELATE_SOURCES.includes("step"));
  const [c] = relateConfigs([{ id: "x", sources: ["step", "configAll", "config"] }]);
  assert.deepEqual(c.sources, ["config", "configAll", "step"]);
});

test("relate: bounds equal the server's (minimum closes 5, last-N 10, stop ceiling 1.25, loss run 0..12)", () => {
  const ok = (patch: object) => relateConfigs([{ id: "p", ...patch }]);
  assert.doesNotThrow(() => ok({ minTrades: 5, lastN: 10, maxSlRatio: 1.25, maxLossRun: 12 }));
  for (const bad of [{ minTrades: 4 }, { lastN: 9, minTrades: 5 }, { maxSlRatio: 1.2501 }, { maxLossRun: 13 }, { maxLossRun: 1.5 }, { ratio: 0.3 }, { minTrades: 61, lastN: 60 }, { sources: ["nope"] }])
    assert.throws(() => ok(bad), undefined, JSON.stringify(bad));
});
