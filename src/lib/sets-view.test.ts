import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_RANGE_ID,
  DEFAULT_TYPE,
  STEP_RANGES,
  STRATEGY_TYPES,
  filterSets,
  formatPf,
  formatPfWithCount,
  inRange,
  pfByStep,
  rangeById,
  rankRows,
  stageFigures,
  windowFigures,
  stepHistogram,
  stepOf,
  summarize,
  summarizeByPack,
  trailArmPct,
  typeById,
  type SetIndexRow,
} from "./sets-view.ts";

const COST = 0.12;
let seq = 0;
function base(st: number, on: number, gp: number, gl: number, extra: Partial<SetIndexRow> = {}): SetIndexRow {
  return { i: seq++, id: `indications:1m:sl1.00:st${st}`, kind: "base", pack: "indications", sl: 1, tr: "", st, on, pf: gl > 0 ? gp / gl : 0, dd: 0, n: 100, gn: 50, gp, gl, ...extra };
}
function trail(arm: string, on: number, gp: number, gl: number, extra: Partial<SetIndexRow> = {}): SetIndexRow {
  return { i: seq++, id: `general:1m:tr${arm}`, kind: "trail", pack: "general", sl: 0.6, tr: arm, st: 0, on, pf: gl > 0 ? gp / gl : 0, dd: 0, n: 100, gn: 50, gp, gl, ...extra };
}

test("the defaults are All and Normal, and the six types are Normal, Exit, Trailing, Axis, Block, DCA in that order", () => {
  assert.equal(DEFAULT_RANGE_ID, "all");
  assert.equal(STEP_RANGES[0].id, "all");
  assert.equal(DEFAULT_TYPE, "normal");
  assert.deepEqual(STRATEGY_TYPES.map((t) => t.label), ["Normal", "Exit", "Trailing", "Axis", "Block", "DCA"]);
  assert.equal(rangeById("nonsense").id, "all", "an unknown range falls back to All");
  assert.equal(typeById("nonsense"), "normal", "an unknown type falls back to Normal");
});

test("range edges are inclusive and All takes every step, even one outside the presets", () => {
  const r26 = rangeById("2-6");
  assert.ok(inRange(2, r26) && inRange(6, r26));
  assert.ok(!inRange(1, r26) && !inRange(7, r26));
  assert.ok(inRange(7, rangeById("7-12")) && inRange(12, rangeById("7-12")) && !inRange(13, rangeById("7-12")));
  assert.ok(inRange(45, rangeById("all")), "All does not clip at the last preset");
  assert.ok(inRange(Number.NaN, rangeById("all")));
  assert.ok(!inRange(Number.NaN, r26), "a step that cannot be read is in no range but All");
});

test("a trailing Set is filtered by its arm in PositionCost steps, not by the step of a base Set", () => {
  assert.equal(trailArmPct("0.60:0.36"), 0.6);
  assert.ok(Number.isNaN(trailArmPct("")));
  const t = trail("0.60:0.36", 1, 1, 1);
  assert.equal(stepOf(t, COST), 5, "0.60 % arm at a 0.12 % cost is step 5");
  assert.equal(stepOf(base(8, 1, 1, 1), COST), 8);
  const rows = [t, trail("1.20:0.60", 1, 1, 1), trail("3.60:1.20", 1, 1, 1)];
  assert.deepEqual(filterSets(rows, "trailing", "2-6", COST).map((r) => r.tr), ["0.60:0.36"]);
  assert.deepEqual(filterSets(rows, "trailing", "7-12", COST).map((r) => r.tr), ["1.20:0.60"]);
  assert.deepEqual(filterSets(rows, "trailing", "21-30", COST).map((r) => r.tr), ["3.60:1.20"]);
});

test("Normal shows base Sets only, Exit the no-TP Sets only, Trailing trailing only, Axis, Block and DCA select no Set", () => {
  const ex = base(5, 1, 1, 1, { kind: "exit", id: "general:1m:sl1.00:st5:xt" });
  const rows = [base(3, 1, 1, 1), base(9, 0, 1, 2), trail("0.60:0.36", 1, 1, 1), ex];
  assert.equal(filterSets(rows, "normal", "all", COST).length, 2);
  assert.deepEqual(filterSets(rows, "exit", "all", COST).map((r) => r.id), [ex.id]);
  assert.deepEqual(filterSets(rows, "exit", "7-12", COST), [], "an Exit Set is filtered by its step like a base Set");
  assert.equal(filterSets(rows, "trailing", "all", COST).length, 1);
  for (const t of ["axis", "block", "dca"] as const) assert.equal(filterSets(rows, t, "all", COST).length, 0);
  assert.deepEqual(filterSets(rows, "normal", "7-12", COST).map((r) => r.st), [9]);
  assert.deepEqual(filterSets(null, "normal", "all", COST), []);
});

test("the summary pools gross profit over gross loss of the valid Sets, as the engine does", () => {
  // valid: gp 3 + 1 = 4, gl 1 + 1 = 2 -> 2.0; every Set: gp 4 + 0.5 = 4.5, gl 2 + 3 = 5 -> 0.9
  const rows = [base(2, 1, 3, 1), base(3, 1, 1, 1), base(4, 0, 0.5, 3)];
  const s = summarize(rows);
  assert.equal(s.total, 3);
  assert.equal(s.valid, 2);
  assert.equal(s.validatedPct, 66.67);
  assert.equal(s.effectivePf, 2);
  assert.equal(s.unfilteredPf, 0.9);
  assert.equal(s.orders, 100, "the valid Sets' gate-window orders");
});

test("PF validation includes a Set waiting for coordination while readiness remains separate", () => {
  const rows = [base(2, 1, 3, 1, { v: 1 }), base(3, 0, 1, 1, { v: 1 }), base(4, 0, 0.5, 3, { v: 0 })];
  const s = summarize(rows);
  assert.equal(s.valid, 2);
  assert.equal(s.eligible, 1);
  assert.equal(s.effectivePf, 2);
  assert.deepEqual(stepHistogram(rows, COST).map((r) => r.valid), [1, 1, 0]);
  assert.deepEqual(pfByStep(rows, COST).map((r) => r.step), [2, 3]);
});

test("a mean of the Sets' own PFs is not the effective PF", () => {
  // two valid Sets: PF 9 on tiny volume and PF 1.1 on large volume. The mean of PFs is 5.05; the pool is 1.1x.
  const rows = [base(2, 1, 0.09, 0.01), base(3, 1, 1.1, 1.0)];
  const s = summarize(rows);
  assert.ok(Math.abs((s.effectivePf as number) - 1.19 / 1.01) < 1e-4);
  assert.ok((s.effectivePf as number) < 1.3);
});

test("empty and flat samples have no PF; all-loss and no-loss samples remain distinct", () => {
  assert.equal(summarize([]).effectivePf, null);
  assert.equal(summarize([]).validatedPct, 0);
  assert.equal(summarize([base(2, 0, 1, 2)]).effectivePf, null);
  assert.equal(summarize([base(2, 1, 1, 0)]).effectivePf, 99);
  assert.equal(summarize([base(2, 1, 0, 0)]).effectivePf, null);
  assert.equal(summarize([base(2, 1, 0, 1)]).effectivePf, 0);
});

test("per-pack split sums to the whole", () => {
  const rows = [base(2, 1, 2, 1), base(3, 0, 1, 2), trail("0.60:0.36", 1, 3, 1)];
  const by = summarizeByPack(rows);
  assert.deepEqual(Object.keys(by), ["general", "indications"]);
  assert.equal(by.indications.total + by.general.total, summarize(rows).total);
  assert.equal(by.indications.valid + by.general.valid, summarize(rows).valid);
});

test("rows are ranked valid first, by the engine rank, then by PF", () => {
  const rows = [
    base(2, 0, 1, 1, { pf: 3 }),
    base(3, 1, 1, 1, { pf: 1.2, rk: 1 }),
    base(4, 1, 1, 1, { pf: 9, rk: 2 }),
    base(5, 1, 1, 1, { pf: 1.1, rk: 0 }),
  ];
  assert.deepEqual(rankRows(rows).map((r) => r.st), [5, 3, 4, 2], "rank 0 first even with the lowest PF; the invalid Set last");
  const noRank = [base(2, 1, 1, 1, { pf: 1.1 }), base(3, 1, 1, 1, { pf: 1.5 })];
  assert.deepEqual(rankRows(noRank).map((r) => r.st), [3, 2], "without a rank the PF decides");
});

test("the step histogram counts total and valid Sets per step in order", () => {
  const rows = [base(4, 1, 1, 1), base(2, 0, 1, 1), base(4, 0, 1, 1)];
  assert.deepEqual(stepHistogram(rows, COST), [
    { step: 2, total: 1, valid: 0 },
    { step: 4, total: 2, valid: 1 },
  ]);
});

test("effective PF per step pools the valid Sets of that step only", () => {
  const rows = [base(4, 1, 3, 1), base(4, 1, 1, 1), base(4, 0, 0, 9), base(6, 1, 1, 2), base(8, 0, 1, 1)];
  const out = pfByStep(rows, COST);
  assert.deepEqual(out, [
    { step: 4, pf: 2, valid: 2, orders: 100 },
    { step: 6, pf: 0.5, valid: 1, orders: 50 },
  ]);
});

test("a PF for display: a window without a loss reads no loss, not 99.00; missing values read a dash", () => {
  assert.equal(formatPf(99), "no loss");
  assert.equal(formatPf(1.2345), "1.23");
  assert.equal(formatPf(0), "0.00");
  assert.equal(formatPf(null), "—");
  assert.equal(formatPf(undefined), "—");
  assert.equal(formatPf(Number.NaN), "—");
  assert.equal(formatPf(98.99), "98.99");
});

test("stage PF is unavailable without its own window sample", () => {
  assert.equal(formatPfWithCount(0, 0), "—");
  assert.equal(formatPfWithCount(99, undefined), "—");
  assert.equal(formatPfWithCount(0, 3), "0.00");
  assert.equal(formatPfWithCount(99, 3), "no loss");
});

test("a flat or empty set window has no measured PF", () => {
  const flat = base(2, 0, 0, 0, { gn: 3 });
  assert.equal(summarize([flat]).unfilteredPf, null);
  const loss = base(2, 0, 0, 1, { gn: 3 });
  assert.equal(summarize([loss]).unfilteredPf, 0);
});

test("stage figures show the Sets that pass each stage, their share of all Sets and their own PF (never the overall average)", () => {
  const fig = (sets: number, pct: number, pf: number, meanPf: number, n: number) => ({ sets, pct, pf, meanPf, n, orders: n });
  const rows = stageFigures({
    stages: {
      base: fig(558, 45.22, 1.6995, 1.71, 27900),
      main: fig(0, 0, 0, 0, 0),
      real: fig(2, 0.16, 0, 0, 0),
      realNeg: fig(3, 0.24, 0.91, 0.9, 75),
      valid: fig(3, 0.24, 0.91, 0.9, 75),
      allSets: 1234,
    },
  });
  assert.deepEqual(rows.map((r) => r.key), ["base", "main", "real", "realNeg", "valid"]);
  assert.equal(rows[0].sets, 558);
  assert.equal(rows[0].pct, 45.22);
  assert.equal(rows[0].pf, 1.6995);
  assert.equal(rows[1].pf, null, "a stage nobody passes has no PF, not 0.00 and not the overall figure");
  assert.equal(rows[1].meanPf, null);
  assert.equal(rows[2].pf, null, "Sets with no stage orders do not show a fabricated PF of zero");
  assert.equal(rows[3].orders, 75);
  assert.deepEqual(stageFigures(undefined), []);
  assert.deepEqual(stageFigures({}), []);
});

test("window figures: each last-N window on its own, an off window counts nothing, a window nobody passes has no PF", () => {
  const w = (on: boolean, n: number | number[] | null, pop: number, sets: number, pf: number, orders: number, extra = {}) =>
    ({ on, n, pop, sets, pct: pop ? Math.round((10000 * sets) / pop) / 100 : 0, pctAll: 0, pf, meanPf: pf, orders, ...extra });
  const rows = windowFigures({
    windows: {
      baseLong: w(true, 50, 1538, 700, 1.7, 33000),
      baseShort: w(true, 25, 1538, 410, 1.9, 9800),
      baseMinimal: w(false, 0, 1538, 0, 0, 0),
      mainLong: w(true, 25, 410, 120, 1.5, 2900),
      mainShort: w(true, [5, 10], 410, 0, 0, 0, { chosen: { "10": 90, "5": 320 } }),
      mainMinimal: w(true, 2, 410, 60, 2.4, 118, { mode: "negative" }),
      negative: w(true, [5, 10], 410, 33, 0.8, 330),
      real: w(true, 15, 120, 40, 1.4, 590),
      realNeg: w(true, 15, 33, 0, 0, 0),
    },
  });
  assert.deepEqual(rows.map((r) => r.key), ["baseLong", "baseShort", "baseMinimal", "mainLong", "mainShort", "mainMinimal", "negative", "real", "realNeg"]);
  assert.equal(rows[2].setting, "off");
  assert.equal(rows[2].pf, null, "an off window has no PF");
  assert.equal(rows[4].setting, "5 · 10");
  assert.equal(rows[4].pf, null, "a window nobody passes has no PF, not 0.00");
  assert.equal(rows[4].chosen, "5: 320 · 10: 90", "the Sets per chosen short window, in window order");
  assert.equal(rows[5].setting, "newest 2 negative");
  assert.equal(rows[5].pf, null, "negative minimal coordination has no PF verdict");
  assert.equal(rows[5].orders, 118);
  assert.equal(rows[0].pct, 45.51);
  assert.equal(rows[0].orders, 33000);
  assert.deepEqual(windowFigures(undefined), []);
  assert.deepEqual(windowFigures({}), []);
});
