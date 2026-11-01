import { test } from "node:test";
import assert from "node:assert/strict";
import { evidencePfText, feeRateText, feeRows } from "./evidence-view.ts";

test("finite high PF is distinct from no loss, flat, missing and empty", () => {
  assert.equal(evidencePfText({ pfState: "finite", pfFinite: 1000 }, 2), "1000.0000");
  assert.equal(evidencePfText({ pf: 99, pfState: "no-loss" }, 5), "No observed losses");
  assert.equal(evidencePfText({ pf: 0, pfState: "flat" }, 5), "Flat (PF undefined)");
  assert.equal(evidencePfText({ pf: 99 }, 0), "— (no sample)");
  assert.equal(evidencePfText({ pf: 99 }, 5), "— (evidence unavailable)");
  assert.equal(evidencePfText({ pf: 1000 }, 5), "1000.0000 (legacy)");
  assert.equal(evidencePfText({ pf: 1.2 }, 5), "1.2000 (legacy)");
});

test("fee rates have explicit percent units, reported zero and unknown remain distinct", () => {
  assert.equal(feeRateText(.0005), "0.0500%");
  assert.equal(feeRateText(0), "0.0000%");
  assert.equal(feeRateText(null), "Unknown");
  assert.equal(feeRateText(NaN), "Unknown");
  assert.deepEqual(feeRows(undefined), []);
  assert.deepEqual(feeRows({ symbols: { BTC: { makerRate: 0, takerRate: null, source: "configured-estimate" } } }),
    [{ symbol: "BTC", maker: "0.0000%", taker: "Unknown", source: "configured-estimate" }]);
});
