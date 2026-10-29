# CTS-G: standards for every session

## Post-Base evaluation is the standard (read this first)

A Set (one grid configuration: pack x SL ratio x TP step, or pack x trailing pair) is judged on its own tape by the Base
validation: PF over its last `setGateWindow` orders at or above `setMinPf`, a drawdown time under `setMaxDdtHours` (default 24 h), and,
when `setGateMinTrades` is above 0, at least that many closed orders (0 = off, the default: a Set is judged on the orders it has; a
Set without any closed order is never judged). Only Sets that pass are tradeable.

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
- **Read the effective PF next to the executed PF.** The effective PF is measured on the tapes the Sets were selected on, so
  it is in-sample: tightening the gate (higher minimum PF, shorter windows, a consistency floor) pushes it from 1.5 to over 10
  while the walk-forward executed PF of the validated Sets stays at 0.6 to 0.7 (30 days, 20 symbols, `scripts/gate_eval.py`).
  `scripts/stage_eval.py`, `axis_eval.py`, `gate_eval.py` and `indicator_postbase.py` report both; do not raise a gate to
  chase the effective PF.

## Defaults decided by the owner (2026-10-28)

- **2026-10-29: the minimum order count is off and the maximum drawdown time is 24 h.** `setGateMinTrades` 0 (engine `GATE_MIN_DEFAULT`, the
  four shipped overlays, the desk): no "n<min" rejection beyond a Set with no closed order, the Base gate judges a Set on the orders it
  has. `setMaxDdtHours` 24 (`GATE_MAX_DDT_HOURS_DEFAULT`, the overlays, the desk; settable 2-36 h in steps of 2). Read the cost of this
  next to the executed PF: a Set with a handful of orders can pass on a lucky stretch, so the effective PF is more in-sample than before
  and the validated share is higher (checks `gates.minimum-order-count-off-...`, `gates.default-max-drawdown-time-is-24-hours`,
  `processing.few-order-sets-judged-when-the-minimum-is-off`). Measured (2 days, 20 symbols, development window, Base 30, Main off / 8,
  `scripts/window_eval.py --patch`, minimum 25 / off x drawdown time 12 / 24 h): the dense 2-day tapes give every Set hundreds of orders
  and drawdowns of minutes, so neither setting binds there: recent small caps identical in all four cells (valid 28.3 % / 14.0 %, effective
  PF 1.98 / 2.31, executed PF 0.94 on 1,829 orders / 0.95 on 1,802), majors the same except the minimum off with Main 8 (valid 9.28 -> 9.33 %,
  executed PF 0.61 on 743 orders in both). The effect belongs to the live book (sparse orders per Set: on x02 220 of 988 Sets were
  "n<min", 152 on drawdown time at 12 h); read it there, next to the executed PF.

- The Base gate is always on (the engine ignores `setUseHistoricGate: false`); the default minimum PF is **1.25**
  (`GATE_MIN_PF_DEFAULT`, `setMinPf`, `minPf`, Short Range floor). The volume factor default is the minimum, **0.05**.
- Block and DCA are enabled (DCA auto plan; its Set and Axis options `dcaOnSets` / `dcaOnAxis` stay off, so under the Base
  gate, where every position is a Set position, both add-ons are idle until a position without a Set exists or an option is
  turned on).
- The Axis gates (prev, last, cont, pause) are disabled. Measured (30-day walk-forward, `scripts/axis_eval.py`): gating on the
  last 6 closes of the unfiltered stream at PF 1.0 lifts the executed PF from 0.645 to 0.705 and cuts the loss by a third, still
  under 1. A gate that reads only the positions it let through never reopens (no new closes), so the Axis must read a paper stream.

## Units and defaults

- PositionCost is in percent, default 0.12 (`position_cost.POSITION_COST_PCT_DEFAULT`, both overlays, desk default).
- One step = one PositionCost. TP = step x PositionCost; SL = SL ratio x TP with ratios 0.5..3.5 step 0.25.
- Overall minimum stop = PositionCost + `slMinSteps` steps (default 1, so 2 x cost); minimum trailing distance =
  PositionCost + `trailMinSteps` steps (default 2, so 3 x cost).
- Short Range strategy (pack `shortrange`, overlay `stratShortRange`): the Low-Range signals, TP steps 2-6.

## Live entries = replay entries (standard)

- Under the Base gate every live entry is the replay's: `SetBook.entry_signal` reads the signal of the minute that just
  completed on the Set history (`bar_signal`, the per-bar rule `pack_signals` uses; the pack with the highest confidence at or
  above `setEntryConf`), once per symbol and minute, within `ENTRY_FRESH_S` (30 s) of that minute's close
  (`Pulse.set_entry_candidates`). Only completed minutes reach the Set history (`Pulse._closed_klines`; the venue's last row
  is the minute still forming). The live 60-bar indication and general scores (outbreak, regime, forming bar) never open a
  Set position: they traded entries no Set was validated on. Checks `trader.live-entry-is-the-replays-signal`,
  `trader.only-completed-minutes-reach-set-history`, with mutants.

## Exit Sets (exitIgnoreTp as its own strategy type)

- With `exitIgnoreTp` on (the default) every base stop (SL ratio x TP step, one Set per distinct stop distance: 182 per
  pack) also runs as an Exit Set (`kind` exit, id `<pack>:1m:sl<r>:st<n>:xt`): no TP exit, leaves by its stop, the time
  exit or the scratch exit, with its own lanes, tape and Base validation. Base Sets always take their TP. `pick_any` ranks
  the valid base and Exit Sets of a pack together (`rank_key`), then the trailing Sets. Default grid: 754 base + 364 Exit +
  420 trailing = 1538 Sets. Client ids mark an Exit Set in the trail field (`CID_EXIT_TR`), so it never decodes to its twin.
  Desk: Sets overview type "Exit".

## Measured edge (30 days, 20 symbols, development window, `scripts/trade_sim.py`)

- Every Set group's average gross result per order is at most +0.034 % (indications trail +0.025 %, Exit +0.014 %, base
  +0.008 %, general about 0) against a PositionCost of 0.12 %. Effective PF after Base 2.33 (in-sample, 15 % validated);
  the validated Sets' next 8 orders PF 0.46, next 50 orders 0.50 (below the ungated average of about 0.58: a Set passes
  at the end of a lucky stretch); executed (validated per order) 0.61 on 9,993 orders, 61 of 199 blocks of 50 orders at PF
  1 or more; the last 12 h on 12 symbols 0.43 on 130 orders (`reports/trade_sim_12h_12sym.txt`); no type,
  step or SL ratio above 1 on its next orders. No gate or ranking can carry a PF above 1 out of sample while the signals'
  gross move per order is under the cost: an edge has to come from the signals or the exits. `trade_sim.py` reports per
  type and range, in orders (never hours); `reports/trade_sim_dev.txt` holds the full listing.

## Independent lanes (standard)

Every strategy type runs its own lane; none may close, tighten or score another's positions.

- **Set lane.** A position opened under a Set (`pos.set_id`) is closed only by its Set's own rules, the ones the replay
  validated: stop (kept on the exchange), TP by the Set's kind (a base Set always takes its TP, an Exit Set never does, a
  trailing Set follows `setHonorTp` / `exitIgnoreTp`; `set_engine.lane_honors_tp`, replay and live alike), trailing only from an independent trailing Set (`trail_set_id`; a base Set does not trail), the time
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
- **DCA is an opt-in lane** (the lane is enabled by default, `dcaEnabled`, but its Set and Axis options `dcaOnSets` / `dcaOnAxis` are off, so under the Base gate it is idle; `dca_engine.DCA_HARD_OFF` is only a build-time kill switch).
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

## Hold time (per step, per Set, Auto Mode)

- Overlay keys: `holdEnabled` (master, default on), `holdAuto` (Auto Mode, default on), `holdAutoMax` (orders judged, at most 50, default
  50), `holdAutoMin` (orders needed, default 10), `stepHoldOn[i]` / `stepHoldMin[i]` (manual hold of step i + 1; desk sliders 1-50 min for
  steps 1-5, 30-350 min for steps 6-12; the engine accepts 1..720). Master off = only the global time stop (`hist_time_bars`).
- Manual: an "on" step leaves every Set of that step on its own time exit after that many minutes. Auto: every Set without a manual step
  hold (pack x range x SL ratio x TP step, or trailing pair, so each indication, coordination, range and step on its own) replays the
  candidates (`HOLD_AUTO_LOW` for steps 1-5, `HOLD_AUTO_HIGH` for steps 6-12) next to the global stop on the same bars and keeps the one
  with the best cost PF over its last orders (as many as it has, at most `holdAutoMax`; a candidate needs `holdAutoMin` orders and
  `HOLD_AUTO_MARGIN` x the global stop's PF). `SetBook._choose_hold`; the Set is validated on the tape of its chosen hold; live reads
  `st.hold_bars` (`Pulse.manage_set_lane`), the same bar as the replay (`trader.auto-hold-same-in-replay-and-live`).
- The choice is made on the orders the Set is then judged on, so the effective PF after Base is in-sample (higher than the executed PF).
  Measured results: `scripts/hold_eval.py` (fixed hold per step), `scripts/hold_auto_eval.py` (Auto Mode against the global stop, both
  PFs, development window; `--sealed` once). Do not read a better effective PF as an edge without the executed PF.
- Validation windows (`position_cost.window_n`, `MIN_VALIDATION_N` = 8): with `windowsLinked` (default on) `pfWindow`, `setPfWindow`,
  `exitPfWindow`, `slToTpRecalcN` and `trailRecalcN` follow the overall validation's last N (`setGateWindow`, 50); unlinked each is its
  own value, at least 8 (shipped 8). The floor of 8 applies to these windows and the hold Auto sample floor only (the Base gate's minimum order
  count has no floor since 2026-10-29: 0 = off). Other counts keep their own ranges; the defaults of `mainEvalPosCount` (20) and `realEvalPosCount` (15, owner, 2026-10-28), and of `prevPosMinCount`,
  `exitMinSamples` and Block's `blockMinSamples` (8) (Block's `blockWindow` 25). Block counts its own samples (overlay `blockMinSamples`,
  `blockWindow`), independent of the coordinator's `prevPos*` counts and of the validation windows (`trader.block-samples-own-...`).
- Measured (2 days, 20 symbols, development window = first 2/3; `scripts/window_eval.py`, extra last-N PF check on top of the Base gate; "none" =
  the Base gate alone, all windows at the overall N): majors executed PF none 0.40, N = 6/8/12/15/20/25: 0.42/0.43/0.41/0.42/0.41/0.43;
  recent small caps none 1.20, N = 6/8/12/15/20/25: 1.19/1.15/1.07/1.12/1.08/1.10 (sealed third, read once: none vs 8, majors 1.54 vs 1.62,
  small caps 0.98 vs 1.01). `scripts/sim12h.py --counts N` (12 h, 20 symbols, windows + Axis last-N + Block evidence at N): executed PF
  as before / 8 / 12 / 15 / 20 / 25: majors 1.08/1.14/1.04/1.04/1.44/1.13, small caps 0.99/1.03/1.03/1.09/1.01/0.92. No N wins in both
  universes and the differences are inside the noise of one 12-hour window, so the shipped default is the minimum, 8 (windows linked to
  the overall N). Do not tune these on a 12-hour window.
- Base gate last N (`setGateWindow`, 2 days, 20 symbols, `window_eval.py --gate-window`, Base gate alone; N = 50 / 25 / 30 / 35): development
  executed PF majors 0.40/0.53/0.47/0.56, small caps 1.20/1.08/1.19/1.14; validated share 9.5/16.2/14.5/12.7 % and 27.1/33.5/31.4/29.1 %.
  Sealed third (read once for 50, 30, 35): majors 1.54/1.08/1.18, small caps 0.98/1.10/0.98. 12 h sim (`sim12h.py --overlay`): majors
  1.08/0.99/1.01/0.94, small caps 0.99/1.19/1.06/1.02. The development ranking flips on the sealed window and between universes, so the
  default stays 50 (shorter windows validate more Sets and read higher in-sample, not a better executed result).

## Stages: Base -> Main -> Real, each stage's orders a new Set (core logic, owner 2026-10-28)

- **Base stage** (coordinations, ranges, types: pack x SL ratio x TP step, Exit Set, trailing pair), judged on the Set's own tape with
  **two windows, both must pass** at the minimum PF: the long window `setGateWindow` (50) and the short window `setGateShortN` (default
  15, 0 = off; tested 5-25 step 5, `scripts/trade_sim.py --patch`), plus minimum orders and drawdown time. Reason `short` for the short one. A third Base window, the
  **minimal** window (`setGateMinN` 0 = off, 1-3; `setGateMinMode` positive / negative; reason `minimal`): the newest N results of the Set's own tape (net after
  PositionCost) must all be positive (or all negative); judged on the results it has unless `stageFullWindow`; at most the short window; part of the chain
  (`set_engine.minimal_ok`), the simulator (`stage_eval.gate_base`) and the desk Settings (`gates.base-minimal-window-...`, `set-base-minimal-window-equals-the-rules`,
  `processing.sim-gate-equals-the-engine-for-every-window-configuration`, `base minimal:` mutants). The short window
  is at most `setGateShortMaxRatio` (0.5, owner 2026-10-29, desk Settings) of the long window: long 40 allows at most 20, long 20 at most 10 (`gates.short-window-is-at-most-half-of-the-long-window`, `short window:` mutants).
- **Main stage** (additional strategies, coordinations, processings; **the last-N validation sits here**): the Base Set's **Main Set**
  (`<id>:main`) holds the orders it opened while Base passed (both windows, each judged on the Set's own closes before that entry). Two
  windows on the Main Set, both must pass: static `mainGateN` (25, owner 2026-10-29, reason `main`) and Auto (`mainAutoEnabled`: each Set picks from
  `mainAutoWindows` 5/10/15 (owner 2026-10-29) the window whose past verdicts were followed by the best PF on its own Main Set; a candidate needs
  `mainAutoMin` (8) orders and `mainAutoMargin` (1.02) x the static result, like `_choose_hold`; `st.main_auto_n`, reason `mainauto`). The Auto windows are at
  most `mainAutoMaxRatio` (0.5, owner 2026-10-29, desk Settings) of the static window, the same rule as the Base short window: static 25 keeps 5 and 10, drops 15
  (`gates.main-auto-windows-are-at-most-half-of-the-static-window`, `main auto:` mutants).
- **Main Negative lane** (separate, `mainNegEnabled`): the average net of the last 5 or the last 10 Main-Set orders (`mainNegWindows`) is
  negative. Its orders form the **Real Negative Set** (`<id>:realneg`), with its own Real validation, stats and rank.
- **Real stage** (evals and overall strategies): the **Real Set** (`<id>:real`) holds the Main-Set orders opened while both Main windows
  passed. One window, the last `realGateN` (15, `realGateEnabled`, reason `real`) of the lane's Real Set at the minimum PF; the Real
  Negative Set is judged the same way on its own orders. The overall strategies (coordinator real stage) read the Real Sets' pooled
  paper stream (`SetBook.real_paper`, `realStagePaper`).
- **Active execution**: `pick_any` takes only Sets valid through Real on a lane (`SetBook.lane_of`), the positive lane first, then the
  Real Negative lane; the position carries its lane (`pos.lane`, client id nonce marker `CID_NEG_MARK`), a live close joins only its own
  lane's Real Set, and orders mirror to the exchange through the usual order path. `stagesEnabled` off = the Base long window alone.
- **Stage windows judge the orders their lane has** (2026-10-29, `stageFullWindow` false, the default; true = the strict rule): the Base short
  window, the Main static and Auto windows, Real and Real Negative each judge the newest N orders of their lane, at least one, at the minimum PF.
  The strict rule (a window must hold its full N) was a hidden minimum order count: a Set needed 25 Main orders before its first Real order and
  15 Real orders after, so on a sparse live book nothing reached Real. Measured on x02 (28 Oct, 1,234 configured Sets): 725 passed Base, 558 stopped at
  Main, 449 of those for lack of orders (median 18 Main orders in the 32 h lookback, 172 of them at PF 1.25 or more on the orders they had); of the 109 with
  25 or more, none reached 1.25 (median Main PF 0.94). On dense tapes (20 symbols, 24 h) the two rules differ by under 0.02 executed PF. Real last N and
  the Negative windows go down to 1 (`realGateN` 1..200, `mainNegWindows` such as 1,2; the Main static window keeps its floor of 8).
  Checks `set-stage-windows-judge-the-orders-available`, `gates.stage-short-window-...`, `gates.stage-windows-accept-one-and-two`, `stage windows:` mutants.
- **Stage figures** (`pfBase.stages`, desk "Stages" card, coverage strip): per stage (Base, Main, Real, Real Negative, valid on any lane) the Sets that
  pass it, their share of all configured Sets, the pooled PF of those Sets' window orders and the mean of their own PFs. Never an average over all Sets
  (`processing.stage-figures-average-the-valid-sets-...`, `stats stages:` mutants).
- Measured, stage audit (`scripts/stage_audit.py`, 20 live-universe symbols, fresh 24 h test after 24 h pre-history, Base 50/15, Main 25 + Auto, Real 15;
  `reports/stage_audit_24h.txt`): PF of the lanes in the test window, Base Sets (whole tape) 0.855 on 2.39 M orders, Main Sets 0.892 on 221,560, Real Sets
  1.000 on 56,627, Real Negative Sets 0.887 on 131,966. By type (Base / Main / Real): trailing 0.894 / 1.032 / 1.254, exit 0.927 / 0.921 / 0.969, base (TP)
  0.797 / 0.769 / 0.770; by category general 0.911 / 0.996 / 1.090, indications 0.816 / 0.787 / 0.853; by step range 2-6 0.615 / 0.889 / 0.997, 7-12 0.843 /
  0.915 / 1.043, 13-20 0.885 / 0.925 / 1.085, 21-30 0.906 / 0.857 / 0.919. The Sets valid at an hour start read 1.64 in-sample (after Base alone, 270 Sets an
  hour) and 0.88 on their next hour's orders (373,090 orders); after every gate 1.84 against 0.89 (56 Sets an hour, 69,825 orders): the drop after
  selection is the in-sample gap, not a stage fault, and the stages do not close it. Only trailing Sets hold above 1 on their next orders (1.02 after Base,
  1.19 after every gate); base (TP) 0.76 / 0.73 and exit 0.86 / 0.73 fall further after the stages; indications base and exit Sets in 13-30 and general exit 21-30
  are the groups that lose most (0.64-0.78 next hour). Live x02, 485 closes (27-29 Oct): all on the Set lane, none closed by an Exit strategy or the
  coordinator, PF net of cost 1.04.
- Measured, window grid (`scripts/window_grid.py`, report `reports/window_grid_24h.html`, relations `scripts/grid_relations.py`; 20 live-universe symbols, fresh 24 h test after
  24 h pre-history, hold Auto off, Main static 25 + Auto 5/10; 788 cells, each its own independent group of SetBooks; every cell's orders, net and PF agree however they are
  cut): Base long 20/30/40/50 x short 5-25 (at most half the long window) x Base minimal window off / +1 / +2 / +3 / -1 / -2 / -3 x Real last N 1/5/15/25 x Negative
  windows 5,10 / 1,2. Mean executed PF over the cells that carry the value (orders per cell, validated share): minimal off 0.937 (1,603, 4.45 %), +1 0.891, +2 0.908, +3 0.906,
  -1 0.974 (1,620, 1.19 %), -2 0.950 (911), -3 0.855 (465, 0.10 %): requiring the newest result(s) to be negative lifts the mean, requiring them positive lowers it
  (mean reversion, not momentum, on these tapes). Negative windows 1,2 0.916 against 5,10 0.920; Real N 1/5/15/25 0.923 / 0.919 / 0.917 / 0.912 (validated 3.14 / 2.06 /
  1.71 / 1.57 %); Base long 20/30/40/50 0.868 / 0.915 / 0.926 / 0.934 (effective PF 2.02 / 1.80 / 1.66 / 1.60); Base short 5/10/15/20/25 0.918 / 0.913 / 0.927 / 0.923 / 0.897.
  The shipped setting (50 / 15 / 25 / Real 15, Negative 5,10, minimal off) 0.968 on 1,365 orders. Best cells (chosen on the same 24 h, so in-sample for the choice):
  long 50 short 5 Real 5 minimal -2 1.157 on 654 orders (halves 1.165 / 1.103), long 30 short 5 Real 25 minimal -1 1.121 on 1,740 (1.142 / 1.076), long 50 short 25 Real 5
  Negative 1,2 1.099 on 1,629 (1.149 / 1.010); 104 of 755 cells with 150 or more orders reach 1.00. One window, a few hundred to a few thousand orders per cell: a gap of
  0.05 between marginal means is noise, the order of -1 against +1 is the only stable shape; read it on a second window before shipping a minimal window.
- One chain function, `set_engine.stage_chain` (each stage reads only the lane the stage before it built; each lane a subset of the one
  before) and `stage_verdicts`; `SetBook.set_processed` builds it in every replay pass. Snapshot index per Set: `mainId/N/Pf`,
  `realId/N/Pf`, `realNegId/N/Pf`, `shortPf`, `autoN`, `lane`; `pfBase`: `base`, `baseOnly`, `main`, `processed`, `real`, `realneg`,
  `stageSets`; execution stats `sets.stages` (Sets per stage) and `lanes` (open, closed, wins, losses per lane). Desk: Settings "Stages ·
  Base -> Main -> Real", Sets overview columns Main / Real / Real Neg / Short PF · Auto / Lane, stats "Stages". The simulator mirrors the
  chain (`stage_eval.stage_lane`, self-check against the engine). Checks: `set-stage-chain-*`, `set-stage-rules-*`, `set-base-needs-both-windows`,
  `trader.lanes-every-stage-on-its-own-lane`, and the `stage:` mutants.
- Measured earlier (single windows; 30 days, 20 symbols, development window, Base 30/25, Main 8; pooled over every Set): Base Sets PF 0.59
  on 15.3 M orders, Main Sets 0.63 on 3.3 M, Real Sets 0.65 on 1.9 M. Each stage lifts the PF a little; none makes it high or stable while
  the signals' gross move per order is under the PositionCost.

## Validation gates: Base last N, Main gate, shared minimum PF, pre-history, execution tracking

- Every last-N evaluation is per Set (`SetBook._score_one` reads that Set's own tape: pack x SL ratio x TP step, or trailing pair, or Exit Set);
  another Set's orders never move it. The coordinator's `pfWindow` evidence is a strategy-level pool by design.
- **Base last N** (`setGateWindow`) **50** plus the short window `setGateShortN`, its minimum order count `setGateMinTrades` is off (0) since 2026-10-29 (was 30). **Main gate**
  (`mainGateN` 25 plus Auto 5/10/15, at least 8 where a floor applies) and **Real gate** (`realGateN` 15) as in the Stages section; execution picks only
  Sets that pass every stage on a lane (`valid_for`, `pick_any`). `pfBase.base` is the end figure (all gates), `baseOnly` the Base gate alone, `real` the end Sets
  over the Real window, `validatedBasePct` / `validatedPct` the shares. **One minimum PF**: Real, Exit, DCA, Block and the coordinator use `setMinPf`
  (Short Range its own floor) while `minPfLinked` (default on); unlinked, Exit/DCA/coordinator take their own `exitMinPf` / `dcaMinPf` / `minPf`.
- **Pre-history grows** (`histLookbackAuto`, `histLookbackMax` 2880): when a quarter of the Sets that traded have fewer orders than the gate
  minimum (with the minimum off: no closed order at all), the lookback grows by 240 bars after a complete pass (the next fetch pages older minutes); it only grows and survives a reload.
- **Execution tracking** (`Pulse.execution_snapshot`, stats `execution`, desk "Executed · after all gates"): Sets valid after all gates, open
  positions and the Sets they run under (a Set that stopped passing keeps its open positions handled by its own lane until they close; counted
  as `underInvalidSet`), orders closed by lane (Set / Exit, wins, losses, Sets that traded) from the whole trades file plus live closes, closes not
  scored (`skippedNotScored`) and positions that left the book without a record (`droppedNoRecord`). Checks `trader.invalid-set-keeps-open-position-...`,
  `processing.real-gate-per-set-...`, `processing.every-validation-gate-asks-the-base-minimum-pf`, `processing.pre-history-grows-...`.
- Measured (2 days, 20 symbols, `scripts/window_eval.py --base-list ... --arms ...`, development window; 7 Base N x 7 Real N): validated Sets
  fall with a longer Base window (small caps 36.7 % at 10 to 29.1 % at 35, majors 31.9 % to 12.7 %) and again with the Main gate (Base 30: 31.4 %
  alone, 20.2-26.8 % after Real 8-25; majors 14.5 % alone, 10.1-12.8 %). The in-sample effective PF rises as the windows shorten and with the Real
  gate (small caps Base 30: 1.79 alone, 1.90-2.01 with Real); the executed PF does not follow: small caps 1.07-1.22 (best Base 30 / Real 8-10 1.22, Base 25
  / Real 10 1.06), majors 0.35-0.57, no cell above 1 on majors and no consistent winner across universes. Merged engine (Exit Sets), Base 30 Real
  off / 12: small caps dev 0.94 / 0.91, sealed 1.07 / 0.97; majors dev 0.55 / 0.42, sealed 0.80 / 0.82. The Main gate removes a third of the validated
  Sets and carries no measurable out-of-sample gain; it is on by default at N = 8 (owner, 2026-10-28).

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

## Venues

- BingX is the default venue (`bingx-x01` live, `bingx-x02` VST demo). **Bybit V5 linear USDT perpetuals** run through the same
  trader: `PULSE_CONN=bybit-x01` (mainnet, `https://api.bybit.com`) or `bybit-x02` (demo trading, `https://api-demo.bybit.com`,
  public data from mainnet). `server/pulse/bybit_fast.py` (`FastBybit`, selected by `pulse_trader.make_api`) takes the trader's
  BingX calls and returns BingX-shaped answers; nothing in the order logic differs by venue. Order tags are `Gb01` / `Gb02`.
  Hedge mode (positionIdx 1 / 2), conditional reduce-only SL/TP (`triggerDirection` from side and kind), stops move by amend,
  every create is idempotent through `orderLinkId` (a lost reply or a timeout is looked up before it is ever resent).
- Bybit limits (`bybit_limits.py`): 80 requests/s IP pace (venue 600 per 5 s; HTTP 403 holds an IP for ten minutes, so nothing is
  sent for 600 s after one), per-endpoint gates driven by `X-Bapi-Limit*` headers, a batch counts once per item, batches of 10,
  failed items are resent alone, retCode 10002 resyncs the clock from `/v5/market/time`, a breaker holds new entries (never closes
  or cancels) after repeated transient failures. Error classes: `bybit_limits.classify`; the words the trader reads are kept
  in `msg`. Checks: `bybit.*` (stub venue, `scripts/regression/bybit.py`) and the `bybit:` mutants.
- **Not verified against the live venue**: Bybit is geo-blocked from the build host, so the adapter is proven on a stub of the V5
  documentation, not on Bybit. Run `bybit-x02` (demo) first. Credentials: `deploy/bybit-up.sh x01|x02` writes
  `/opt/grok-x01-pulse/cred-bybit-xNN.json` (mode 600: `api_key`, `api_secret`, `is_testnet` "0" live / "1" demo); keys need Unified
  Trading, Orders + Positions (trade) and Wallet (read), no withdrawal, bound to the host IP. The Bybit lanes are separate from
  Overall in the desk and are not started by Overall Start or the watchdog. The drawdown halt ships on (18) in the Bybit overlays.

## Live safety

- x01 runs real money. The drawdown halt is a protection: never clear it or reset the start equity without an explicit
  instruction. **Owner decision (2026-10-28, later): the drawdown halt is 80 % (`ddHaltPct` 80, x01 and x02; the Bybit overlays keep 18) of the equity the
  active session started with (`ddHaltBasis` "session": the first balance this engine process reads, or the one after an explicit Start;
  "start" = the stored start equity), replacing the 18 % / off settings below.** Checks `trader.drawdown-halt-*`. It is the overlay setting `ddHaltPct` (percent of the start equity, default 18, 0 = off). On 2026-10-28
  the owner reset x01's start equity and switched its halt off (`ddHaltPct` 0) and set x01 to the minimum order size
  (`sizePctOfBalance` 0, `targetNotional` 0.2); the volume factor is the minimum, 0.05 (owner, later the same day)); the minimum-equity guard (`EQ_MIN`) stays on. The same day x01 took
  x02's settings (its overlay is x02's plus those three keys and `symbolCap` 50). The Base gate
  (`setUseHistoricGate` true) is the default on every connection: entries open only under a Base-validated Set. (x02 ran
  with it off from 01e842b, 2026-10-21, until 2026-10-28; switched back on by the owner.) Credentials are never printed or stored in the repo; rotate any that appeared in a session.
- State files of the live engine live in the pulse directory; tests must use private temp paths.

## Checks

`python3 -B scripts/regression/run.py` (all green, about 6 min), `python3 -B scripts/regression/mutate.py` (no survivors; each
mutant first runs the check that caught it last time, `scripts/regression/mutant_catchers.json`, and the whole suite only
when that one passes, so a run takes minutes, not hours; `--only PREFIX`, `--full`),
`npm run typecheck`, `npm test`. A new check must fail on the previous code.
