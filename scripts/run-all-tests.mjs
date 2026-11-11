#!/usr/bin/env node
/**
 * `npm test`: the Node and TypeScript desk tests and both Python engine suites run concurrently.
 *
 * They share no state, so running them in parallel cuts wall time to the slowest of the three. The exit code is
 * nonzero if any of them fails, and each one's output is printed under its own label.
 */
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const logDirectory = mkdtempSync(join(tmpdir(), "cts-g-tests-"));

const jobs = [
  {label:"local x02 and scheduling",cmd:"python3 -B scripts/test_regression_scheduling.py && python3 -B scripts/test_regression_progress.py && python3 -B scripts/test_control_coverage.py && python3 -B scripts/test_x02_native_session.py"},
  {label:"replay report",cmd:"python3 -B scripts/test_audit_reporting.py && python3 -B scripts/test_strategy_switches.py && python3 -B scripts/test_demo_acceptance_ownership.py && python3 -B scripts/test_indexed_stage_query.py"},
  {label:"Tactic/Inverse/volume",cmd:"python3 scripts/test_ported_indications.py && node --experimental-strip-types --test shared/cts-indications/ported.test.ts shared/cts-volume/ratios.test.ts"},
  { label: "node", cmd: "node --test scripts/*.test.mjs" },
  { label: "ts", cmd: "node scripts/run-ts-tests.mjs" },
  { label: "python", cmd: "python3 scripts/regression/run.py" },
  { label: "engine", cmd: "python3 scripts/engine-test.py" },
];

function run({ label, cmd }) {
  return new Promise((resolve) => {
    const started = Date.now();
    console.log(`Started ${label}`);
    const child = spawn(cmd, { shell: true, stdio: ["ignore", "pipe", "pipe"] });
    const lines = [];
    const collect = (chunk) => {
      for (const line of chunk.toString().split("\n")) if (line) {
        lines.push(line);
        if (/regression (early failure|progress):/.test(line)) console.log(`${label}: ${line}`);
      }
    };
    child.stdout.on("data", collect);
    child.stderr.on("data", collect);
    child.on("close", (code) => {
      const path = join(logDirectory, label.replace(/[^a-z0-9-]+/gi,"-")+".log");
      writeFileSync(path,lines.join("\n")+"\n");
      console.log(`Finished ${label}: ${code === 0 ? "ok" : "FAILED"} (${Math.round((Date.now()-started)/1000)}s); full output ${path}`);
      resolve({ label, code: code ?? 1, lines });
    });
    child.on("error", (err) => resolve({ label, code: 1, lines: [String(err)] }));
  });
}

const results = await Promise.all(jobs.map(run));
let failed = 0;
for (const r of results) {
  const status = r.code === 0 ? "ok" : `FAILED (exit ${r.code})`;
  console.log(`\n=== ${r.label}: ${status}`);
  const tail = r.lines.slice(-12);
  if (r.code !== 0) for (const line of r.lines.filter(line=>/^(FAIL\b|regression early failure:)/.test(line))) {
    if (!tail.includes(line)) console.log(`    ${line}`);
  }
  for (const line of tail) console.log(`    ${line}`);
  if (r.code !== 0) failed += 1;
}
process.exit(failed ? 1 : 0);
