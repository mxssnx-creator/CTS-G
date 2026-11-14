# Live x01 against the walk-forward sim, 2026-11-13 / 14

No account start is implied by this record; x01 was started on the owner's instruction with the key the owner supplied.

## Sim (trade_sim.py, 8 liquid symbols, 3 days of 1m history, 24 h pre-history, development window then the sealed third read once; x01 overlay of 2026-11-14)
| arm | window | Sets | orders | executed PF | net % | hours with orders | hours net > 0 |
|---|---|---|---|---|---|---|---|
| all packs, SL ratios 0.25 / 0.5 / 0.75 | dev | 4266 | 83 | 0.874 | -6.89 | 26 | 9 |
| same | sealed | 4266 | 32 | 0.443 | -17.06 | 12 | 2 |
| without flow and multi | dev | 3026 | 91 | 0.772 | -13.50 | 25 | 9 |
| same | sealed | 3026 | 29 | 0.261 | -20.03 | 12 | 2 |
Removing the two worst-looking packs (flow short 0.58 on 17 orders, multi short 0.32 on 6) lowered the result on both windows: the counterfactual rule holds, nothing was switched off.
Positive groups on the dev window (orders, small): base Sets of the long TP range 1.65 (23), reversion base 1.54 (22), general base 1.63 (13). Negative: exit Sets (normal range 0 of 5), trailing 0.72 (22), flow / multi short.
The executed PF is under 1 on both windows and most hours are net negative: **"every hour positive" is not met by any arm measured.**

## Live (x01, venue income feed via scripts/live_cash_audit.py, closes via scripts/live_sim_diff.py)
- 22:10 - 01:50 UTC: venue realized PnL negative for the first three hours (-0.56 USDT, PF in USDT 0.08, of it -0.27 on a position the engine did not open), positive in the last 1.5 h (+0.14 USDT, PF 2.24; the engine scored +0.05, PF 1.65; the rest was a closed-while-down win and a position it did not open).
- Stops fill beyond the plan: median 1.38 x the planned stop distance, 90th percentile 2.45 x (17 stops); targets 2.4 x median on 6 (above the level). The replay assumes 1.00 x. The configured PositionCost (0.176 %) is higher than the fees paid, so the cost gap favours live (+1.8 points over 24 closes) while the fill gap costs -2.7 points.
- The loss clusters are on trailing Sets with a 1.02 % stop (tr0.80:0.70 / 0.40), above every minimum distance, and on thin symbols: raising `minSlDistancePct` / `microMinSlDistancePct` would not touch them, so no minimum was changed. The side-aware group gate (`groupGateSide`) targets exactly that cluster.
- Foreign activity on the account (market orders without a client id, an unprotected SOPH short that held most of the margin) was seen and left alone.
