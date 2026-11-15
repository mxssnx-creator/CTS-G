import { test } from "node:test";
import assert from "node:assert/strict";
import { fmtPf, groupCells, historySeries, packKind, packRows, parseSignalPack, pfTone, signalGrid, splitClosed, stageFunnel } from "./insights.ts";
import type { Execution, LiveClosed, PfBase } from "./live-stats.ts";

const pool = (pf: number, n: number) => ({ pf, n, sets: 1 });

test("signal packs parse and group by family", () => {
  assert.deepEqual(parseSignalPack("sig-ema-cross-short-t5"), { source: "ema-cross", range: "short", tf: 5 });
  assert.deepEqual(parseSignalPack("sig-bb-bounce-medium-t30"), { source: "bb-bounce", range: "medium", tf: 30 });
  assert.equal(parseSignalPack("sig-ema-cross-long-t5"), null);
  assert.equal(packKind("sig-rsi-reversal-short-t1"), "signals");
  assert.equal(packKind("trend"), "signal pack");
  assert.equal(packKind("extra.donch_n21_t5"), "extra");
  assert.equal(packKind("indications"), "indications");
});

test("pack rows carry validated share, PF after Base and open positions, sorted by validated Sets", () => {
  const pfBase = { byPack: {
    a: { base: pool(1.5, 40), unfiltered: pool(0.8, 400), total: 100, valid: 10, validatedPct: 10 },
    b: { base: pool(2.0, 0), unfiltered: pool(0.9, 300), total: 50, valid: 20, validatedPct: 40 },
  } } as unknown as PfBase;
  const exec = { open: { byPack: { a: { open: 3, underInvalidSet: 0 } } } } as unknown as Execution;
  const rows = packRows(pfBase, exec);
  assert.deepEqual(rows.map((r) => r.pack), ["b", "a"]);
  assert.equal(rows[0].basePf, null);               // no orders behind it: no PF
  assert.equal(rows[1].basePf, 1.5);
  assert.equal(rows[1].open, 3);
});

test("group scopes split into pack, kind, range, lane and side, pack names with ':' kept whole", () => {
  const exec = { groups: { enabled: true, n: 3, pf: 1, hours: 6, min: 1, scopes: {
    "group:multi:base:long:pos:S": { orders: 4, pf: 0.5, allow: false },
    "group:extra.ao__x:y:trail:short:neg": { orders: 9, pf: 1.4, allow: true },
  } } } as unknown as Execution;
  const cells = groupCells(exec);
  assert.deepEqual(cells[0], { pack: "extra.ao__x:y", kind: "trail", range: "short", lane: "neg", side: "", orders: 9, pf: 1.4, allow: true });
  assert.deepEqual(cells[1], { pack: "multi", kind: "base", range: "long", lane: "pos", side: "S", orders: 4, pf: 0.5, allow: false });
});

test("closed splits leave unscored closes out and pool the net returns", () => {
  const rows = [
    { reason: "tp", pack: "a", net_pnl_pct: 0.02 }, { reason: "sl", pack: "a", net_pnl_pct: -0.01 },
    { reason: "tp-downtime", pack: "a", net_pnl_pct: 0.05 }, { reason: "sl", pack: "b", pnl_pct: -0.01 },
  ] as unknown as LiveClosed[];
  const out = splitClosed(rows, (r) => r.pack ?? "-");
  const a = out.find((x) => x.key === "a")!;
  assert.equal(a.orders, 2);
  assert.equal(a.wins, 1);
  assert.ok(Math.abs((a.pf ?? 0) - 2) < 1e-12);
  assert.equal(out.find((x) => x.key === "b")!.pf, 0);
});

test("funnel and history read the engine's stage figures; signal grid orders short before medium and by timeframe", () => {
  const st = (sets: number, pf: number, orders: number) => ({ sets, pct: sets, pf, meanPf: pf, n: orders, orders });
  const pfBase = { stages: { base: st(50, 1.5, 100), main: st(20, 2, 40), real: st(10, 2.5, 0), realNeg: st(2, 1.1, 5), valid: st(12, 2.2, 45), allSets: 100 },
    history: [{ t: 1, pf: 2, n: 5, valid: 3, validatedPct: 3, unfiltered: 0.9, stages: { base: { sets: 5, pct: 5, pf: 1.4 } } }] } as unknown as PfBase;
  const f = stageFunnel(pfBase);
  assert.deepEqual(f.map((x) => x.stage), ["base", "main", "real", "realNeg", "valid"]);
  assert.equal(f[2].pf, null);
  const h = historySeries(pfBase);
  assert.equal(h[0].base, 1.4);
  assert.equal(h[0].main, null);
  const g = signalGrid([
    { pack: "sig-ema-cross-medium-t5", kind: "signals", total: 1, valid: 0, validatedPct: 0, basePf: null, baseOrders: 0, unfilteredPf: null, open: 0 },
    { pack: "sig-ema-cross-short-t15", kind: "signals", total: 1, valid: 0, validatedPct: 0, basePf: null, baseOrders: 0, unfilteredPf: null, open: 0 },
    { pack: "sig-donchian-short-t5", kind: "signals", total: 1, valid: 0, validatedPct: 0, basePf: null, baseOrders: 0, unfilteredPf: null, open: 0 },
  ]);
  assert.deepEqual(g.cols, ["short 5m", "short 15m", "medium 5m"]);
  assert.deepEqual(g.sources, ["donchian", "ema-cross"]);
});

test("PF formatting and tone", () => {
  assert.equal(fmtPf(Infinity), "∞");
  assert.equal(fmtPf(null), "–");
  assert.equal(fmtPf(1.234), "1.23");
  assert.equal(pfTone(0.9), "bad");
  assert.equal(pfTone(1.1), "flat");
  assert.equal(pfTone(1.5), "good");
});
