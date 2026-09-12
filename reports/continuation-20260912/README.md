# CTS-G VST continuity follow-up

Continues the merged `15d50a4` work from main `8978efb6404f5d34f2e8c51d559be6d6f13a70e8`.

The running VST lane had progressed to 13 owned logical positions, 6 exchange position groups and 35,381,856 historical closes. The initial historical pass was still incomplete. The earlier 11-position observation had 11 protected config pairs and no gaps. These are observed demo counts, not the offline 700-entry regression or monetary mainnet orders.

A 20-second sampling profile found 6.653 sampled seconds in repeated retention sorting on the history thread and 5.837 in unchanged indication metrics on the main thread. See `profile-before.json` and `vst-before.json`; sampling weights are inclusive and must not be added as exclusive CPU time.

The replay now accumulates close columns in bounded rings and materializes retained rows once per direction. It still counts every close and retains enough independent LONG/SHORT evidence for last75. The auxiliary Block/DCA simulation visits only its representative config instead of walking the entire already-vectorized catalog. Inner progress callbacks report the current direction and bar and allow superseded generations to exit promptly.

Indication metrics reuse a cache keyed by full input contents, evaluation window, cost and PF floor. Same-length corrections invalidate immediately. Returned views cannot mutate the cached evidence. The configured overall PF also applies to the indication gate. Unchanged processing lineages preserve the existing snapshot, and the snapshot reuses its already-calculated stage-flow counts.

## Verification

- Complete Python discovery: 324/324; engine suite: 409/409.
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
