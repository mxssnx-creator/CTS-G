import assert from "node:assert/strict";
import { test } from "node:test";
import { REVERSAL_SOURCES, trendReversalAt, trendReversalSeries } from "./reversal.ts";

const minute = 60_000;
function bars(n: number) {
  let seed = 291, close = 100;
  const random = () => ((seed = Math.imul(1664525, seed) + 1013904223 >>> 0) / 2 ** 32);
  const rows = Array.from({ length: n }, (_, i) => {
    const open = close;
    close += Math.sin(i / 3) * .23 + (random() - .5) * 1.2;
    const high = Math.max(open, close) + random() * .25;
    const low = Math.min(open, close) - random() * .25;
    return [1_790_000_040_000 + i * minute, open, high, low, close, 5 + random() * 40];
  });
  const col = (i: number) => rows.map(r => r[i]);
  return { t: col(0), o: col(1), h: col(2), l: col(3), c: col(4), v: col(5) };
}

test("reversal profiles preserve sign symmetry, prefix parity and independent source IDs", () => {
  const b = bars(1400);
  const flipped = { t: b.t, o: b.o.map(x => 200 - x), h: b.l.map(x => 200 - x),
    l: b.h.map(x => 200 - x), c: b.c.map(x => 200 - x), v: b.v };
  assert.equal(new Set(REVERSAL_SOURCES.map(s => s.id)).size, REVERSAL_SOURCES.length);
  for (const { id, period, tactic } of REVERSAL_SOURCES) {
    const a = trendReversalSeries(b, period, tactic, minute);
    assert.ok(a.some(Boolean), id);
    const reverse = trendReversalSeries(flipped, period, tactic, minute);
    assert.deepEqual(reverse, a.map(x => -x), id);
    const cut = 590;
    const prefix = { t: b.t.slice(0, cut), o: b.o.slice(0, cut), h: b.h.slice(0, cut),
      l: b.l.slice(0, cut), c: b.c.slice(0, cut), v: b.v.slice(0, cut) };
    assert.deepEqual(trendReversalSeries(prefix, period, tactic, minute), a.slice(0, cut), id);
  }
});

test("bad and missing candles do not arm a reversal", () => {
  const b = bars(300);
  const p = { period: 10, tactic: "structure" } as const;
  const i = trendReversalSeries(b, p.period, p.tactic, minute).findIndex(Boolean);
  assert.ok(i > p.period);
  const times = b.t.slice();
  for (let j = i - 1; j < times.length; j++) times[j] += minute;
  assert.equal(trendReversalAt({ ...b, t: times }, i, p.period, p.tactic, minute), 0);
  const lows = b.l.slice();
  lows[i - 4] = NaN;
  assert.equal(trendReversalAt({ ...b, l: lows }, i, p.period, p.tactic, minute), 0);
  assert.equal(trendReversalAt(b, i, 61, p.tactic, minute), 0);
});
