import { test } from "node:test";
import assert from "node:assert/strict";
import { basePackFigures, historicalPlotPf } from "./base-progress.ts";

test("Base charts use Base-only evidence, not the final-stage cohort", () => {
  const rows = basePackFigures({ general: {
    base: { pf: 3, n: 10, sets: 1 }, baseOnly: { pf: 1.4, n: 50, sets: 5 },
    unfiltered: { pf: .8, n: 100, sets: 10 }, total: 10, valid: 1, validatedPct: 10, validatedBasePct: 50,
  } });
  assert.deepEqual(rows[0], { id: "general", effective: 1.4, unfiltered: .8,
    validated: 50, baseValid: 5, allStageValid: 1, total: 10 });
});

test("missing Base evidence and an ambiguous no-loss sentinel are not plotted", () => {
  const rows = basePackFigures({ general: {
    base: { pf: 3, n: 10, sets: 1 }, unfiltered: { pf: 0, n: 0, sets: 10 },
    total: 10, valid: 1, validatedPct: 10,
  } });
  assert.equal(rows[0].effective, null);
  assert.equal(rows[0].validated, null);
  assert.equal(rows[0].unfiltered, null);
  assert.equal(historicalPlotPf(99, 5), null);
  assert.equal(historicalPlotPf(100, 5), null);
  assert.equal(historicalPlotPf(0, 5), 0);
});
