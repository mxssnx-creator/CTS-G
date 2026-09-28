import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { laneFallbackRows } from "./stats-fallback.mjs";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

test("sidecar-down lane fallback keeps only that lane's rows", () => {
  const overall = {
    connType: "overall",
    pfCost: { ratio: 9 },
    sets: { validatedCount: 45000 },
    open: [
      { symbol: "BTC-USDT", connection: "bingx-x01", connType: "live" },
      { symbol: "ETH-USDT", connection: "bingx-x02", connType: "vst" },
    ],
    closed: [
      { pnl: 1, connection: "bingx-x01", connType: "live" },
      { pnl: -2, connection: "bingx-x02", connType: "vst" },
      { pnl: 3 },
    ],
  };
  assert.deepEqual(laneFallbackRows(overall, "live", "bingx-x01"), {
    open: [overall.open[0]],
    closed: [overall.closed[0]],
  });
  assert.deepEqual(laneFallbackRows(overall, "vst", "bingx-x02").closed.map((row) => row.pnl), [-2]);
  assert.deepEqual(laneFallbackRows({}, "vst", "bingx-x02"), { open: [], closed: [] });
  assert.deepEqual(laneFallbackRows(null, "live", "bingx-x01"), { open: [], closed: [] });
});

test("vite lane fallbacks never spread the Overall snapshot", () => {
  const source = readFileSync(join(ROOT, "vite.config.ts"), "utf8");
  for (const lane of ["live", "vst"]) {
    const branch = source.match(new RegExp(`if \\(conn === "${lane}"\\) \\{\\s*return \\{([\\s\\S]*?)\\};\\s*\\}`));
    assert.ok(branch, `${lane} fallback branch`);
    assert.doesNotMatch(branch[1], /\.\.\.snap\b/);
    assert.match(branch[1], new RegExp(`laneFallbackRows\\(snap, "${lane}"`));
  }
});
