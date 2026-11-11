# Block coordinations: what an add is worth, and what was changed — 11 November 2026

Owner: "Make Block fully effectively working with high PF as without and change, try different coordinations completely until its working" /
"Check the Block structure in general ... independent for each block count with ratios and correct stacking, for each Set and Overall, for the
active orders".

No account, no connection. Walk-forward replays on public 1m candles (read-only), at most three days per run, the x01 overlay with a 24 h
pre-history, parents admitted by the overlay's Axis, one position per symbol. The same 20 symbols throughout (chosen on 10 November's volume: a
retrospective universe). Volume-weighted figures: a parent counts 1, an add its ratio.

## Result in one paragraph

No Block coordination makes the adds pay out of sample. 11,704 coordinations were swept on four windows; 25 were at or above "without" on all
four; on three windows no rule had been chosen on, 24 of them failed and the one that "held" made 3 adds. The reason is structural and was
measured directly: an add's result follows its parent's. Adds on a type of parent paid in exactly the windows where that type of parent was
itself profitable, and the executed parents were below PF 1 on all nine windows. Block is leverage on the parents plus a second cost; it cannot
earn what the parents do not. What was changed therefore does not make Block profitable: it stops Block from costing more than one losing
episode. Over 13 days with the book carried from window to window the earlier Block lost 22.1 net points on 17 adds (PF 0.639 against 0.648
without); the Block as shipped now lost 0.7 on 1 add (0.6472 against 0.6475) and is holding, because the adds it would have sent kept losing
(another 19 points on paper).

## Windows

| name | span (UTC) | report hours | parents | PF without Block |
|---|---|---:|---:|---:|
| W5 dev / sealed | 10-29 18:00 .. 11-01 18:00 | 32 / 16 | 196 / 107 | 0.302 / 0.788 |
| W3 dev / sealed | 11-01 18:00 .. 11-04 18:00 | 32 / 16 | 233 / 228 | 0.538 / 0.912 |
| W2 dev / sealed | 11-04 18:00 .. 11-07 18:00 | 32 / 16 | 222 / 74 | 0.523 / 0.944 |
| W1 dev / sealed | 11-07 18:00 .. 11-10 18:00 | 32 / 16 | 405 / 204 | 0.708 / 0.978 |
| W4 | 11-09 18:00 .. 11-11 11:00 | 17 | 190 | 0.495 |

W1 and W2 (four windows) were the development set. W3 and W4 (three windows) were fetched for confirmation and read once, after the finalists
were fixed. W5 (two windows) was fetched last and used only in the final before / now table.

## 1. What an add is worth before any evidence rule

Every parent that reached a level, an add there to the parent's exit, one PositionCost. Adds' PF / count / net % of one base position at 3
PositionCosts in favour, by the parent's Set type:

| window | trailing parents (their own PF) | base parents (their own PF) | Exit parents |
|---|---|---|---|
| W1 dev | 2.01 / 49 / +30 (1.10) | 0.72 / 120 / −27 (0.62) | 0.35 / 11 / −11 |
| W1 sealed | 1.21 / 19 / +2 (0.91) | 1.27 / 83 / +14 (1.27) | 0.25 / 14 / −14 |
| W2 dev | 0.53 / 17 / −4 (0.46) | 0.49 / 106 / −51 (0.55) | 0 / 4 / −5 |
| W2 sealed | 4.32 / 19 / +9 (9.48) | 0.92 / 15 / −1 (0.61) | 0.06 / 10 / −10 |
| W3 dev | no trailing parent | 0.44 / 99 / −34 (0.58) | 0.18 / 15 / −18 |
| W3 sealed | 0 / 2 / −3 (0) | 1.44 / 98 / +21 (1.11) | 0.02 / 13 / −16 |
| W4 | 1.17 / 14 / +1 (0.68) | 0.56 / 69 / −37 (0.52) | 0.08 / 13 / −20 |

- The adds are positive where the parents of that type are (trailing on W1 dev and W2 sealed, base on W1 sealed and W3 sealed) and negative where
  they are not. Which type wins changes from one 16–32 h window to the next.
- Adds on Exit-Set parents (no target) lost on seven of seven windows.
- Adds past the middle of the way to a fixed target lost on seven of seven windows (0 of 7 positive at 2 and at 4 PositionCosts, −40 and −48 net
  points in all): little room is left and the whole way back to the stop is below.
- No other state of an add had a stable sign over the seven windows: how fast the level was reached (within 3 min, 4–15, after 15, after 60),
  the room left in PositionCosts, long / short, the lane, the level itself, or whether the parent's Set was still valid with its Axis allowing
  (the engine's precondition; the adds it lets through read no better than those it refuses).

## 2. The sweep and its confirmation

`scripts/block_coordination_study.py sweep`: relations (each alone, pairs, all four; every-relation-must-agree and any-relation-agrees), eleven
count tables (1–6 counts, steps of 1–6 PositionCosts), the first 0 / 1 counts observing, minimum observations 1 / 3, parents filtered by Set
type, lane and TP range, with and without "the relation's own parent net at or above zero": 11,704 coordinations. The shipped one reproduces
`stack_eval.py` to the digit on all four development windows.

- Development: 9,533 coordinations make at least one add; 2,264 have a positive pooled add net; **25** are at or above "without" on each of the
  four windows with at least 20 adds. The best family (any of symbol / step agrees, three counts one PositionCost apart, Long / Wide / trailing
  parents) reads +49 to +56 net points on 200–289 adds, adds' PF 1.25.
- Confirmation on W3 dev, W3 sealed and W4: that family reads **−44 to −49** on 160–190 adds. 24 of the 25 fail; the one that holds made 3 adds
  (+1.7). The shipped Block is below "without" on all three as well.
- The development result was selection among thousands of settings. Nothing was shipped from the sweep.

### The structural sweep (counts, volume ratios, stacking)

`--grid structure`: every structural setting crossed without any parent filter: the nine relation sets in both modes, eleven count tables (1 to
12 counts, steps of 1 to 4 PositionCosts), ratios 0.25 / 0.5 / 1.0 Additive and Shared with a budget of 1 and 2, the first 0 / 1 / 2 counts
observing, minimum observations 1 / 5 / 10, PF threshold ratio 0.2 / 0.8 / 2 / 5: **22,680 coordinations**. 19,982 make at least one add,
2,448 have a positive pooled add net, and **none** is at or above "without" on all four development windows (W2 dev is negative for every one of
them). A volume ratio scales the adds' result, it cannot change its sign; Shared against Additive changes how the same adds are sized.

## 3. What was changed: three rules that only remove adds

All three live in `server/pulse/block_rules.py`, the module the engine book (`set_block.SetBlockBook`) and both simulators call, so none of them
re-implements a rule. None can create an add.

- **Deficit guard** (`blockGuard`, shipped on): the book's own executed adds must not be in deficit. While their cumulative net is below zero no
  add is sent; the add that would have gone is followed on paper from its price (once per count and parent); when the paper adds have earned the
  deficit back both restart at zero and adding resumes. It reads no window and needs no minimum count. The record is in the Block state file
  and survives a restart. A relation's own executed record already stopped that relation after a losing add; the guard is the book's.
- **Target share** (`blockTargetShare`, shipped 0.5): a count of a fixed-target parent adds only up to that share of the way from the base
  entry to the target. A parent that ignores its target is not bound.
- **Parent types** (`blockKinds`, shipped base + trailing): Exit-Set parents receive no add; their observations still collect.

Before / now, the book carried chronologically over the nine windows (13 days, 1,859 parents); adds and the adds' net % per window:

| | W5d | W5s | W3d | W3s | W2d | W2s | W1d | W1s | W4 | adds | net | PF with / without |
|---|---|---|---|---|---|---|---|---|---|---:|---:|---|
| Block before (2026-11-05) | 1 +0.9 | 1 −0.7 | 2 −3.5 | 4 −12.3 | 2 −2.8 | 1 0.0 | 6 −3.6 | 0 | 0 | 17 | −22.1 | 0.6392 / 0.6475 |
| + target share 0.5 | 0 | 1 −0.7 | 1 −1.5 | 2 −5.1 | 2 −2.8 | 1 0.0 | 6 −3.6 | 0 | 0 | 13 | −13.8 | 0.6427 / 0.6475 |
| + deficit guard | 1 +0.9 | 1 −0.7 | 2 −3.5 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | −3.4 | 0.6462 / 0.6475 |
| **Block now (all three)** | 0 | 1 −0.7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | −0.7 | **0.6472 / 0.6475** |

With a fresh book in every window (the worst case for the guard, it pays its first loss nine times): before 41 adds −24.9, now 19 adds −14.9.

Read this for what it is: the Block now sits at "without" because it almost stopped adding, and it stopped because its own adds lost. It resumes
by itself when the adds it follows on paper have earned the deficit back; over these 13 days they lost a further 19 points instead.

## 4. Structure checked

`scripts/regression/block_structure.py`, through the engine's own book:

- Every count is independent: its own step (levels 1 / 3 / 6 PositionCosts for steps 1 + 2 + 3), its own ratio, its own evidence record. With
  count 2's record losing, counts 1 and 3 add and count 2 does not.
- Six counts one PositionCost apart (levels 1..6) with ratios 0.1 / 0.2 / 0.3 / 0.4 / 0.5 / 1.0 run six independent procedures: with the group's
  previous positions having taught counts 1, 3, 4, 6 a winning record and counts 2, 5 a losing one, a new parent gets exactly the adds of counts
  1, 3, 4 and 6, each its own ratio of the original volume (1, 3, 4 and 10 of a base 10; final 28), the threshold rising only with the volume
  already stacked; its close adds one observation to each count reached and one executed result to each count added, in its own groups only.
- Stacking is from the parent's ORIGINAL volume: ratios 0.5 and 0.25 on a base volume 10 add 5.0 and 2.5 (final 17.5), never a share of the
  grown position; a count adds once; Shared splits one budget over the counts that are on.
- Relations for the open parents: "set" is taught only by that Set's own parents; "step" (Overall) by every Set of the pack and TP step on that
  lane and direction, not by another step or direction; with both selected both must agree. A parent that gets no add still observes, each
  count once, from the price an add would have had when the count was first seen reached.
- The guard, the target share and the parent types as described above; the Set type is read correctly from the id of all 2,280 x01 Sets.

No structural defect was found in the counts, ratios, stacking or relations.

## Reproduce

```bash
python3 scripts/block_coordination_study.py parents --data DATA --cache stack.pkl --out parents.pkl --window dev --patch '{"histLookbackBars":1440}'
```

```bash
python3 scripts/block_coordination_study.py explore --parents parents.pkl
```

```bash
python3 scripts/block_coordination_study.py sweep --parents dev1.pkl dev2.pkl --confirm fresh1.pkl fresh2.pkl
```

Revert to the earlier Block: `blockGuard: false`, `blockTargetShare: 0`, `blockKinds: ["base", "exit", "trail"]`.

## 5. Block rounds: Block as volume control over rounds of N positions (owner definition, later the same day)

The owner's definition of Block is not the add into an open parent measured above. It is sizing over rounds: a group's positions form blocks of
N; a losing block raises the next blocks by the ratio from the original base volume; the raise is kept until the volume-weighted net since
the losing block is back at or above zero; with Step every further losing block raises one more level; after a positive block or a recovery
the next `pause` blocks run at base volume. Every group (relation key) runs its own procedure.

`scripts/block_rounds_study.py` simulates exactly that on the stored executions (sizing changes no entry; a position's volume is decided at
its open from the blocks completed before it). Nine windows in time order, one carried book, 1,859 positions, unit PF 0.6475, net −495.1 %.

- 2,304 settings (8 relations x N 1–6 x ratio 0.25 / 0.5 / 1 / 2 x one level / step to 3 / step to 6 x pause 0–3, ceiling x 5): the
  volume-weighted PF is above the unit PF for **2,262** of them, the net per unit of volume is better for 2,244, and 434 are at or above the
  unit PF on all nine windows. This is broad, not a handful of lucky settings: on these 13 days the positions opened after a losing block were
  better than the average position (the same anti-persistence the relation lanes and the Axis `last` rule showed from the other side).
- The absolute net is another matter. The base is under PF 1, so more volume loses more: the weighted net is above the unit net for only
  **144** of the 2,304. By relation (mean weighted PF / mean net): step (Overall: pack + TP step) 0.689 / −616, Set 0.665 / −498, type 0.685 /
  −1,161, overall 0.656 / −1,472. A higher ratio raises the PF and the loss together (0.25: 0.663 / −724; 2.0: 0.680 / −1,473).
- With a ceiling of x 2 or x 3 (864 settings) 16 are at or above the unit PF on all nine windows with a net not below the unit net, all of
  them relation step with N 5: e.g. ratio 0.5, step to 2, ceiling x 2: PF 0.706 against 0.648, net −488.4 against −495.1, 587 positions
  raised; ratio 1, ceiling x 3: 0.750, −480.2.
- Forward check: 65 settings pass that rule on the first five windows; on the last four, with a fresh book, **none** is at or above the unit
  PF on all four. And 15 finalists chosen on W1 + W2 by the same rule: 0 hold on W5 + W3 + W4 (PF about equal, net 9 to 56 points worse).

So Block rounds does what it is meant to do to the PF (with above without, widely), does not make the result positive (no window reaches
PF 1 through it), and no setting chosen on earlier windows kept "better on every window" on later ones. It is not in the engine yet: it
changes the size of real positions by up to the ceiling on a base that loses, which is the owner's call.
