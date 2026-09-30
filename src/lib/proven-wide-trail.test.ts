import assert from "node:assert/strict";
import { test } from "node:test";
import { PROVEN_WIDE_TRAIL, PROVEN_WIDE_TRAIL_PAIRS } from "./proven-wide-trail.ts";

test("proven wide-trail set is the 24 pairs that cleared the gate", () => {
  assert.equal(PROVEN_WIDE_TRAIL.length, 24);
  assert.deepEqual(
    PROVEN_WIDE_TRAIL_PAIRS,
    PROVEN_WIDE_TRAIL.map((p) => p.pair),
  );
  const ids = new Set(PROVEN_WIDE_TRAIL_PAIRS);
  assert.equal(ids.size, 24);
  for (const p of PROVEN_WIDE_TRAIL) {
    assert.match(p.pair, /^[a-z]+\|[a-z0-9.@-]+$/);
    assert.ok(p.pf >= 1.1);
    assert.ok(p.n >= 12);
    assert.ok(p.netR > 0);
    assert.equal(p.pair.includes("sig-"), false);
  }
});
