import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const script = fileURLToPath(new URL("./prepare_signal_min_distance.mjs", import.meta.url));
const fixture = () => ({ settings: { cost: 0.0015, signals: { enabled: true, guard: { lastN: 12 },
  minTrail: 0.002, minSl: 0.005, normal: { tp: [0.01] }, trailing: { tp: [0.01] } } },
  wf: { signalValidLastN: 12, gates: { minPf: 1.5 } } });

test("four isolated arms preserve cost and qualification floors", () => {
  const root = mkdtempSync(join(tmpdir(), "signal-min-distance-"));
  const input = join(root, "base.json"), out = join(root, "new");
  const base = fixture();
  writeFileSync(input, JSON.stringify(base));
  const run = spawnSync(process.execPath, [script, input, out], { encoding: "utf8" });
  assert.equal(run.status, 0, run.stderr);
  const load = (id) => JSON.parse(readFileSync(join(out, `${id}.json`)));
  const control = load("control"), last8 = load("last8"), distance = load("distance"), combined = load("last8-distance");
  assert.deepEqual(control.settings, base.settings);
  assert.deepEqual(control.wf, base.wf);
  assert.equal(last8.settings.signals.guard.lastN, 8);
  assert.equal(last8.wf.signalValidLastN, 8);
  assert.equal(last8.settings.signals.minTrail, 0.002);
  assert.equal(distance.settings.signals.guard.lastN, 12);
  assert.equal(distance.wf.signalValidLastN, 12);
  assert.equal(distance.settings.signals.minTrail, 0.003);
  assert.deepEqual(distance.settings.signals.normal.tp, [0.01, 0.015]);
  assert.deepEqual(distance.settings.signals.trailing.tp, [0.01, 0.015]);
  assert.equal(combined.settings.signals.guard.lastN, 8);
  assert.equal(combined.settings.signals.minTrail, 0.003);
  for (const arm of [control, last8, distance, combined]) {
    assert.equal(arm.settings.cost, 0.0015);
    assert.equal(arm.settings.signals.minSl, 0.005);
    assert.equal(arm.wf.gates.minPf, 1.5);
    assert.equal(arm.study.symbols, 8);
    assert.equal(arm.study.historyHours + arm.study.tradingHours, 24);
  }
  assert.notEqual(spawnSync(process.execPath, [script, input, out], { encoding: "utf8" }).status, 0,
    "a new run must not overwrite prior evidence");
});

test("unexpected baseline is refused before producing arms", () => {
  const root = mkdtempSync(join(tmpdir(), "signal-min-distance-invalid-"));
  const base = fixture();
  base.settings.cost = 0.0;
  const input = join(root, "base.json");
  writeFileSync(input, JSON.stringify(base));
  const run = spawnSync(process.execPath, [script, input, join(root, "out")], { encoding: "utf8" });
  assert.notEqual(run.status, 0);
  assert.match(run.stderr, /unexpected baseline/);
});
