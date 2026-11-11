import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_BLOCK_ROUNDS, blockRoundsMax, blockRoundsOf } from "./block-rounds.ts";

test("block rounds: an absent key is the shipped default, an object follows the server's parsing", () => {
  assert.deepEqual(blockRoundsOf(undefined), DEFAULT_BLOCK_ROUNDS);
  assert.deepEqual(blockRoundsOf(DEFAULT_BLOCK_ROUNDS), DEFAULT_BLOCK_ROUNDS);
  const c = blockRoundsOf({ enabled: "yes", relations: ["nope", "symbol"], maxMult: 99, counts: { "2": { on: true, ratio: 9, step: true, maxLevel: 40, pause: -3 }, "3": { ratio: 1 } } });
  assert.equal(c.enabled, false);
  assert.deepEqual(c.relations, ["symbol"]);
  assert.equal(c.maxMult, 5);
  assert.deepEqual(c.counts["2"], { on: true, ratio: 2, step: true, maxLevel: 6, pause: 0 });
  assert.equal(c.counts["3"].on, false);
  assert.equal(c.counts["1"].ratio, 0);
});

test("block rounds: the largest multiple is bounded by the ceiling and is 1 when off", () => {
  assert.equal(blockRoundsMax(DEFAULT_BLOCK_ROUNDS), 3);
  assert.equal(blockRoundsMax({ ...DEFAULT_BLOCK_ROUNDS, maxMult: 2 }), 2);
  assert.equal(blockRoundsMax({ ...DEFAULT_BLOCK_ROUNDS, enabled: false }), 1);
});
