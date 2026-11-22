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
