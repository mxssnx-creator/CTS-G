import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_RELATION_LANES, RELATIONS, RELATION_ROLE, relationLanesOf, relationMultiple } from "./relation-lanes.ts";
import { liftRows, relationFigures, type ExecutionLift } from "./live-stats.ts";
import { stageProgress } from "./base-progress.ts";

test("relation lanes are off unless explicitly enabled and unreadable parts take their defaults", () => {
  assert.deepEqual(relationLanesOf(undefined), DEFAULT_RELATION_LANES);
  assert.equal(relationLanesOf({ enabled: "yes" }).enabled, false);
  assert.equal(relationLanesOf({ enabled: true, mainN: "x" }).mainN, DEFAULT_RELATION_LANES.mainN);
  assert.deepEqual(RELATIONS.map((r) => RELATION_ROLE[r]), ["overall", "major", "major", "minor", "minor"]);
});

test("relation lane values are bounded as the engine bounds them", () => {
  const c = relationLanesOf({ enabled: true, mainN: 999, realN: -4, minPf: 0.3, maxMult: 40, relations: { overall: -1, type: { on: false, ratio: 9 }, step: false } });
  assert.equal(c.mainN, 200);
  assert.equal(c.realN, 0);
  assert.equal(c.minPf, 1.02);
  assert.equal(c.maxMult, 5);
  assert.deepEqual(c.relations.overall, { on: true, ratio: 0 });
  assert.deepEqual(c.relations.type, { on: false, ratio: 2 });
  assert.deepEqual(c.relations.step, { on: false, ratio: 0 });
  assert.deepEqual(c.relations.indication, DEFAULT_RELATION_LANES.relations.indication);
});

test("the multiple is 1 plus the passing relations' ratios, capped, and 1 when the lanes are off", () => {
  const c = relationLanesOf({ enabled: true, maxMult: 1.6, relations: { overall: 0.25, indication: 0.25, type: 0.25, range: 0.5, step: { on: false, ratio: 1 } } });
  assert.equal(relationMultiple(c, []), 1);
  assert.equal(relationMultiple(c, ["overall", "type"]), 1.5);
  assert.equal(relationMultiple(c, ["overall", "indication", "type", "range"]), 1.6);
  assert.equal(relationMultiple(c, ["step"]), 1);
  assert.equal(relationMultiple({ ...c, enabled: false }, ["overall", "type"]), 1);
});

test("relation figures appear only for built lanes of enabled relation lanes, in the fixed order", () => {
  const row = { role: "major", on: true, ratio: 0.25, lanes: 2, mainOk: 1, ok: 1, entries: 9, orders: 30, mainPf: 1.2, mainObs: 9, passPf: 1.5, passObs: 4 };
  assert.deepEqual(relationFigures({ config: { enabled: false }, byRelation: { type: row } }), []);
  assert.deepEqual(relationFigures({ config: { enabled: true }, byRelation: {} }), []);
  assert.deepEqual(relationFigures({ config: { enabled: true }, byRelation: { type: row, overall: { ...row, role: "overall" } } }).map((r) => r.key), ["overall", "type"]);
});

test("with / without rows show only stack types that acted, on the same parents", () => {
  const pair = (pf: number | null, netPct: number) => ({ pf, netPct });
  const lift: ExecutionLift = {
    parents: 6, unknown: 1,
    block: { parentsWithAdds: 2, adds: 3, without: pair(3.29, 3.2), with: pair(2.42, 2.7), addParentsWithout: pair(99, 3), addParentsWith: pair(6, 2.5) },
    relation: { raised: 0, volume: 6, without: pair(3.29, 3.2), with: pair(3.29, 3.2), netPerVolumeWithoutPct: 0.53, netPerVolumeWithPct: 0.53, raisedUnit: pair(null, 0), notRaisedUnit: pair(3.29, 3.2) },
  };
  assert.deepEqual(liftRows(lift).map((r) => r.key), ["block-all", "block-add"]);
  assert.deepEqual(liftRows({ ...lift, block: { ...lift.block, parentsWithAdds: 0, adds: 0 }, relation: { ...lift.relation, raised: 2 } }).map((r) => r.key), ["relation-all", "relation-raised"]);
  assert.deepEqual(liftRows({ ...lift, parents: 0 }), []);
  assert.deepEqual(liftRows(undefined), []);
});

test("stage progress keeps every stage on its own line and a missing figure is a gap, not zero", () => {
  const points = stageProgress([
    { t: 1_700_000_000, pf: 1.2, n: 10, valid: 3, validatedPct: 3, unfiltered: 0.8 },
    { t: 1_700_000_060, pf: 1.3, n: 12, valid: 4, validatedPct: 4, unfiltered: 0.8,
      stages: { base: { sets: 20, pct: 20, pf: 1.4 }, main: { sets: 8, pct: 8, pf: 1.5 }, real: { sets: 4, pct: 4, pf: 1.6 }, realNeg: { sets: 2, pct: 2, pf: 1.1 } },
      relations: { lanes: 40, passing: 7 } },
    { t: 1_700_000_120, pf: 1.3, n: 12, valid: 4, validatedPct: 4, unfiltered: 0.8, stages: { base: { sets: 0, pct: 0, pf: null } }, relations: { lanes: 0, passing: 0 } },
  ]);
  assert.deepEqual([points[0].base, points[0].relations], [null, null]);
  assert.deepEqual([points[1].base, points[1].main, points[1].real, points[1].realNeg, points[1].relations], [20, 8, 4, 2, 17.5]);
  assert.deepEqual([points[2].base, points[2].main, points[2].relations], [0, null, null]);
});
