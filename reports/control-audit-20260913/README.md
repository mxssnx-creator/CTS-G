# Control off and architecture audit · 13 September 2026

User instruction: `controlMinTrades: 0` is the default. This disables only the
additional chronological holdout admission test. Each exact Set still needs
its own positive training evidence, entry-time Base/Main/Real PF, and the
configured live evaluation windows. Exchange accounting, costs, protective
orders, independent ownership and venue rate limits remain active.

## Reproducible seven-day results

Data: 4–11 September 2026 UTC, BCH/SOL/XRP, one-minute candles plus warmup.
All 48 symbol/indication/direction groups completed: 7,920 requested risk
configuration IDs × 3,375 policies per group = **1,283,040,000 alternatives**.

| Symbol | Net-positive alternatives | Training-qualified, control off | Own training selections | Unique price configurations selected |
|---|---:|---:|---:|---:|
| BCH | 28,248,683 | 2,800,917 | 11,886 | 11,621 |
| SOL | 1,265,393 | 86,181 | 1,849 | 1,819 |
| XRP | 6,917,887 | 76,502 | 575 | 550 |
| Total | 36,431,963 | 2,963,600 | 14,310 | 13,990 |

These are alternative simulations, not an additive portfolio, simultaneous
orders or confirmed exchange trades. The additional training criterion is at
least eight admitted closes, positive net and CTS cost PF > 1.02. Each risk
configuration chooses its own policy solely from qualifying training data;
control values cannot influence that choice. Last-N 5–75/5, deactivation
5–25/5, DDT 1–9h/2h, three recalculation cadences and three Main/Real window
pairs are all included. TP, SL, trailing, Block and DCA details are in HTML.

The optional five-close / PF > 1.00 control admits 13,463 full-search variants;
the former strict eight-close / PF > 1.02 admits 10,032. Among the 13,990 unique
training selections, 198 independently selected configurations also have at
least five control closes, positive control net and control PF > 1.00.
Control-off admission is not proof of a positive control result.

All **48 numerical result digests match the previous run exactly**, including
net-positive counts and the original strict control counts. See
`numeric-parity.json`. No price-path, execution cost, deactivation or causal
entry formula was relaxed to manufacture extra profits. Control sensitivities
reuse this already inspected period; they are not new unseen validation.
Base Last-N 30 and deactivation Last-N 25 remain installation defaults.
Existing remote user settings are preserved during this targeted rollout.

## Architecture findings and fixes

| Boundary | Finding | Correction and evidence |
|---|---|---|
| Independent selection | One group winner hid independent valid configurations; nine legacy winners had negative training. | Each exact risk Set chooses only among its own positive training policies; changing every holdout value leaves that choice unchanged. |
| Control setting | An eight-trade holdout could veto all training candidates. | Default 0 survives UI, save, shared profile, request, worker, report and admission. Optional positive counts retain a positive-net control gate. |
| Hidden control veto | Whole-period PF, drawdown and recent windows included holdout evidence. | With control off, baseline admission uses training-only PF, windows and drawdown. All later results remain reported. |
| Split and terminal accounting | Open baseline positions were discarded at the split/end. | Close at the boundary price with costs; count once, never re-enter on that boundary. Admission uses unrounded evidence. Two open losing trades now both appear in totals. |
| Expensive sweep reuse | Matching code alone could reuse results from changed source candles. | Verify source SHA, period, symbol and rule. Computation dependencies are fingerprinted separately from report formatting. |
| Queued baseline job | The shared runtime consumed the request as normal history work and never executed its baseline calculation. | Execute it on the existing history worker; maintain progress, preserve the main Set book, observe stop/supersession, and keep newer requests queued. |
| Connection evidence | Baseline files used one shared filename. | Versioned, connection-specific files and provenance checks; no X01/X02 evidence borrowing. |
| Rule change and cache | A formerly filtered shortlist could stay filtered after control was disabled. | Reclassify every stored exact Set from its existing scalar training evidence; no candle replay for a threshold-only change. |
| Release configuration | Applying all installation defaults would overwrite newer remote edits. | Targeted rollout preserves explicit settings, changing the requested control default and 20-symbol test selection. |

The baseline model reports conventional net gross-profit/gross-loss PF
(training floor 1.05). The full strategy matrix uses CTS cost PF. These are
separate definitions and are labelled separately. TP/SL control orders are
unrelated to the optional historical control-trade count.

## Verification

- Forced grid: 19/19, including boundary losses and disabled-control independence.
- Engine: 410/410.
- Independent Python suite: 369/369 before the additional deployment-preservation
  regression and stop regression; the added 8-test connection-profile suite and
  5-test forced-queue suite also pass. GitHub CI verifies the final 371 tests.
- JavaScript/TypeScript: 262 passed, four intentional skips, zero failures.
- Release contract: 15/15; Typecheck and production build pass.
- Dev and production browser checks: desktop/mobile, no console errors, no page
  overflow or build divergence. Interactive settings save 0 → 5 → 0 correctly.
- Report checked at 1440px and 390px; seven scrollable tables, no page overflow.
- Existing 700-Set fake-exchange tests cover eventual opening, accepted-order
  idempotency, retry deadlines, ownership and all 1,400 TP/SL controls.

The continuing runtime test universe is 20 symbols. The only restart target is
VST demo X02. Mainnet X01 is prepared while preserving its STOP and PID 0.
Actual retained exchange roundtrip results are separately documented in
`../window-sweep-20260912/`; the historical matrix never relabels them as simulated
wins or seven-day account performance.

## Reproduce

```sh
python3 scripts/sweep_seven_days.py \
  --data reports/7d-simulation-20260911/data \
  --output reports/control-audit-20260913 --workers 2
python3 scripts/sweep_seven_days.py \
  --data reports/7d-simulation-20260911/data \
  --output reports/control-audit-20260913 --report-only
```

Use a separate output directory and `--control-min-trades 5` for an explicitly
enabled control. `summary.json` includes sensitivity counts, selected windows,
TP/SL/trailing/risk parameters and actual net/cost/PF statistics. The 48 gzip
groups retain every training-selected configuration and exact aggregate counts.
