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
- Stops fill beyond the plan: median 1.4 x the planned stop distance, 90th percentile 2.4 x (18 stops); targets also fill beyond the level (+8.0 points over 7). The replay assumes 1.00 x. **Cost correction:** an earlier version of this record compared against the overlay's positionCostPct 0.176 %; the engine charges its exchange-derived paper cost (`stats.costModel.paperCostPct` 0.11 % = the account's taker 0.05 % round trip + 10 % slippage allowance), and with that figure the cost gap is +0.25 points over 26 closes while the fill gap is -3.7: the replay's costs match live, the fills do not. `live_sim_diff.py --stats` now reads the engine's cost.
- The loss clusters are on trailing Sets with a 1.02 % stop (tr0.80:0.70 / 0.40), above every minimum distance, and on thin symbols: raising `minSlDistancePct` / `microMinSlDistancePct` would not touch them, so no minimum was changed. The side-aware group gate (`groupGateSide`) targets exactly that cluster.
- Foreign activity on the account (market orders without a client id, an unprotected SOPH short that held most of the margin) was seen and left alone.

## Cost audit (2026-11-14, x01 live trades file against the venue)
- Net result = gross move - fees / notional, once: max deviation 7e-18 over 26 closes with fees; cash `pnl` = gross cash - fees exactly (0.0). No second deduction of the position cost, and the fill slippage is in the prices (`costModel.executionSlippage`: "included in actual fill prices; not deducted again").
- Fee source: 22 of 26 closes carry the venue's reported per-fill commission (`reported-fee`), 4 an exchange-rate estimate. The account's commission rate is read from the exchange (`/user/commissionRate`: taker 0.05 %, maker 0.02 %) per symbol and refreshed hourly (`FeeSchedule`).
- Engine-recorded fees equal the income feed's TRADING_FEE per symbol for positions the engine opened and closed (BARD, SHELL, BERA, GANG, TAC, TIA to 1e-5 USDT); differences are positions it did not open (SOPH), positions still open, and closes made while it was down.
