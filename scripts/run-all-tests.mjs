#!/usr/bin/env node
/**
 * Run desk/source-parity checks first, then the heavy Python suites together.
 * This bounds CPU contention without dropping checks. Any failed suite makes
 * the command fail; every suite keeps its complete diagnostic output.
 */
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const logDirectory = mkdtempSync(join(tmpdir(), "cts-g-tests-"));

const jobs = [
  {label:"local x02 and scheduling",cmd:"python3 -B scripts/test_regression_scheduling.py && python3 -B scripts/test_regression_progress.py && python3 -B scripts/test_bounded_replay_build.py && python3 -B scripts/test_control_coverage.py && python3 -B scripts/test_native_cleanup_evidence.py && python3 -B scripts/test_x02_native_session.py && python3 -B scripts/test_x02_universe.py && python3 -B scripts/test_micro_distance.py"},
  {label:"replay report",cmd:"python3 -B scripts/test_audit_reporting.py && python3 -B scripts/test_strategy_switches.py && python3 -B scripts/test_demo_acceptance_ownership.py && python3 -B scripts/test_indexed_stage_query.py && python3 -B scripts/test_replay_memo.py"},
  {label:"Tactic/Inverse/volume",cmd:"python3 -B scripts/test_venue_fill_fees.py && python3 scripts/test_ported_indications.py && node --experimental-strip-types --test shared/cts-indications/ported.test.ts shared/cts-volume/ratios.test.ts"},
  {label:"native process replay",cmd:"python3 -B scripts/test_parallel_native_replay.py && python3 -B scripts/test_bingx_signed_pool.py && python3 -B scripts/test_native_fill_audit.py && python3 -B scripts/test_position_settlement.py && python3 -B scripts/test_compact_replay_memory.py && python3 -B scripts/test_indication_feature_cache.py"},
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

// Start source-parity checks before the four-process replay suite. Launching
// every CPU-heavy child together could starve the TypeScript bridge past its
// computation deadline and turn available indications into timeout results.
const sourceParity=jobs.filter(job=>job.label==='Tactic/Inverse/volume');
const lightweight=jobs.filter(job=>!['python','engine','Tactic/Inverse/volume'].includes(job.label));
const heavyweight=jobs.filter(job=>['python','engine'].includes(job.label));
const results = await Promise.all(sourceParity.map(run));
results.push(...await Promise.all(lightweight.map(run)));
results.push(...await Promise.all(heavyweight.map(run)));
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
