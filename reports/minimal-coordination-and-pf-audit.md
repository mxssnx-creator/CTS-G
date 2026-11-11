# Minimal coordination and PF audit

## Rules and corrections

- Base and Main long/short windows retain their own net-PF thresholds. A minimal window is a separate entry-coordination condition: 1–3 results, with positive, negative, both, and average-sign modes. A sign mismatch is reported as a coordination wait when the Set otherwise passes PF/risk validation. It does not remove the Set from the PF-validated population. A genuine long/short, Real, drawdown, or other risk failure remains a failure.
- The existing Negative lane remains a separate route, including its own Real Negative gate. A negative minimal condition accepts negative net results; it does not require a positive minimal-window PF. Minimal statistics report matching Sets and orders but no PF verdict.
- The Main PF figure now uses Sets that passed Main long/short PF. The former figure also included final Sets admitted through the Negative bypass: a controlled example has Main PF 2.00 on one passing Set, but 0.75 when a second Set with failing Main PF enters through the Negative lane. `mainAllLanes` retains that latter population explicitly. Empty windows display a dash; 99 is the engine's no-loss sentinel, not a measured PF of exactly 99.
- Main long N is independently clamped to 10–200. A smaller Base N no longer truncates Main N. The window-grid evaluator reloads each configuration so its short-window candidates follow its own Main N.
- A live result is attributed to its actual Set, not also to a different trailing Set. Non-finite results and orphan venue fills without a verified entry basis do not become artificial cost-only losses. The live tape retains up to 200 newest closes for each Real lane, ordered by close time. Round-trip PositionCost remains 0.12% and is deducted once.
- Main, Real-positive and Real-negative total order counts now add each accepted live close to its actual lane. Counts remain accurate when old closes leave the bounded window.
- The walk-forward evaluator checks gates at the completed candle's entry time and refreshes at a cache expiry boundary. The hold-time adapter resolves each Set's selected tape, including cached choices. The independent chain checker now applies each window's configured size-dependent PF floor.
- Historical Base-short and Main PF checks now use the same four-decimal threshold decision as the current Set verdict. The replay rounds only close to a threshold, so the common path avoids unnecessary rounding work.

`pfBase.baseOnly` and `validatedBasePct` describe Base PF/risk validation; `pfBase.base` and `validatedPct` describe the final PF-validated population across both lanes. `eligible` and `coordinationWaiting` describe entry readiness. The index's `v` is PF validation and `on` is readiness. These are different populations. Real paper streams and live execution records also have different purposes and should not be interpreted as interchangeable.

## Short historical checks

Two supplied BTC/ETH one-minute series were sampled on 14 October 2026 using 0.12% round-trip cost. The stage-chain audit used a six-hour test window and six hours of pre-history, one SL ratio, steps 2–30, four trailing variants, and 122 Sets. It disabled automatic hold for the chain comparison. Twenty Sets and 215 orders matched the independent chain; 553 stop and 110 target exits matched their configured distances. The full results are in `main-minimal-negative-market-audit.txt` and `.json`.

The separate rolling SetBook snapshots use six-hour histories at 18:07, 20:37, and 23:07 UTC, automatic hold enabled, the same bounded grid, and 1-result minimal coordination at both stages. At 20:37, examples for the general strategy were:

| Minimal mode | Set type | Step range | Base PF-valid Sets / configured | Base PF | Base-window orders | Main PF-valid Sets | Main PF | Main shadow orders | Real-lane shadow orders | Ready Sets |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Negative | Normal | 2–6 | 3 / 5 | 2.2771 | 45 | 1 | no loss | 2 | 0 | 0 |
| Negative | Normal | 7–12 | 6 / 6 | 9.1661 | 43 | 1 | no loss | 1 | 0 | 0 |
| Negative | Exit | 2–6 | 2 / 5 | 1.7068 | 14 | 0 | — | 0 | 0 | 0 |
| Positive | Normal | 2–6 | 3 / 5 | 2.2771 | 45 | 3 | 1.8596 | 33 | 36 | 3 |
| Positive | Normal | 7–12 | 6 / 6 | 9.1661 | 43 | 6 | 3.1089 | 30 | 20 | 6 |
| Positive | Trailing | 2–6 | 1 / 1 | no loss | 6 | 1 | no loss | 4 | 2 | 1 |

These counts sum Set/lane shadow orders, not distinct exchange positions; a shadow order can appear in more than one Set or lane. The complete per-pack, type, and range breakdowns, including 13–20 and 21–30, are in `minimal-negative-market-snapshots.json` and `minimal-positive-market-snapshots.json`. Reproduce the rolling snapshots with `scripts/minimal_coordination_snapshot.py` and local one-minute OHLCV JSON; it uses at most 11 hours per symbol.

The negative sample had no ready Sets or Real-lane shadow orders at the three snapshot times. The positive sample is also small and includes shared signals and no-loss windows. Neither establishes expected live PF. No live exchange fills were available for this audit. In particular, a high selection-window PF cannot by itself guarantee that later orders or live executions will achieve the same PF.

## Remaining investigation

The Set tape currently combines replayed shadow rows with live closes. Whether a live close overlaps a replayed shadow trade cannot be resolved safely from the sampled data alone; doing so requires a reliable shared trade identity and live reconciliation evidence. The Main short-window choice is recomputed on replay; a live-only close can change the underlying evidence before the next replay. These areas require further reconciliation with real position and fill records before claiming complete live-stage correctness.
