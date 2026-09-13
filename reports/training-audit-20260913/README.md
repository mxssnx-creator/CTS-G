# Training minimum zero and continuous VST · 13 September 2026

The additional fixed training/holdout sample minimum is zero. Every Set retains
its own configured Last-N PF evaluation; an empty tape never qualifies.
Runtime baseline evidence v3 retains up to 80 own training closes and rechecks
the requested Base window without replaying unchanged prices. Available and
requested sample counts, window completeness and costs remain explicit.

Seven-day BCH/SOL/XRP benchmark, 4–11 September 2026 UTC:

| Symbol | Tested alternatives | Net positive | Training-qualified, control off |
|---|---:|---:|---:|
| BCH | 427,680,000 | 28,248,683 | 5,344,685 |
| SOL | 427,680,000 | 1,265,393 | 874,294 |
| XRP | 427,680,000 | 6,917,887 | 670,384 |

Total: 1,283,040,000 alternatives, 36,431,963 net positive and 6,889,363
training-qualified. Independent training selection admits 46,229 requested
configuration IDs (45,118 unique parameter combinations). Of those unique
selections, 1,242 also have five or more observed control closes with positive
control net and CTS cost PF > 1.00. These are alternative simulations, not an
additive portfolio or exchange orders. The previously inspected control period
is exploratory evidence; it does not justify replacing installation defaults
with a hindsight winner. Defaults remain Last-N 30 / deactivation 25; existing
remote user edits PF 1.04 / deactivation 15 are preserved.

All 48 raw trade-result digests equal the preceding control audit. Changing
admission did not change fills, costs, losses or numerical results. The sweep
was started from calculation sources at commit 44cb29e6; run-provenance.json
records exact source hashes. Later runtime/reporting gate changes are separate.
Reproduce the benchmark from that revision using:

```
python scripts/sweep_seven_days.py --data reports/7d-simulation-20260911/data --output reports/training-audit-20260913 --workers 2
```

The HTML contains seven tables with TP/SL/trailing/Block/DCA parameters,
Last-N 5–75 in steps of 5, deactivation 5–25 in steps of 5, DDT 1–9 hours in
steps of 2, recalculation cadence and Main/Real window comparisons. Execution
costs are included; funding, order-book latency and slippage beyond the stated
model are not. Drawdown uses closed results. Baseline-only replay reports
conventional net gross-profit/gross-loss separately from the CTS cost ratio
used by the larger strategy benchmark.

Runtime fixes include cooperative baseline batches, per-Set pending-order
ownership before duplicate checks, removal of additional fixed eight-close
ranking/diagnostic gates, configured DCA/Exit windows, durable history request
acknowledgements, isolated connection artifacts, and protective-order IDs for
baseline configurations outside the indexed catalog. Venue retry deadlines,
confirmed-fill deduplication, margin checks and protective orders remain.
VST uses 20 symbols and zero logical position/config caps. Mainnet preparation
preserves STOP and does not activate real-money trading.

Verification: 410 engine cases, 374 existing independent regression cases,
four additional window/pending-ownership cases, 21 forced-replay tests and
15 release contracts passed locally. The report renders on desktop/mobile
with seven tables, no page overflow or browser errors. See report-browser.json.
