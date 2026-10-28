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

## Independent lanes (standard)

Every strategy type runs its own lane; none may close, tighten or score another's positions.

- **Set lane.** A position opened under a Set (`pos.set_id`) is closed only by its Set's own rules, the ones the replay
  validated: stop (kept on the exchange), TP when the lane honours it (`setHonorTp` / `exitIgnoreTp` decide, in the replay
  and live alike), trailing only from an independent trailing Set (`trail_set_id`; a base Set does not trail), the time
  exit after `hist_time_bars`, the scratch exit. Live code: `Pulse.manage_set_lane`. The Exit strategies, the
  coordinator and the default variants never read or move these positions (`security_prices` skips the exit-lane stop
  tightening for them).
- **Exit lane.** The Exit strategies (lock, peak, rev, time, hard) manage only positions that have no Set. They score only
  their own closes (`exits.on_close` and `variants.on_close` get closes without a set_id).
- **Why.** A Set is validated on its own lane; an Exit strategy closing the same position early makes the live result
  something the Base validation never measured (the PF after Base then does not carry over to live).
- **No cross-lane actions on a Set position.** The Block and DCA add-ons never add to it (no parent or DCA lane is
  attached at open), the coordinator's rearrange never swaps it out (`Pulse.rearrange_opens`), and the global 6 h hold
  cap leaves it to its own time exit. Each rule has a `trader.lanes-*` check and a mutant.
- **Live = replay for a Set position.** Its SL and TP come from `set_engine.lane_fracs` (the replay's formula, no overlay
  clamp); a base Set never trails, a trailing Set trails with its own arm and give (the independent trailing pick is for
  positions without a Set); the lane stops at its own level even when the venue stop sits farther out; scratch is judged
  on a completed 1m close after `SetBook.scratch_bars`. The same holds for a position recovered after a restart.
  `scripts/regression/lanes.py` drives the live trader through the replay's bars and requires the same exit (reason, bar,
  price), and the same outcome with every other strategy switched on (differential check).
- **Block stacks on its own lane.** Only positions without a Set; its evidence is the net PF of those positions' closes
  (`Pulse.exit_lane_pf`, at least Block's minimum samples); it adds only to a parent at least one step in profit, within
  the book cap; Set closes never reach Block's or DCA's scores. DCA is hard-off (`dca_engine.DCA_HARD_OFF`) until it is
  re-approved. With the Base gate on (the standard) every entry is a Set position, so Block has nothing to stack on and
  is idle; it only acts when a position has no Set.
- **Block volume (`block_engine`).** Additive from the group's original base volume V0: each VALID group (its PF decision
  passes, not paused) adds one partial ratio (`blockVolumeRatio` x V0), so the stack is V0 x (1 + ratio x valid groups),
  at most `blockMaxStack` (8) groups, never a cumulative jump in one order and never computed on the grown volume. The
  k-th valid group must pass the PF threshold of the stack it creates. A stack that was increased stays increased: a
  parent that closes at a net loss keeps the level (`carried`, `top`) and V0, the next parent opens at V0 x (1 + carried
  x ratio) (`Pulse.place`, `BlockBook.parent_scale`), and the group's net accumulates over its parents; once that net is
  back to equal (>= 0) the next parent opens at the base volume. The lane's observed PF, pauses and parent ring persist
  across parents. Checks: `block.*` and `trader.block-*`.

## Live safety

- x01 runs real money. The drawdown halt is a protection: never clear it or reset the start equity without an explicit
  instruction. It is the overlay setting `ddHaltPct` (percent of the start equity, default 18, 0 = off). On 2026-10-28
  the owner reset x01's start equity and switched its halt off (`ddHaltPct` 0) and set x01 to the minimum order size
  (`sizePctOfBalance` 0, `targetNotional` 0.2); the minimum-equity guard (`EQ_MIN`) stays on. The same day x01 took
  x02's settings (its overlay is x02's plus those three keys and `symbolCap` 50). The Base gate
  (`setUseHistoricGate` true) is the default on every connection: entries open only under a Base-validated Set. (x02 ran
  with it off from 01e842b, 2026-10-21, until 2026-10-28; switched back on by the owner.) Credentials are never printed or stored in the repo; rotate any that appeared in a session.
- State files of the live engine live in the pulse directory; tests must use private temp paths.

## Checks

`python3 -B scripts/regression/run.py` (all green), `python3 -B scripts/regression/mutate.py` (no survivors),
`npm run typecheck`, `npm test`. A new check must fail on the previous code.
