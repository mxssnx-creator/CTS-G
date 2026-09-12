# CTS-G: continuous Base → Real verification

Source checkpoint: `15d50a4`, merged with GitHub main without discarding its commits.
Runtime changes are deployed to the isolated CTS-GX VST lane. Deployment records identify immutable revisions; no monetary X01 orders are initiated by this verification.

Open [cts-g-seven-days.html](cts-g-seven-days.html) for the full parameter matrix, 50 leading observed variants, best result in each symbol/indication/direction group, and training-only choices with their independent holdout results.

The seven-day source window is September 4–11, 2026 UTC. Three validated BingX one-minute OHLCV inputs (BCH/SOL/XRP) are retained in `../7d-simulation-20260911/data`. The runtime history default is separately two days. All 48 symbol/indication/direction groups completed; 7,920 risk configurations × 3,375 admission policies per group = 1,283,040,000 requested alternatives. Equivalent TP-floor parameters share computed price paths but retain all requested config IDs. Their profits must not be added into an account return.

| Symbol | Alternatives | Alternatives with trades | Positive net | Positive in both periods, sufficient samples, Cost-PF >1.05 |
|---|---:|---:|---:|---:|
| BCH-USDT | 427,680,000 | 80,760,830 | 25,664,505 | 4,770 |
| SOL-USDT | 427,680,000 | 13,048,610 | 1,142,474 | 0 |
| XRP-USDT | 427,680,000 | 12,462,715 | 5,897,423 | 28 |

No winner selected solely on training has at least eight holdout trades with positive total net and Cost-PF above 1.05. Consequently no hindsight-selected best configuration has been promoted to a proven live default. Requested defaults are two days, Base last30, the shared 1.05 floor, axes off, and unlimited logical position/set/symbol counts. Higher configured PF floors apply to all stages; the default floor is strict (>1.05).

## Functional verification

- Full Python discovery, including durable SQLite crash recovery and Redis Lua tests: 319/319.
- Engine integration suite: 409/409.
- Nine continuous regressions: 700 independently scored normal/trailing sets produce exactly 700 adapter-confirmed owned openings, 1,400 quantity-matched TP/SL controls, zero duplicates and zero remaining admission lanes. A valid LONG remains visible despite losing SHORT history. The first live close retains sufficient historical evidence; unchanged content reuses calculations. Retention preserves last75 per direction and every middle history slice publishes qualification.
- Admission regressions: 24/24; performance regressions: 18/18, including reuse of the exact published snapshot for periodic exports.
- C++ policy kernel compared with an independent Python scalar oracle; cold-start/same-bar causal boundary tested.
- Release contract: 15/15; forced-config checks: 16/16.
- JavaScript/TypeScript suites: 261 passed, four intentional skips. Typecheck and production build pass.
- Focused production browser test confirms the single PF control synchronizes every stage and Base last30 can be saved as last75, including its minimum sample count. DCA/Exit support deactivation after five closes and the same strict PF floor. This check stubs optional external resources and captures the settings POST without exchange access.
- Dev and production settings/dashboard rendered at desktop and mobile sizes, without JavaScript page exceptions. Automated browser smoke reports external Google Fonts and platform extension requests blocked in the workspace; these failures are retained in the raw browser verdicts. They are not represented as a fully clean network smoke test.

## Runtime deployment

The VST-only release deployment script preserves `/var/lib/cts-gx` and leaves the X01 process on its existing code. X02, its HTTP sidecar and the CTS-GX UI use an isolated revision. Runtime checks require `VST_DEMO`; `CTS_VST_ONLY=1` rejects any mainnet endpoint before execution. The separate forced baseline also selects all eligible rows, uses a 1.05 minimum and rechecks the shared PF at admission. Its VST-only switch remains explicit. Logical test order counts above are not claims of 700 BingX exchange positions. Exchange position endpoints aggregate by symbol and hedge side; CTS owns independent quantity-scoped order/control groups.

The CTS-GX UI restart loop was a conflict with CTS-GA on port 3107. CTS-GX now uses its intended free port 3102. Before the change it had over 17,000 restarts; after repair it serves HTTP 200 without restarts.

The unrestricted 37,440-set/574-symbol process exceeded its 3,456 MiB `MemoryHigh` during the second symbol. The kernel recorded 175,722 high-memory throttling events, without an OOM. Available host memory permitted a VST-only adjustment to 4,608 MiB high / 5,120 MiB maximum while preserving at least 2 GiB host headroom. The same process then published the second symbol. `runtime-memory-verification.json` retains the before/after evidence. Initial coverage was still incomplete and no real VST opening had been observed at that checkpoint. Periodic report generation also now reuses the published snapshot instead of rebuilding it under the shared state lock.

CSS compilation previously scanned large research JSON/HTML as candidate class sources. Restricting Tailwind detection to `src` restores compilation (client approximately six seconds). Source-path syntax: [Tailwind documentation](https://tailwindcss.com/docs/detecting-classes-in-source-files#setting-your-base-path).

The Actions workflow could not create any job because job-level `env` referenced `runner.temp`, which is unavailable in that evaluation context. It now uses the isolated Ubuntu job temporary path, and GitHub creates and executes the job. The newly exposed old fixtures were corrected to include installation ownership and explicitly requested evaluation/guard settings. See [GitHub context availability](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#context-availability).

Overall reports now preserve full logical counts even with truncated display rows, honor the configured PF window up to 75, and reject insufficient samples or PF exactly 1.05. An older report cannot silently substitute PF 1.10 or a last15 window.

## Reproduce

```sh
python scripts/test_continuous_real_live.py
python scripts/test_policy_sweep.py
python scripts/engine-test.py
python -m pip install -r scripts/requirements-test.txt
python -m unittest discover -s scripts -p 'test_*.py'
npm test
npm run typecheck
npm run build
python scripts/sweep_seven_days.py --data reports/7d-simulation-20260911/data --output reports/continuous-7d-20260912 --workers 2
```

`summary.json` contains the exact risk and policy definitions, counters, input SHA256 values, selected rows and training choices. Each `.json.gz` group retains complete per-policy aggregate counters, best rows and the hash of every computed result vector. The source signature must match before cached groups can be reused. Temporary `.npz` files and the compiled `.so` are rebuildable and excluded from version control.
