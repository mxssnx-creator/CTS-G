import { test } from "node:test";
import assert from "node:assert/strict";
import { hourlyPf } from "./hourly-pf.ts";

const NOW = 1_800_000_000 + 1800; // half past an hour
const H = Math.floor(NOW / 3600) * 3600;

test("each close lands in its own clock hour and PF is net of PositionCost", () => {
  const closed = [
    { t: H + 10, pnl_pct: 0.0036 }, // +0.36% gross, +0.24% net
    { t: H + 20, pnl_pct: 0.0 }, // -0.12% net
    { t: H - 3600 + 5, pnl_pct: 0.0012 }, // exactly the cost: net 0, neither profit nor loss
  ] as never[];
  const rows = hourlyPf(closed, 0.12, NOW, 3);
  assert.equal(rows.length, 3);
  assert.equal(rows[2].n, 2);
  assert.ok(Math.abs((rows[2].pf as number) - 2) < 1e-9);
  assert.equal(rows[1].n, 1);
  assert.equal(rows[1].pf, 0, "a close at exactly the cost has no profit: PF 0, not null");
  assert.equal(rows[0].n, 0);
  assert.equal(rows[0].pf, null, "an hour without trades is a gap, not a zero");
});

test("a close without pnl_pct is skipped, not counted as a loss", () => {
  const closed = [
    { t: H + 1, pnl_pct: null },
    { t: H + 2, pnl_pct: undefined },
    { t: H + 3, pnl_pct: 0.0036 },
  ] as never[];
  const last = hourlyPf(closed, 0.12, NOW, 1)[0];
  assert.equal(last.n, 1);
  assert.equal(last.pf, 9.99, "no loss in the hour: capped at 9.99");
});

test("millisecond timestamps are read as such", () => {
  const closed = [{ t: (H + 10) * 1000, pnl_pct: 0.0036 }] as never[];
  assert.equal(hourlyPf(closed, 0.12, NOW * 1000, 1)[0].n, 1);
});
