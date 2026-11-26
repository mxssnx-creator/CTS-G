# Stable-02 Exec and replay performance — 2026-11-26

## Changes

- Merge per-Set symbol histories with `heapq.merge` and compact symbol provenance, avoiding the large temporary tuple list previously built for every Set.
- Add a summary-only StageGrid query for report aggregates; retain full order streams only for selected candidates, and bounded Real-lane tails for Axis checks.
- Cache Set gate/rank results until their evidence can change (a new close, lookback expiry, or drawdown-time boundary). Refresh only stale Sets and use a bounded process pool for independent rank refreshes.
- Release duplicate shadow-trade arrays after gate evaluation while keeping signals and pause plans needed by execution. Report current and peak memory during replay progress.
- Preserve the legacy scheduled refresh cadence. Do not reuse evidence-bound caching for its ordinary scheduled refreshes.
- Make report-only gate sampling configurable. Long replays can take sparse opening/final stage snapshots while execution and settled P&L stay hourly; hourly gate fields identify the source sample and its age.

## Correctness and runtime checks

The complete `npm test` run passed before the report-cadence change: 775 regression checks, 195 Node checks with 4 skipped, 137 TypeScript checks, and 114 engine checks; typecheck passed. The new report-cadence tests passed 3/3, StageGrid checks passed 30/30, and compact-replay memory checks passed 3/3. The 30-symbol replay's 60/60 gate/rank self-checks matched the native engine. Serial and parallel refresh produced identical one-hour trades.

| Replay | Symbols / forward span | Orders | Exec PF | Net | Runtime / peak RSS |
|---|---:|---:|---:|---:|---:|
| Historical x02-overlay, parallel | 30 / 1 hour | 72 | 1.111 | +3.37% | 1,511.7 s; parent 1,480 MiB, child 1,205 MiB |
| Same replay, prior serial path | 30 / 1 hour | 72 | 1.111 | +3.37% | 2,522 s |
| Historical x02-overlay | 30 / 3 hours | 193 | 0.778 | −26.21% | 1,228.1 s; parent 1,477 MiB, child 1,688 MiB |

The parallel one-hour run was about 40% faster with identical output. The three-hour replay did **not** sustain a high Exec PF: hourly cohorts were PF 0.508, 1.118, and 1.414. This is a clear performance failure, not a pass. Weighted Exec PF was 0.841 over the same 193 orders. A profitable selected-window PF is not evidence of profitable execution.

The overlapping group breakdown flags Flow for follow-up: 83 orders at PF 0.539 and −32.74% net; 58 were Flow/long at PF 0.469. Flow/base contributed 38 orders at PF 0.353. These are diagnostic slices of the same 193-order portfolio, not independent trials; defaults should not be changed from this one short window alone.

The tested configuration reported Base Short 4–16 and Last 1–5, with positive and negative modes. Real and Exec Short covered 4–30 and Last 1–5, also with both signs. Every tested replay used the 48-hour lookback and minimum PF 1.35. Stage counts and gate results were checked for all 13,957 independent Sets.

## Boundaries

These are historical replays using the x02 overlay; they are not a live exchange session. Native x02 session fixtures passed 49/49, but no authenticated x02 exchange order test was available in this run. Live order placement, fills, protection orders, and account-side parity therefore remain unverified here.

The long 20-symbol replay uses 10 days of one-minute bars with a seven-day forward window, 48-hour lookback, and 1.35 minimum PF. Its 60/60 engine self-check passed, and the cached execution walk was running when the workspace disconnected at roughly 36 simulated hours. No seven-day PF result is available. The long run reported stable RSS near 2.47 GiB after a 2.77 GiB peak; hourly rank refreshes recomputed roughly 10k–12k Sets and ranged from about 25 to 390 seconds in the observed interval, so long-run refresh latency remains a performance issue.

Parameter selection must account for trying many variants. The [Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253) paper discusses selection bias from repeated strategy trials, and the [Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) adjusts for multiple testing and non-normal returns. This is why short-window PF gains are reported separately from longer held-out execution results.
