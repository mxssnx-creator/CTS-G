#!/usr/bin/env node
/**
 * Run the TypeScript unit tests through node's type stripping.
 *
 * `--experimental-strip-types` only exists on Node >= 22.6. Older Nodes (CI
 * sandboxes, LTS 20) crash the whole `npm test` with "bad option" before a
 * single assertion runs. On those runtimes the .ts suites are skipped with a
 * clear note instead of failing; the scripts/*.test.mjs suites still run
 * unconditionally. Node 22+ runs everything. On an older Node the run fails unless CTSG_SKIP_TS=1 is set.
 */
import { spawnSync } from "node:child_process";

const [major, minor] = process.versions.node.split(".").map((n) => parseInt(n, 10));
const supportsStripTypes = major > 22 || (major === 22 && minor >= 6);

if (!supportsStripTypes) {
  // A silent skip made `npm test` green with zero desk TS tests. Fail unless the skip is explicit.
  const msg = `[ts-tests] node ${process.versions.node} lacks --experimental-strip-types (needs >= 22.6)`;
  if (process.env.CTSG_SKIP_TS === "1") {
    console.log(`${msg}; CTSG_SKIP_TS=1 set, skipping .ts suites`);
    process.exit(0);
  }
  console.error(`${msg}. Use Node >= 22.6, or set CTSG_SKIP_TS=1 to skip the .ts suites on purpose.`);
  process.exit(1);
}

const files = [
  "src/lib/app-data/app-data.test.ts",
  "src/lib/auth/gate-identity.test.ts",
  "src/lib/poll.test.ts",
  "src/lib/live-stats.test.ts",
];
const run = spawnSync(
  process.execPath,
  ["--experimental-strip-types", "--test", ...files],
  { stdio: "inherit" },
);
if (run.error) {
  console.error(`[ts-tests] failed to launch: ${run.error.message || run.error}`);
  process.exit(127);
}
process.exit(run.status ?? 1);
