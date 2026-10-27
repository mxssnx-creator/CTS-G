# CTS-G: standards for every session

## Post-Base evaluation is the standard (read this first)

A Set (one grid configuration: pack x SL ratio x TP step, or pack x trailing pair) is judged on its own tape by the Base
validation: at least `setGateMinTrades` closed orders, PF over its last `setGateWindow` orders at or above `setMinPf`,
and a drawdown time under `setMaxDdtHours`. Only Sets that pass are tradeable.

- The number that counts for any strategy, indicator or setting is the **PF after Base validation** ("effective PF",
  `sets.pfBase.base`), together with **how many percent of the Sets are validated** (`validatedPct`, also per pack in
  `byPack`). Quote both, always, with the order count behind them.
- The **unfiltered PF** (every Set before Base) is under 1 by design, because Base exists to drop the losing Sets. Never
  read it as the result, and never optimise it.
- A change is good when the effective PF is high and stable across windows (every hour if possible), the validated share
  is not collapsing, and the order count stays large enough to mean something. A high effective PF on a handful of
  Sets or orders is noise.
- Choose on the development windows only; read the sealed window once, for the finalists. Report the numbers as they
  come out, positive or not. Do not tune on the sealed window.

## Units and defaults

- PositionCost is in percent, default 0.12 (`position_cost.POSITION_COST_PCT_DEFAULT`, both overlays, desk default).
- One step = one PositionCost. TP = step x PositionCost; SL = SL ratio x TP with ratios 0.5..3.5 step 0.25.
- Overall minimum stop = PositionCost + `slMinSteps` steps (default 1, so 2 x cost); minimum trailing distance =
  PositionCost + `trailMinSteps` steps (default 2, so 3 x cost).
- Short Range strategy (pack `shortrange`, overlay `stratShortRange`): the Low-Range signals, TP steps 2-6.

## Live safety

- x01 runs real money. The drawdown halt is a protection: never clear it or reset the start equity without an explicit
  instruction. Credentials are never printed or stored in the repo; rotate any that appeared in a session.
- State files of the live engine live in the pulse directory; tests must use private temp paths.

## Checks

`python3 -B scripts/regression/run.py` (all green), `python3 -B scripts/regression/mutate.py` (no survivors),
`npm run typecheck`, `npm test`. A new check must fail on the previous code.
