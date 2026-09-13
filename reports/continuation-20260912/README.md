# CTS-G VST continuity follow-up

Continues the merged `15d50a4` work from main `8978efb6404f5d34f2e8c51d559be6d6f13a70e8`.

## 20-symbol test continuation

The user's September 12 update sets **20 symbols** as the VST/test default,
including BCH, SOL and XRP. This preference is recorded in `AGENTS.project.md`,
the X02 overlay and the repeatable VST release helper. Logical position and
config counts remain unlimited. Existing positions retain management when
their symbol leaves the selection.

The continued runtime inspection found a false completion: a 574-symbol pass
had published only five symbols but declared the entire pass complete. A
consumed manual request also incorrectly cancelled automatic runs through the
request cache's fast path. Both defects are corrected. Completion now advances
only after a successful replay and score slice, using the input's watermark,
not a newer price observed after calculation. Failed slices do not starve
later symbols; unfinished symbols are prioritized on retry. New prices and
same-minute corrections remain dirty until calculated. Coverage, processed
symbols and per-slice scored-config counts are distinct. A config generation
invalidates its old completion claims.

Offline verification: 328 tests passed in the complete discovery run, 409/409
engine checks passed, and all three completion regressions passed after the
final request-cache correction. The completion tests exercise 19/20 with a
failed middle symbol, retry only that symbol while new prices arrive, refresh
the changed prefix, skip identical input, and keep data gaps incomplete.
GitHub run `34707108814` passed on `e83b0bb`: 329 Python tests, 409 engine
checks, 15 release contracts and 16 forced-grid checks.

The deployed VST lane selected exactly 20 symbols and completed the first
full pass, then further hourly publications and incremental updates. The
observations include 4/20, 16/20, all 20 published watermarks, and advancing
closed-bar evidence. Around the storage recovery, 1,510 Base, 1,098 Main and
1,029 Real Sets qualified; seven logical positions had seven recorded
protective pairs, matching one owned exchange symbol/side group. A separate
VST REST snapshot confirmed one nonzero position group. These counts have
different meanings: an eligible matrix changes with current signals, and
closed positions and deactivation continue while the queue is processed.

The runtime inspection also exposed a full server filesystem: Redis AOF
writes and cache writes failed with `No space left on device`. Reclaiming
7.36 GB of old, regenerable Node/Jest/npm cache files allowed a regular Redis
background AOF rewrite. The AOF shrank from 59.04 GB to 0.69 GB, leaving
59.27 GB available; both write and rewrite status returned `ok`. No project
data or position state was removed. Redis and the VST process continued
running, and the calculation cache resumed successful writes. Its eight
historical errors are cumulative; its current error message is empty.

The selected symbols, timestamped progress/position/queue/coverage readings,
native REST observation and storage recovery evidence are in
[`vst-20-symbols.json`](vst-20-symbols.json). The accompanying
[`vst-20-symbols.html`](vst-20-symbols.html) distinguishes this 20-symbol
operational test from the exhaustive seven-day BCH/SOL/XRP benchmark.

Before the update the VST lane reported 13 owned logical positions, 6 internal symbol/side groups and 35,381,856 historical closes. These were local book counts, not a contemporaneous independently confirmed exchange position count. The earlier 11-position observation had 11 recorded protected config pairs and no gaps. These demo observations are distinct from the offline 700-entry regression and monetary mainnet orders.

The exchange returned an empty position snapshot shortly before the update. Startup reconciliation then confirmed two empty snapshots and retired the 13 stale local entries. The current account was flat, rather than 13 still-open exchange positions being discarded by a file migration. Later runtime observations include explicit exchange-own/total counts and reconciliation status; internal symbol/side group counts alone cannot prove exchange openings.

A 20-second sampling profile found 6.653 sampled seconds in repeated retention sorting on the history thread and 5.837 in unchanged indication metrics on the main thread. See `profile-before.json` and `vst-before.json`; sampling weights are inclusive and must not be added as exclusive CPU time.

The replay now accumulates close columns in bounded rings and materializes retained rows once per direction. It still counts every close and retains enough independent LONG/SHORT evidence for last75. The auxiliary Block/DCA simulation visits only its representative config instead of walking the entire already-vectorized catalog. Inner progress callbacks report the current direction and bar and allow superseded generations to exit promptly.

Indication metrics reuse a cache keyed by full input contents, evaluation window, cost and PF floor. Same-length corrections invalidate immediately. Returned views cannot mutate the cached evidence. The configured overall PF also applies to the indication gate. Unchanged processing lineages preserve the existing snapshot, and the snapshot reuses its already-calculated stage-flow counts.

A second runtime profile reduced sampled indication-metric time from 5.837 to 0.041 seconds, while exposing duplicate catalog scans in statistics. The final pipeline change shares one catalog snapshot between statistics/coverage and publishes score progress without rebuilding that snapshot. Completed score batches expose accurate done/total/remaining counts. The first completed batch makes fully qualified configs available for admission while the rest continues; exact per-config Base/Main/Real, sample, PF and direction gates still apply. A 32-of-90 regression admits exactly those 32 LONG configs, no SHORTs, and reports 58 remaining; both one-worker and two-worker paths pass.

Before this final pipeline update, the running optimized replay had opened 24 logical config positions with 24 recorded protective pairs; the exchange confirmed 18 owned symbol/side groups. Three local absences were still pending reconciliation. `vst-confirmed-intermediate.json` preserves that distinction and its timestamp. The full 574-symbol initial pass was still in progress.

## Verification

- Complete Python discovery: 326/326; engine suite: 409/409. This includes progress-only publication without a catalog rebuild and early admission from completed scoring batches.
- All 48 seven-day groups were regenerated with the current source signature. The 1,283,040,000 alternatives retain exactly the previous aggregate results: 32,704,402 positive and 4,798 meeting the report's two-period sufficient-sample/PF qualification. No training-selected winner passes holdout; the requested defaults are retained. See [the full HTML report](../continuous-7d-20260912/cts-g-seven-days.html).
- Scalar oracle: exact retained close values/counts for both independent directions, TP on/off, overflowing tapes and Block/DCA representative histories.
- Four isolated old/new replay comparisons have identical SHA256 hashes across all retained rows, complete close counts and auxiliary histories.

| Input | Configs | Bars | Complete closes | Previous seconds | Current seconds | Speedup |
|---|---:|---:|---:|---:|---:|---:|
| BCH | 208 | 2,880 | 39,505 | 0.890 | 0.205 | 4.35× |
| SOL | 208 | 2,880 | 39,602 | 0.880 | 0.228 | 3.86× |
| XRP | 208 | 2,880 | 41,240 | 1.047 | 0.255 | 4.10× |
| Synthetic overflow | 208 | 1,000 | 69,680 | 3.540 | 0.187 | 18.97× |

These benchmarks exclude concurrent live traffic and do not predict whole-service speed. `replay-benchmark.json` retains exact timings, parameter settings and evidence hashes.

```sh
git show 8978efb6404f5d34f2e8c51d559be6d6f13a70e8:server/pulse/set_engine.py > /tmp/cts-g-baseline-set-engine.py
python scripts/benchmark_replay_continuity.py --baseline /tmp/cts-g-baseline-set-engine.py --output reports/continuation-20260912/replay-benchmark.json
python -m unittest discover -s scripts -p 'test_*.py'
python scripts/engine-test.py
```
