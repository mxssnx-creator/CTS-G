import assert from "node:assert/strict";
import { test } from "node:test";
import { detailsCsv, dimensionMatrix, metricPoints, sortSetDetails } from "./dimension-stats.ts";
import type { SetOverviewRow } from "./set-overview.ts";

const row = (id: string, values: Partial<SetOverviewRow> = {}): SetOverviewRow => ({ id, scope: "system", indicationKind: "trend", strategyType: "normal", tpRange: "0.3000", n: 15, last15Ratio: 1.2, maxDdS: 120, active: true, ...values });

test("matrix sums full catalog counts across TP ranges without using preview metrics", () => {
  const counts = dimensionMatrix([
    { ...row("one"), setCount: 340 }, { ...row("two", { tpRange: "1.2000" }), setCount: 10 },
    { ...row("block", { strategyType: "block" }), setCount: 20 },
  ]);
  assert.deepEqual(counts, [{ indication: "trend", strategy: "normal", count: 350 }, { indication: "trend", strategy: "block", count: 20 }]);
});

test("scatter preserves zero, missing values and source-specific PF/DDT correctly", () => {
  const result = metricPoints([row("system"), row("exchange", { scope: "exchange", last15Ratio: .8, maxDdS: 0 }),
    row("missing", { maxDdS: null }), row("pf-missing", { last15Ratio: null }), row("empty", { n: 0 }),
    row("bad", { last15Ratio: Infinity }), row("negative", { maxDdS: -1 })]);
  assert.equal(result.available, 2);
  assert.deepEqual(result.points.map((r) => [r.id, r.pf, r.ddMinutes]), [["exchange", .8, 0], ["system", 1.2, 2]]);
});

test("bounded scatter retains small indication/strategy families and leaves input unchanged", () => {
  const rows = Array.from({ length: 2000 }, (_, i) => row(`large-${i}`));
  rows.push(row("rare", { indicationKind: "break", strategyType: "dca", connection: "VST" }));
  const before = JSON.stringify(rows);
  const result = metricPoints(rows);
  assert.equal(result.available, 2001);
  assert.equal(result.points.length, 350);
  assert.ok(result.points.some((r) => r.id === "rare"));
  assert.equal(JSON.stringify(rows), before);
  assert.deepEqual(metricPoints(rows, 0).points, []);
});

test("details sort missing values last, preserve all rows and stabilize ties", () => {
  const rows = [row("none", { last15Ratio: null }), row("b"), row("a"), row("zero", { last15Ratio: 0 })];
  assert.deepEqual(sortSetDetails(rows, "pf").map((r) => r.id), ["a", "b", "zero", "none"]);
  assert.equal(rows[0].id, "none");
});

test("CSV keeps negative numbers, quotes and missing values without spreadsheet formulas", () => {
  const csv = detailsCsv([row("=DANGER()", { expectancy: -.002, deactReason: 'comma, "quote"', avgDdS: null })]);
  assert.ok(csv.includes('"\'=DANGER()"'));
  assert.ok(csv.includes('"-0.002"'));
  assert.ok(csv.includes('"comma, ""quote"""'));
  assert.ok(!csv.includes("null"));
  assert.equal(csv.split("\r\n").length, 2);
});
