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

## Defaults decided by the owner (2026-10-28)

- The Base gate is always on (the engine ignores `setUseHistoricGate: false`); the default minimum PF is **1.25**
  (`GATE_MIN_PF_DEFAULT`, `setMinPf`, `minPf`, Short Range floor). The volume factor default is the minimum, **0.05**.
- Block and DCA are enabled (DCA auto plan; its Set and Axis options `dcaOnSets` / `dcaOnAxis` stay off, so under the Base
  gate, where every position is a Set position, both add-ons are idle until a position without a Set exists or an option is
  turned on).
- The Axis gates (prev, last, cont, pause) are disabled until a configuration is shown to improve the post-Base result.

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
- **No cross-lane actions on a Set position.** The Block add-on never adds to it, and DCA only through its opt-in switches (no parent lane is
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
  the book cap; Set closes never reach Block's or DCA's scores. With the Base gate on (the standard) every entry is a
  Set position, so Block has nothing to stack on and is idle; it only acts when a position has no Set. Block defaults:
  max stack 6 groups, volume ratio 1 of the original volume (`blockMaxStack`, `blockVolumeRatio`).
- **DCA is an opt-in lane** (off by default: `dcaEnabled`; `dca_engine.DCA_HARD_OFF` is only a build-time kill switch).
  Independent of Block. `dcaAuto` (on by default) calculates the plan from each position's own stop distance (rungs as
  fractions of it, one small rung, exposure ceiling `dcaMaxExposureX`, stop kept at the original level; the measured
  least-harmful shape, `scripts/dca_eval.py`); off = the static lists (`dcaMaxSteps`, distances, multipliers).
  Set positions are added to only with `dcaOnSets`, or `dcaOnAxis` (an Axis is on and the coordinator allows); the Set
  then keeps its own stop and TP at the entry it opened at (`pos.dca_ref`), is scored on its base result, and the DCA
  lane is scored on its own adds (`dca_pct`) and switches itself off when they lose. Measured walk-forward on Base-executed
  positions (8 and 30 days, 20 symbols): every DCA shape lowered or left the PF unchanged and scaled the losses, so it
  stays off until a window shows otherwise.
- **Block volume (`block_engine`).** Additive from the group's original base volume V0: each VALID group (its PF decision
  passes, not paused) adds one partial ratio (`blockVolumeRatio` x V0), so the stack is V0 x (1 + ratio x valid groups),
  at most `blockMaxStack` (6) groups, never a cumulative jump in one order and never computed on the grown volume. The
  k-th valid group must pass the PF threshold of the stack it creates. A stack that was increased stays increased: a
  parent that closes at a net loss keeps the level (`carried`, `top`) and V0, the next parent opens at V0 x (1 + carried
  x ratio) (`Pulse.place`, `BlockBook.parent_scale`), and the group's net accumulates over its parents; once that net is
  back to equal (>= 0) the next parent opens at the base volume. The lane's observed PF, pauses and parent ring persist
  across parents. Checks: `block.*` and `trader.block-*`.

## Indication library and extra packs

- `server/pulse/indication_lib.py`: 37 causal indicator families (trend, breakout, reversion, volume and flow; relations and
  ratios such as higher-high counts, close location, up/down volume, ATR expansion, last-part acceleration), each at ranges
  5, 8 (minimal) and 14, 21 (medial) on the 1m, 5m and 15m series (`<family>_n<range>_t<minutes>`), the three timeframes
  combined (`<family>_c<range>`, at least two agree, none disagrees) and group votes (`g_<group>_<min|med>`). They are in
  `indication_extra.INDICATORS` next to the older ones, so any name works in `extraIndicators` (a vote of the
  indications pack) or in a pack's variant list. The 15m series is limited by the 300-row window (ranges up to about 17).
- Extra packs (`set_engine.EXTRA_PACKS`: trend, breakout, reversion, flow, multi): each its own Sets and its own Base
  validation, switched by `stratTrend` etc. (off by default), indicator names by `trendVariants` etc. They exist in the Set
  engine and the evaluation scripts (`scripts/indicator_postbase.py`: effective PF, validated share and orders per pack and
  per variant, development window only, `--sealed` once for finalists). The live trader does not open positions from them.
- Measured so far (8 and 30 day walk-forward, 20 symbols): every single indicator has an unfiltered PF of 0.4 to 0.65; the
  effective PF after Base reads 1.3 to 1.7 in-sample and the executed PF of the walk-forward stays under 1 (best 0.87 on 459
  orders). Choose on development windows; do not read these as an edge until an executed PF above 1 holds on the sealed window.

## Live safety

- x01 runs real money. The drawdown halt is a protection: never clear it or reset the start equity without an explicit
  instruction. It is the overlay setting `ddHaltPct` (percent of the start equity, default 18, 0 = off). On 2026-10-28
  the owner reset x01's start equity and switched its halt off (`ddHaltPct` 0) and set x01 to the minimum order size
  (`sizePctOfBalance` 0, `targetNotional` 0.2); the volume factor is the minimum, 0.05 (owner, later the same day)); the minimum-equity guard (`EQ_MIN`) stays on. The same day x01 took
  x02's settings (its overlay is x02's plus those three keys and `symbolCap` 50). The Base gate
  (`setUseHistoricGate` true) is the default on every connection: entries open only under a Base-validated Set. (x02 ran
  with it off from 01e842b, 2026-10-21, until 2026-10-28; switched back on by the owner.) Credentials are never printed or stored in the repo; rotate any that appeared in a session.
- State files of the live engine live in the pulse directory; tests must use private temp paths.

## Checks

`python3 -B scripts/regression/run.py` (all green), `python3 -B scripts/regression/mutate.py` (no survivors),
`npm run typecheck`, `npm test`. A new check must fail on the previous code.
