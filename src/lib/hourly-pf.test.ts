import { test } from "node:test";
import assert from "node:assert/strict";
import { hourlyPf } from "./hourly-pf.ts";

test("hourly execution PF uses recorded net without recharging the allowance", () => {
  const now = 1793322000;
  const rows = [
    { t: now, pnl_pct: .001, net_pnl_pct: .0004 },
    { t: now + 1, pnl_pct: .001, net_pnl_pct: -.0002 },
  ] as never[];
  assert.equal(hourlyPf(rows, .3, now + 2, 1)[0].pf, 2);
});

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
  assert.equal(rows[1].pf, null, "a flat-only sample has no defined PF");
  assert.equal(rows[1].flat, true);
  assert.equal(rows[0].n, 0);
  assert.equal(rows[0].pf, null, "an hour without trades is a gap, not a zero");
});

test("a close without a measurable return makes its hour unavailable", () => {
  const closed = [
    { t: H + 1, pnl_pct: null },
    { t: H + 2, pnl_pct: undefined },
    { t: H + 3, pnl_pct: 0.0036 },
  ] as never[];
  const last = hourlyPf(closed, 0.12, NOW, 1)[0];
  assert.equal(last.n, 1);
  assert.equal(last.missing, 2);
  assert.equal(last.pf, null);
  assert.equal(last.noLoss, false, "unknown returns cannot certify no losses");
});

test("no-loss, high finite PF, and future closes remain distinct", () => {
  const noLoss = hourlyPf([{ t: H + 1, pnl_pct: .01 }] as never[], .12, H + 10, 1)[0];
  assert.equal(noLoss.noLoss, true);
  assert.equal(noLoss.pf, null);
  const finite = hourlyPf([
    { t: H + 1, net_pnl_pct: .01 }, { t: H + 2, net_pnl_pct: -.0001 },
    { t: H + 20, net_pnl_pct: -.5 },
  ] as never[], .12, H + 10, 1)[0];
  assert.equal(finite.n, 2, "the future close cannot affect this snapshot");
  assert.equal(finite.pf, 100, "a large finite PF is not clamped or called no loss");
  assert.equal(finite.noLoss, false);
});

test("millisecond timestamps are read as such", () => {
  const closed = [{ t: (H + 10) * 1000, pnl_pct: 0.0036 }] as never[];
  assert.equal(hourlyPf(closed, 0.12, NOW * 1000, 1)[0].n, 1);
});

test("positive return PF can still lose cash when quantities differ", () => {
  const row = hourlyPf([
    {t:H-3600+1,entry:100,qty:1,net_pnl_pct:.02,fee_source:"reported-fee"},
    {t:H-3600+2,entry:100,qty:10,net_pnl_pct:-.01,fee_source:"reported-fee"},
  ] as never[], .12, NOW, 2)[0];
  assert.equal(row.pf,2);
  assert.equal(row.cashPf,.2);
  assert.equal(row.netCash,-8);
  assert.equal(row.partial,false);
  assert.equal(row.estimated,0);
});

test("partial, empty, flat and incomplete cash hours are not profitable evidence", () => {
  const rows = hourlyPf([
    {t:H-3600+1,entry:100,qty:1,net_pnl_pct:0},
    {t:H+1,net_pnl_pct:.02},
  ] as never[], .12, NOW, 3);
  assert.equal(rows[0].netCash,null);
  assert.equal(rows[1].netCash,0);
  assert.equal(rows[1].flat,true);
  assert.equal(rows[2].netCash,null);
  assert.equal(rows[2].cashMissing,1);
  assert.equal(rows[2].partial,true);
  assert.equal(rows[2].estimated,1);
  assert.deepEqual(hourlyPf([], .12, NaN),[]);
  assert.deepEqual(hourlyPf([], .12, NOW, 1.5),[]);
});

test("clock-hour boundary has one owner and configured cost is charged once", () => {
  const rows = hourlyPf([{t:H,entry:100,qty:2,pnl_pct:.005}] as never[], .12, NOW, 2);
  assert.equal(rows[0].n,0);
  assert.equal(rows[1].n,1);
  assert.ok(Math.abs((rows[1].netCash ?? 0)-.76)<1e-12);
});
