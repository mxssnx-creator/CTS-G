import assert from "node:assert/strict";
import { test } from "node:test";
import { changeSetSelection, enabledAxes, INITIAL_SET_SELECTION, matchesSetGroup, readSetOverview, setMetric, tpRangeLabel, type SetGroup } from "./set-overview.ts";
import type { LiveStats } from "./live-stats.ts";

test("all four levels intersect across every source, indication, range and strategy", () => {
  const groups: SetGroup[] = [];
  for (const scope of ["system", "exchange"] as const)
    for (const indicationKind of ["general", "signals", "trend", "break"])
      for (const tpRange of ["0.3000", "1.2000", "unknown"])
        for (const strategyType of ["normal", "trailing", "axis", "block", "dca"])
          groups.push({ scope, indicationKind, tpRange, strategyType, setCount: 1 });
  for (const target of groups) {
    const selection = { scope: target.scope, indication: target.indicationKind, range: target.tpRange, strategy: target.strategyType };
    assert.deepEqual(groups.filter((row) => matchesSetGroup(row, selection)), [target]);
    assert.equal(groups.filter((row) => matchesSetGroup(row, { ...selection, strategy: "all" })).length, 5);
  }
});

test("switching a parent resets only dependent selections", () => {
  const selected = { ...INITIAL_SET_SELECTION, indication: "trend", range: "0.3000", strategy: "block" };
  assert.deepEqual(changeSetSelection(selected, "scope", "exchange"), { ...INITIAL_SET_SELECTION, scope: "exchange" });
  assert.deepEqual(changeSetSelection(selected, "indication", "break"), { ...selected, indication: "break", range: "all", strategy: "all" });
  assert.deepEqual(changeSetSelection(selected, "range", "1.2000"), { ...selected, range: "1.2000", strategy: "all" });
  assert.deepEqual(changeSetSelection(selected, "strategy", "dca"), { ...selected, strategy: "dca" });
});

test("legacy blended results never leak into System or replace Exchange metrics", () => {
  const stats = { sets: { rows: [{ id: "mixed", pack: "indications", n: 95, liveN: 15, last15Ratio: 2.3, maxDdS: 9,
    live: { n: 15, source: "live-exchange", last15Ratio: .9, maxDdS: 120 }, tpPct: .3 }] } } as LiveStats;
  const result = readSetOverview(stats.sets);
  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].scope, "exchange");
  assert.equal(result.rows[0].last15Ratio, .9);
  assert.equal(result.rows[0].maxDdS, 120);
  assert.equal(result.rows[0].last25AvgR, null);
  assert.equal(result.rows[0].indicationKind, "combined");
});

test("axis visibility requires explicit runtime enablement", () => {
  assert.deepEqual(enabledAxes(null), []);
  assert.deepEqual(enabledAxes({ coord: { axes: { last: { enabled: false, max_window: 5 } } } }), []);
  assert.deepEqual(enabledAxes({ coord: { axes: { last: { enabled: true, max_window: 5 }, prev: { enabled: false, max_window: 10 } } } }), ["last"]);
});

test("missing measurements remain distinct from legitimate zero and unknown ranges", () => {
  assert.equal(setMetric(undefined), "—");
  assert.equal(setMetric(null), "—");
  assert.equal(setMetric(0), "0.00");
  assert.equal(tpRangeLabel("0.3000"), "0.3%");
  assert.equal(tpRangeLabel("unknown"), "Unassigned");
});
