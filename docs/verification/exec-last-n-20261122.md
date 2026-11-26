# Independent Last-N evaluation and Exec PF

Each Base Set owns its closed-position history. Its Long, Short and Last
blocks are disjoint and ordered from oldest to newest. The Short and Last
signs are independent. Last sizes 1 through 5 have both signs. Negative
Short reads 4, 8 or 12 positions. Exec also evaluates positive Short at
20, 24, 28 and 30. A window needs every position in its configured blocks.

Each qualified child reads its own predecessor path. A position joins a
child's paper history if the parent windows passed at the position's entry.
The entry can read only closes with an earlier timestamp. Long inheritance
uses the original Base Set's Long value. Independent paths with identical
position memberships share calculations and retain their logical counts.

The decision query keeps the earlier entry-time history that its four stages
can reach, while preserving the original position indices. The replay can
reuse a completed timeframe across thin indicators in the same minute. Each
indicator still receives its own trailing span. Existing history limits also
apply to the x02 test wrapper.

## Reported figures

- Post-Base counts each close once per Set across the qualifying Base lanes.
- Post-Main, Post-Real and Post-Exec use the selected path of each Base Set.
- Each simulated close is net of the configured round-trip cost, deducted
  once from its gross return.
- Executed PF divides gains by absolute losses for settled positions.
  Active positions, fills and protection orders have separate counts.
- Auto PF fits each Set on chronological folds of its own earlier results.
  Its report identifies learned, range-default and step-derived floors.

## Reproduction

`python3 scripts/test_stage_grid.py` checks signed windows, path membership,
strict timestamps, per-Set Auto PF, order attribution and the wide Exec
window. `python3 scripts/benchmark_stage_grid.py` compares the same decisions
against the full historical query and checks the paths and position IDs.

The measured 12,000-close fixture used 66.5 ms and 0.81 MB of traced Python
memory with the bounded query. The full query used 605 ms and 7.97 MB.
Both qualified the same 37 Exec lanes. The grouped thin indicators matched
82,080 separate signal calculations across BTC, ETH and XRP.

The 48-hour market study and its per-Set tables include the data hashes,
selection rules and forward execution results. Window PF can be high even
when the portfolio of closed executions loses after costs. Default selection
uses executed results on held-out market windows.

## Inverse signal comparison

The inverse MACD, EMA and two SAR configurations are available in Settings.
Each inverse source changes its signal before Base and builds its own Long
and Short Sets. In the hourly replay, the fixed four-source portfolio reached
PF 4.00 with 69 closes and PF 1.11 with 425 closes across two 48-hour windows.
Both runs used 48 hours of warmup and a 0.176% round-trip cost model.

The inverse MACD alone reached PF 2.19 with 157 closes at the 120-second
refresh in one window. Its November 20–22 replay reached PF 0.21 with 78
closes. The profile therefore remains an explicit comparison choice.
