import { test } from "node:test";
import assert from "node:assert/strict";
import { lastNCostPf } from "./analytics.ts";

// The window's PF, its net summed over the positions and its mean net per position share one set of net results (gross fraction less one PositionCost).
test("last-N cost PF: net sum, mean net per position and PF read the same nets", () => {
  const rows = [
    { t: 1, pnl: 0, pnl_pct: 0.004 },
    { t: 2, pnl: 0, pnl_pct: -0.002 },
    { t: 3, pnl: 0, pnl_pct: 0.003 },
  ];
  const m = lastNCostPf(rows, 15, 0.12);
  // nets 0.0028, -0.0032, 0.0018: gross profit 0.0046, gross loss 0.0032
  assert.equal(m.count, 3);
  assert.equal(m.pf, 1.4375);
  assert.ok(Math.abs(m.netSumPct - 0.14) < 1e-9, `net sum ${m.netSumPct}`);
  assert.ok(Math.abs(m.netPct - 0.14 / 3) < 1e-3, `net per position ${m.netPct}`);
  assert.ok(Math.abs(m.netPct * m.count - m.netSumPct) < 1e-3);
});

test("last-N cost PF: PF above 1 exactly when the net is positive", () => {
  for (const [rows, positive] of [
    [[{ t: 1, pnl: 0, pnl_pct: 0.0005 }, { t: 2, pnl: 0, pnl_pct: 0.0006 }], false], // both gains are under one PositionCost: net losses
    [[{ t: 1, pnl: 0, pnl_pct: 0.005 }, { t: 2, pnl: 0, pnl_pct: -0.001 }], true],
    [[{ t: 1, pnl: 0, pnl_pct: -0.004 }, { t: 2, pnl: 0, pnl_pct: 0.0015 }], false],
  ] as const) {
    const m = lastNCostPf([...rows], 15, 0.12);
    assert.equal(m.netSumPct > 0, positive);
    assert.equal(m.pf > 1, positive);
    assert.equal(m.netPct > 0, positive);
  }
});
