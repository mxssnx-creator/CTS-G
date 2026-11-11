# Relation lanes, Axis and Block against "without" — 10/11 November 2026

Owner requests: indications also by type and by step (distance, range) next to the ordinary per-configuration lanes, with volume
ratios for the overall and major lane types, set optimally; progress per relation and lane; correct last-N lanes for every relation;
then "Test, Fix Axis, Block .. make fully work with correct high PF related to without".

Nothing here was run against an account. No connection was started. Every figure is a walk-forward replay on public 1m candles
(read-only), at most three days per run. A selection window that reads well is not evidence of later profit.

## Data and method

- Two independent three-day datasets of the same 20 USDT perpetuals (the 20 largest by 24 h quote volume on 10 November, chosen
  before any replay; a retrospective universe): **W1** 2026-11-07 18:00 .. 11-10 18:00 UTC, **W2** 11-04 18:00 .. 11-07 18:00 UTC.
- The shipped x01 overlay with a 24 h pre-history (`histLookbackBars` 1440): 2,280 Sets, manual step floors, split directions, the
  Negative lane on. Each dataset gives a development window (the first 32 report hours) and a sealed window (the last 16 hours).
  Everything was chosen on the development windows; each sealed window was read once, for the Axis and Block arms only.
- Execution is `stage_eval.execute`: the engine's gate and rank checked on 40 sampled (Set, hour) pairs per run (40 / 40 each run),
  per-order validation, one position per symbol, the rest after a close as live.
- PF is gains over losses of the net results of the executed positions, each position counted once ("unit"). Where a figure is
  volume weighted it says so; the two are never mixed. At 75–400 positions a PF carries about ±0.1.

## 1. Relation lanes (new, `relationLanes`, shipped off)

A relation lane pools what the Sets of one relation delivered **after Base** and judges that pool on its own last-N windows:

| relation | members | lane type |
|---|---|---|
| overall | every Set of one direction | overall |
| indication | every Set of one indication (pack) and direction | major |
| type | ... of one Set type (base / exit / trail) | major |
| range | ... of one named TP range (micro .. wide, "trail") | minor |
| step | ... of one TP step, i.e. one target distance (the arm for trailing Sets) | minor |

Each lane is a chain of new Sets built only from orders that were valid after Base:

- **Relation Set (Main stage)**: the member Sets' Main-Set orders (the orders each member opened while its own Base windows passed),
  one observation per entry (symbol, entry minute): the mean net of that entry's closed member orders. A pack's Sets trade the same
  signals and differ in their exits, so pooled rows count one entry once per member; here it counts once.
- **Relation Real Set**: the entries opened while the Relation Set's Main window passed, read from the closes strictly before each
  entry, judged on its own last `realN` observations.

A lane passes when both windows pass at `minPf`. A passing lane adds its ratio to the volume of a member Set's positive-lane entry:
multiple = 1 + the passing relations' ratios, at most `maxMult`. The raise is rounded down to the lot step, stays under the size
ceiling, the book room, the margin and (strict cap) the budget times the same ratio; when it does not fit, the entry opens at its base
size. A lane never opens, blocks, closes or scores a position, and never changes a Set's verdict. The Real Negative lane is not raised.

Defaults when enabled: Main 25, Real 15, minimum PF 1.1, ratios overall / indication / type 0.25 each, range / step 0 (built and
shown, no raise), ceiling ×2.

### Measured: a raise on passing lanes did not help

The lanes change no entry, so one execution gives the positions and every arm is read on those same positions.

| window (development) | positions | unit PF (lanes off) | weighted PF (shipped ratios) | raised | unit PF raised | unit PF not raised |
|---|---:|---:|---:|---:|---:|---:|
| W1 | 405 | 0.708 | 0.661 | 77 | 0.265 | 0.847 |
| W2 | 219 | 0.522 | 0.526 | 13 | 0.782 | 0.503 |

Seven arms per window (Main 10 / 25 / 50, Real 0 / 5 / 15 / 25, floor 1.02 / 1.1 / 1.25 / 1.5): on W1 every arm is worse than the
lanes off (weighted PF 0.640–0.670 against 0.708; the raised positions read 0.18–0.34 against 0.81–0.92 for the others); on W2
every arm is within 0.025 of the lanes off (6–30 raised positions). Per relation on W1 the positions whose lane passed read
0.15–0.47 against 0.55–0.67 for those whose lane failed, in all five relations; on W2 the overall lane read better when passing
(9 positions) and the others mixed. The sign is not stable between the two windows, so no ratio above 0 is supported.

**Shipped: off.** By the ship rule (PF at or above the baseline on every window) the raise is not enabled on any overlay. The lanes,
their statistics and the setting are in place; one key (`relationLanes.enabled`) switches them on.

### Correctness

- The engine's lanes after a replay equal a plain reading of the same pass (each Set's Main Set from `stage_chain` on the rows the
  engine scored, grouped by spelled-out keys, each verdict recomputed from the rows closed before it): 46 lanes, every figure.
- `lane_state` against the plain reading on 600 random lanes (several members per entry, shared close instants, entries stamped at a
  close instant, both window rules), Real-Set membership compared entry by entry.
- The simulator builds the engine's lanes (`stage_eval.relation_lanes_at`): equal on every lane; in the study the engine path was
  compared at 12 + 9 instants, all equal.
- The same replay with the lanes on and off gives every Set the same verdict, lane, windows and tape signature.
- Cost: about 1.3 s per scoring pass for 228,000 Main orders of 2,280 Sets (338 lanes), nothing when off.

## 2. Axis against no Axis

| arm | W1 dev | W1 sealed | W2 dev | W2 sealed | pooled PF / net % / positions |
|---|---:|---:|---:|---:|---|
| no Axis | 0.916 (390) | 0.775 (221) | 0.504 (226) | 0.865 (76) | 0.759 / −163.1 / 913 |
| x01 all four (prev 12, last 4, cont 8, pause 8) | 0.708 (405) | 0.977 (204) | 0.523 (222) | 0.944 (74) | 0.728 / −185.7 / 905 |
| last 4 only | 0.759 (406) | 0.920 (209) | 0.519 (218) | 0.944 (74) | 0.742 / −173.1 / 907 |
| prev 12 only | 0.908 (390) | 0.797 (219) | 0.478 (228) | 0.883 (75) | 0.753 / −168.6 / 912 |
| cont 8 only | 0.911 (388) | 0.775 (221) | 0.495 (231) | 0.866 (76) | 0.752 / −170.8 / 916 |
| all but last | 0.886 (388) | 0.787 (218) | 0.487 (236) | 0.883 (75) | 0.742 / −178.8 / 917 |

- The Axis as shipped is better than no Axis on three of four windows (+0.20, +0.02, +0.08) and worse on one (−0.21). Pooled it is
  0.03 lower, carried by W1 development. Almost all of the effect is the `last` axis; `pause` at 8 never bound; `prev` and `cont`
  move the result by less than the noise.
- The sign of `last` flips between the development and the sealed part of the same three days. That is the regime dependence the
  earlier audits found: a rule that reads a Set's newest results works in one stretch and against you in the next.
- **No Axis setting is changed.** No axis improves the replay on every window when removed, and none does when added.
- No window reaches PF 1 with or without the Axis.

## 3. Block against the same parents without Block

Parents admitted by the overlay's Axis; Block v2 as shipped (relations symbol + step, three counts three PositionCosts apart, ratio
1.0, the first count observes). Volume-weighted figures (a parent 1, an add its ratio).

| arm | W1 dev | W1 sealed | W2 dev | W2 sealed | adds | adds' net % |
|---|---:|---:|---:|---:|---:|---:|
| without Block | 0.708 | 0.977 | 0.523 | 0.944 | 0 | 0 |
| x01 shipped | 0.693 | 0.969 | 0.516 | 0.909 | 13 / 6 / 2 / 2 | −12.0 / −1.4 / −2.4 / −1.5 |
| symbol only | 0.702 | 0.968 | 0.492 | 0.874 | 36 / 21 / 6 / 4 | −15.3 / −2.0 / −12.4 / −3.1 |
| step only | 0.732 | 0.984 | 0.525 | 0.938 | 21 / 10 / 14 / 4 | +0.7 / +0.8 / −5.4 / −0.3 |
| shipped, ratio 0.5 | 0.700 | 0.973 | 0.519 | 0.926 | 13 / 6 / 2 / 2 | −6.0 / −0.7 / −1.2 / −0.8 |

- The shipped Block lowers the result on **four of four** windows: 23 adds, −17.4 net points in all, the adds' own PF 0.48, 0.81,
  0 and 0. No candidate's adds are net positive over the four windows (the best, step only, −4.3).
- This agrees with the earlier readings (2026-11-04 sealed windows, the 52-case sensitivity of 2026-11-09): an add after a move in
  the parent's favour has not paid on any window read so far.
- **Block is left as the owner switched it on (2026-11-05); it is not profitable on this evidence.** Removing it improves the replay
  on every window, which is the condition the 2026-11-06 rule names for switching a configuration off; the samples are 2–13 adds
  per window, and the switch on a real-money overlay is the owner's. One key: `blockEnabled: false` (or `stratBlock: false`).

## 4. Lane order (no change)

Development windows, without / with the Axis: `pos-neg` (shipped) W1 0.916 / 0.708, W2 0.504 / 0.523; `neg-pos` 0.884 / 0.684 and
0.532 / 0.527; `neg` 0.936 / 0.688 and 0.533 / 0.551; `pos` 0.804 / 0.786 and 0.495 / 0.428. On W1 the positive lane lost (187
positions, PF 0.60) and the Negative lane won (203, PF 1.48) under the shipped order, but the Negative lane does not keep that when
it also takes the positive lane's entries (401–407 positions, 0.87–0.94). No order is better on both windows beyond the noise.

## 5. Defects found and fixed

1. **A Set open on both lanes was always named positive.** `SetBook.lane_of` returned the positive lane whatever `laneOrder` said, so
   with `neg-pos` or `neg` (and no Axis) a position picked for the Real Negative lane was opened, scored, ranked and group-gated as
   a positive-lane position; and when the pack's positive scope was closed while the negative one was open, such a Set was dropped
   instead of taken on the negative lane. `lane_of` now follows the lane order and `lane_in` names the lane still open among the
   allowed ones. The Axis path was not affected (it carries the lane explicitly). Check
   `lane_rank.a-set-open-on-both-lanes-executes-on-the-lane-it-was-picked-for` (fails on the previous code).
2. **Simulator parity under the Axis.** `stage_eval.execute` listed a Set under one lane only, so a Set the Axis refused on the positive
   lane was not tried on the negative lane as the engine tries it. It is now a candidate of every lane it passes.
3. **Simulator Block floor** read the positive lane's Real window for a Negative-lane parent; it reads the parent's own lane window.
4. **`stack_eval.py` started one hour later** than `trade_sim.py` when the pre-history ended exactly on the hour (31 against 32 report
   hours): same formula now.
5. **A check that fails on every fault-injection copy.** `lane_rank.hold-auto-is-off-by-default-...` located `presets/` relative to
   the engine copy under test, so it failed for any mutant and was recorded as the "catcher" of 14 new mutants it does not test.
   It reads the repository root now; all 23 new mutants are caught by the relation, lane and lift checks.

## 6. Statistics added

- **Executed · with against without** (`execution.lift`, desk card): the settled Set parents once without and once with Block adds
  (the base position alone against all the parent's cash, on the original base notional) and once at volume 1 against their relation
  multiple, with the raised parents next to the others. Rebuilt from the trades file at a restart. This is the live counterpart of
  sections 1 and 3: it shows on the account's own closes whether a stack type pays.
- **Relation lanes** table under the Stages card: per relation the lanes built, passing Main, passing both, the pooled PF of every
  observation the windows read, entries, ratio; the passing lanes by name.
- **Stage and lane progress** chart: one point per scoring pass with the share of Sets passing Base / Main / Real / Real Negative
  each on its own line, and the share of the relation lanes that pass. The replay progress line names the passing relation lanes.

## 7. Independent audit of the stages on the newest data

`scripts/main_stage_audit.py`, W1, two 24 h windows: the chain of 120 Sets (73,206 orders) recomputed by a plain reading differs on
0; Real and Real Negative lie inside Main for every Set; 475,994 stop exits and 379,362 target exits sit at the lane distances
(deviation 2.2e-16); every net is its gross minus one PositionCost, once. Selection PF 1.92 at Main entry and 2.27 at Real entry
against 0.65 and 0.52 delivered (Base tape 0.71; shuffled placebo 0.91); the highest decile of the Base window PF (2.69) was
followed by 0.64 on the next 25 orders, deciles 7–8 (0.9–1.1) by 0.75–0.81. Mean gross result per order: base −0.02 %, Exit
−0.14 %, trailing +0.10 % against a cost of 0.176 %. The arithmetic is right; the signals do not carry the cost on these days.

## Reproduce

```bash
python3 scripts/fetch_audit_market.py --out DATA --hours 72 --top-symbols 20
```

```bash
python3 scripts/relation_lane_study.py --data DATA --cache ts.pkl --patch '{"histLookbackBars":1440}' --axis-current
```

```bash
python3 scripts/lane_axis_study.py --data DATA --cache ts.pkl --patch '{"histLookbackBars":1440}'
```

```bash
python3 scripts/stack_eval.py --data DATA --cache stack.pkl --out stack.json --patch '{"histLookbackBars":1440}' --part axis,block2 --block-axis-current
```

Checks: `python3 -B scripts/regression/run.py` 693 pass, 0 fail; `python3 -B scripts/regression/mutate.py --only "relation lanes|lane:|lift:"`
23 mutants, 0 survive; `npm run typecheck`; the desk unit tests 114 pass.
