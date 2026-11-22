import assert from "node:assert/strict";
import test from "node:test";
import { stageGridOf } from "./stage-grid.ts";

test("stage settings preserve independent sign and inherited Long selections", () => {
  const settings = stageGridOf({
    base: { long: [20, 50, 99, 20], shortModes: ["negative"] },
    main: { last: [1, 3], longModes: ["none"], lastModes: ["negative"] },
    real: { longModes: ["inherit"] },
    exec: { short: [16], shortModes: ["negative"] },
  });
  assert.deepEqual(settings.base.long, [20, 50]);
  assert.deepEqual(settings.base.shortModes, ["negative"]);
  assert.deepEqual(settings.main.longModes, ["none"]);
  assert.deepEqual(settings.main.last, [1, 3]);
  assert.deepEqual(settings.main.lastModes, ["negative"]);
  assert.deepEqual(settings.real.longModes, ["inherit"]);
  assert.deepEqual(settings.exec.shortModes, ["positive"]);
  assert.deepEqual(stageGridOf(settings), settings);
  assert.equal(stageGridOf(false).enabled, false);
});

test("all Exec Short counts are enabled by default, with custom choices preserved", () => {
  const settings = stageGridOf({
    real: { short: [8, 28] },
    exec: { short: [4, 20, 24, 28, 30, 32], shortModes: ["positive", "negative"] },
  });
  assert.deepEqual(settings.real.short, [8]);
  assert.deepEqual(settings.exec.short, [4, 20, 24, 28, 30]);
  assert.deepEqual(settings.exec.shortModes, ["positive", "negative"]);
  assert.deepEqual(stageGridOf({ exec: { short: [] } }).exec.short, [4, 8, 12, 16, 20, 24, 28, 30]);
  assert.deepEqual(stageGridOf({ exec: { short: [4, 8, 12, 16] } }).exec.short, [4, 8, 12, 16]);
});
